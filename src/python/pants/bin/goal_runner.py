# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import logging
import sys
from typing import List

from pants.base.specs import AddressSpecs, Specs
from pants.base.workunit import WorkUnit, WorkUnitLabel
from pants.build_graph.build_configuration import BuildConfiguration
from pants.build_graph.build_file_parser import BuildFileParser
from pants.engine.legacy.graph import LegacyBuildGraph
from pants.engine.round_engine import RoundEngine
from pants.goal.context import Context
from pants.goal.goal import Goal
from pants.goal.run_tracker import RunTracker
from pants.init.engine_initializer import LegacyGraphSession
from pants.java.nailgun_executor import NailgunProcessGroup
from pants.option.options import Options
from pants.option.ranked_value import RankedValue
from pants.reporting.reporting import Reporting
from pants.task.task import QuietTaskMixin


logger = logging.getLogger(__name__)


class FilesystemSpecsUnsupported(Exception):
  """FS specs are not yet backported to V1 (but will be to replace `--owner-of`)."""


class GoalRunnerFactory:
  def __init__(
    self,
    root_dir: str,
    options: Options,
    build_config: BuildConfiguration,
    run_tracker: RunTracker,
    reporting: Reporting,
    graph_session: LegacyGraphSession,
    specs: Specs,
    exiter=sys.exit,
  ) -> None:
    """
    :param root_dir: The root directory of the pants workspace (aka the "build root").
    :param options: The global, pre-initialized Options instance.
    :param build_config: A pre-initialized BuildConfiguration instance.
    :param run_tracker: The global, pre-initialized/running RunTracker instance.
    :param reporting: The global, pre-initialized Reporting instance.
    :param graph_session: The graph session for this run.
    :param specs: The specs for this run, i.e. either the address or filesystem specs.
    :param func exiter: A function that accepts an exit code value and exits. (for tests, Optional)
    """
    self._root_dir = root_dir
    self._options = options
    self._build_config = build_config
    self._run_tracker = run_tracker
    self._reporting = reporting
    self._graph_session = graph_session
    self._specs = specs
    self._exiter = exiter

    self._global_options = options.for_global_scope()
    self._fail_fast = self._global_options.fail_fast
    self._explain = self._global_options.explain
    self._kill_nailguns = self._global_options.kill_nailguns

    if self._specs.filesystem_specs.dependencies:
      pants_bin_name = self._global_options.pants_bin_name
      v1_goals = ' '.join(repr(goal) for goal in self._determine_v1_goals(options))
      provided_specs = ' '.join(spec.glob for spec in self._specs.filesystem_specs)
      approximate_original_command = f"{pants_bin_name} {v1_goals} {provided_specs}"
      suggested_owners_args = " ".join(
        f"--owner-of={spec.glob}" for spec in self._specs.filesystem_specs
      )
      suggestion = f"run `{pants_bin_name} {suggested_owners_args} {v1_goals}`."
      trying_to_use_globs = any("*" in spec.glob for spec in self._specs.filesystem_specs)
      if trying_to_use_globs:
        suggestion = (
          f"run `{pants_bin_name} --owner-of=src/python/f1.py --owner-of=src/python/f2.py "
          f"{v1_goals}`. (You must explicitly enumerate every file because " f"`--owner-of` does "
          f"not support globs.)"
        )
      raise FilesystemSpecsUnsupported(
        f"Instead of running `{approximate_original_command}`, {suggestion}\n\n"
        f"Why? Filesystem specs like `src/python/example.py` and `src/**/*.java` (currently) only "
        f"work when running goals implemented with the V2 engine. When using V1 goals, either use "
        f"traditional address specs like `src/python/example:foo` and `::` or use `--owner-of` "
        f"for Pants to find the file's owning target(s) for you.\n\n"
        f"(You may find which goals are implemented in V1 by running `{pants_bin_name} --v1 --no-v2 "
        f"goals` and find V2 goals by running `{pants_bin_name} --no-v1 --v2 goals`.)"
      )

  def _determine_v1_goals(self, options: Options) -> List[Goal]:
    """Check and populate the requested goals for a given run."""
    v1_goals, ambiguous_goals, _ = options.goals_by_version
    return [Goal.by_name(goal) for goal in v1_goals + ambiguous_goals]

  def _address_specs_to_targets(self, build_graph: LegacyBuildGraph, address_specs: AddressSpecs):
    """Populate the BuildGraph and target list from a set of input TargetRoots."""
    with self._run_tracker.new_workunit(name='parse', labels=[WorkUnitLabel.SETUP]):
      return [
        build_graph.get_target(address)
        for address
        in build_graph.inject_roots_closure(address_specs, self._fail_fast)
      ]

  def _should_be_quiet(self, goals):
    if self._explain:
      return True

    if self._global_options.get_rank('quiet') > RankedValue.HARDCODED:
      return self._global_options.quiet

    return any(goal.has_task_of_type(QuietTaskMixin) for goal in goals)

  def _setup_context(self):
    with self._run_tracker.new_workunit(name='setup', labels=[WorkUnitLabel.SETUP]):
      build_file_parser = BuildFileParser(self._build_config, self._root_dir)
      build_graph, address_mapper = self._graph_session.create_build_graph(
        self._specs,
        self._root_dir
      )

      goals = self._determine_v1_goals(self._options)
      is_quiet = self._should_be_quiet(goals)

      target_root_instances = self._address_specs_to_targets(
        build_graph, self._specs.address_specs,
      )

      # Now that we've parsed the bootstrap BUILD files, and know about the SCM system.
      self._run_tracker.run_info.add_scm_info()

      # Update the Reporting settings now that we have options and goal info.
      invalidation_report = self._reporting.update_reporting(self._global_options,
                                                             is_quiet,
                                                             self._run_tracker)

      context = Context(options=self._options,
                        run_tracker=self._run_tracker,
                        target_roots=target_root_instances,
                        requested_goals=self._options.goals,
                        build_graph=build_graph,
                        build_file_parser=build_file_parser,
                        build_configuration=self._build_config,
                        address_mapper=address_mapper,
                        invalidation_report=invalidation_report,
                        scheduler=self._graph_session.scheduler_session)

      return goals, context

  def create(self):
    goals, context = self._setup_context()
    return GoalRunner(context=context,
                      goals=goals,
                      run_tracker=self._run_tracker,
                      kill_nailguns=self._kill_nailguns)


class GoalRunner:
  """Lists installed goals or else executes a named goal.

  NB: GoalRunner represents a v1-only codepath. v2 goals are registered via `@goal_rule` and
  the `pants.engine.goal.Goal` class.
  """

  Factory = GoalRunnerFactory

  def __init__(self, context, goals, run_tracker, kill_nailguns):
    """
    :param Context context: The global, pre-initialized Context as created by GoalRunnerFactory.
    :param list[Goal] goals: The list of goals to act on.
    :param Runtracker run_tracker: The global, pre-initialized/running RunTracker instance.
    :param bool kill_nailguns: Whether or not to kill nailguns after the run.
    """
    self._context = context
    self._goals = goals
    self._run_tracker = run_tracker
    self._kill_nailguns = kill_nailguns

  def _is_valid_workdir(self, workdir):
    if workdir.endswith('.pants.d'):
      return True

    self._context.log.error(
      'Pants working directory should end with \'.pants.d\', currently it is {}\n'
      .format(workdir)
    )
    return False

  def _execute_engine(self):
    engine = RoundEngine()
    sorted_goal_infos = engine.sort_goals(self._context, self._goals)
    RunTracker.global_instance().set_sorted_goal_infos(sorted_goal_infos)
    result = engine.execute(self._context, self._goals)

    if self._context.invalidation_report:
      self._context.invalidation_report.report()

    return result

  def _run_goals(self):
    should_kill_nailguns = self._kill_nailguns

    try:
      with self._context.executing():
        result = self._execute_engine()
        if result:
          self._run_tracker.set_root_outcome(WorkUnit.FAILURE)
    except KeyboardInterrupt:
      self._run_tracker.set_root_outcome(WorkUnit.FAILURE)
      # On ctrl-c we always kill nailguns, otherwise they might keep running
      # some heavyweight compilation and gum up the system during a subsequent run.
      should_kill_nailguns = True
      raise
    except Exception:
      self._run_tracker.set_root_outcome(WorkUnit.FAILURE)
      raise
    finally:
      # Must kill nailguns only after run_tracker.end() is called, otherwise there may still
      # be pending background work that needs a nailgun.
      if should_kill_nailguns:
        # TODO: This is JVM-specific and really doesn't belong here.
        # TODO: Make this more selective? Only kill nailguns that affect state?
        # E.g., checkstyle may not need to be killed.
        NailgunProcessGroup().killall()

    return result

  def run(self):
    global_options = self._context.options.for_global_scope()

    if not self._is_valid_workdir(global_options.pants_workdir):
      return 1

    return self._run_goals()
