# Test lanes on small nodes

Before a finished project goes to review, the pre-review runs the whole test suite twice over: once in the
project's `checks.sh` (900 s) and once as the standards gate's shared head run (the pytest rules that judge the head tree, 1200 s).
Both send their two pytest lanes (`-m "not slow"` and `-m slow`) to child actions through
the `flyte_agent_factory._slow_lane` module when `AGENT_FACTORY_SLOW_LANE=child`. This page is how that works
on a cluster whose largest placeable node has 2 or 4 CPUs, what the knobs are, and how to read the outcome.

Nothing here skips, samples or waives a test, and no time limit was raised: each lane is split across several
small children instead of one big one, and a lane that cannot be placed is reported as such instead of being
run on the session pod until it times out.

## How a lane runs

1. The launcher attaches to the run. Without a run (CI, a laptop) the lane runs here as one pytest process,
   as before.
2. It lists the test files under `tests/` and weighs each by its duration in the last run of the same lane
   (cached per repository and per lane under `${XDG_CACHE_HOME:-~/.cache}/factory-lanes/`), else by the
   repository's own record in `tests/lane_durations.json` (`{lane: {file: seconds}}`, so that a pod that has
   never run the lane still knows the Terraform file is long), else by its size.
3. It picks a shard count so that each shard of `AGENT_FACTORY_LANE_CPUS` CPUs is planned to finish within
   `AGENT_FACTORY_LANE_TARGET_S`, at most `AGENT_FACTORY_LANE_MAX_SHARDS`, and spreads the files
   longest-first. A file is kept whole, so the Terraform benchmark file keeps its owner's cap of two
   concurrent Terraform checks, unless it weighs more than 85% of the longest runtime any child could have
   (the whole budget less on-demand's queue bound). Such a file would run out in every child, so it is split
   by test into the fewest equal parts that fit, named `<file>#i/k`: part `i` is the file's collected tests
   whose position modulo `k` is `i`, so every test of it runs in exactly one shard.
4. It uploads the tree and its history once, and submits every shard at once, on spot first. A shard
   expected to run longer than on-demand would have left after a full spot wait goes on-demand first instead,
   with the runtime of the whole budget, and to spot only if on-demand has no room; the launcher prints
   `slow lane: shard i/N (<files>): about <s>s, more than the <s>s on-demand would have left after waiting
   for spot; on-demand first`. A shard is expected to take its seconds over its CPUs, but no less than its
   longest file's. Each shard collects the suite
   exactly as the unsharded lane would (so `collect_ignore`d fixture projects stay out) and a small pytest
   plugin keeps only its own files; the first shard keeps every file the others do not, so a test the plan
   missed still runs once.
5. Inside a shard, pytest's `-n auto` is sized to the container's CPU quota (`cpu.max`), not the node's CPU
   count: every pod on this cluster reports 16 CPUs whatever its quota. `checks.sh` and the gate's local runs
   do the same through `PYTEST_XDIST_AUTO_NUM_WORKERS`.
6. The shards' junit reports are merged into the one file the caller asked for, and the lane exits with
   pytest's own code over all of them. The last thing a lane prints is its verdict, `=== lane verdict: exit
   N ===` and one line per shard (its child action, its exit and pytest's summary line, or why it did not
   run) followed by the tests that failed in it. `checks.sh` tees each lane's output and repeats both
   verdicts at its very end, because the supervisor keeps only the last 12000 bytes of the output and the
   lane that finishes last would otherwise push the other's verdict out of it.

A shard asks for three quarters of its CPUs as its request and all of them as its limit, because a node's
allocatable CPU is below its size: a request of exactly 2 CPUs never places on a 2-CPU node.

## Knobs

Read when the lane starts. A value that is not a whole number at or above its minimum is reported on stderr
and the default is used.

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_FACTORY_LANE_CPUS` | 2 | CPUs per shard. Set it to the size of the cluster's typical placeable node. |
| `AGENT_FACTORY_LANE_MAX_SHARDS` | 8 | The most shards a lane is split into (the most children at once). |
| `AGENT_FACTORY_LANE_TARGET_S` | 300 | The runtime a shard is planned for. With the lane's estimated seconds it sets the shard count. |
| `AGENT_FACTORY_LANE_BUDGET_S` | 1100 | The caller's wall budget for the lane. `checks.sh` gets 780 (its 900 s less 120), the gate 1100 (1200 less 100). Each attempt's `max_runtime` is what is left when it is submitted, less its queue bound, so that no attempt can end past the budget. A retry that would get less than 60 s is not submitted; the shard is then capacity (`no budget left for a retry after waiting for capacity`). |
| `AGENT_FACTORY_LANE_RUN_HERE` | unset | Set to `1` to run on the session pod, labelled `ran here`, a lane the child actions could not run for a reason that is not capacity, setup or the change (no image looked up, a launcher that could not attach or crashed). Off by default, even in a run: on a 2- or 4-CPU pod the whole lane overruns the limits it was sent away from. A lane without capacity is never run here. |

A shard's resources follow from its CPUs (`_resources.lane_shard`); a deployment profile's `[resources.lanes]` table
replaces any key it sets at every shard size, read from the profile text the pod carries
([small-nodes.md](small-nodes.md#test-lane-shards)).

The queue bounds are fixed: two minutes on spot, then three on on-demand (`_slow_lane.QUEUE_BOUND` and
`ON_DEMAND_QUEUE_BOUND`). So a lane waits at most five minutes for room before it says it has none.

## Outcomes and how to read them

There are three ways a limit can be hit, and the report names which one it was. When shards end
differently, the first of these that applies decides the lane: a failing test, then a shard that ran out of
its own time or memory, then setup, then capacity. So a failing test is never hidden behind another shard that found no
room; each shard that did not report gets a line `slow lane: shard i/N (<files>) did not run: <why>`.

Beside the junit file the launcher writes `<junit>.result`, its outcome record, atomically:
`{"completed": true, "code": N}` when pytest ran to the end, else `{"completed": false, "outcome": "ran
out" | "out of memory" | "setup" | "upload" | "capacity", "code": ..., "reason": "..."}`. The gate reads it to tell "the change ran
out of time" (a failure, not run again on the session pod) from "the launcher never reported". The
supervisor reads the records in `checks.sh`'s junit directory with the `.capacity` files: a failed
`checks.sh` is capacity only when there is a reason file, every lane that left a file has either a
`.capacity` file or a record of a passing run, and there is no evidence against it (no failing junit case,
no record of a lane that completed with failures, none of a lane that ran out, ran out of memory or could not set up). A lane
with neither, which is what the checks' own time limit leaves, is a failure.

- **The change.** A shard that runs out of its own `max_runtime` in its own child prints
  `slow lane: shard i/N (<files>) ran out of its <s>s in its own <c>-CPU child` and the lane fails with exit
  1. Another child would take as long, so it is not retried. This holds for a shard's first attempt, which had
  the whole budget; its retry has only what the first attempt left, so running out there is capacity
  (`ran out of the <s>s on-demand left after no spot capacity`, or `spot` after `on-demand` for a long shard
  that went on-demand first). When the first attempt was placed but lost (`OutputUploadFailed`, a preempted
  node), the line says that instead: `... left after its spot child failed before reporting`. A failing test fails its shard, the lane, the
  gate and the pre-review, and the merged junit names it.
  A shard whose child is killed for its memory limit (`OOMError`) is the change as well: it prints
  `slow lane: shard i/N (<files>) ran out of its <limit> memory in its own <c>-CPU child` (the limit is
  `2 * CPUs + 1` GiB, the same on-demand, so it is not retried), the lane fails with exit 1, the record says
  `out of memory`, and there is no `.capacity` file. The gate reads any record that is not `capacity` this way.
- **Capacity.** A shard that could not be placed within its spot and on-demand bounds, or whose child twice
  ended without reporting, ends the lane with exit 69 and a one-line reason in `<junit>.capacity`, for
  example `no capacity: 1 of 3 shards (2 CPUs) not placed within 2m spot + 3m on-demand`. Nothing is run on
  the session pod instead. The gate turns that into status `no capacity` on the rules that share the head run (not PASS,
  not a project failure, reason `fast lane: ...` or `slow lane: ...`), `checks.sh` into a capacity outcome
  rather than "checks failed". The supervisor does not count it as a preflight bounce: its timeline row ends
  in `, no capacity`, it runs the pre-review again after 5, 10 and 20 minutes, and after a fourth capacity
  outcome it escalates to the human as a capacity problem. A lane that ran and failed is a failure even if
  another lane had no capacity.
- **The environment.** A shard whose child was placed and ran but could not set up the lane (an image
  without git, no network for `uv sync`), on its retry on-demand too, ends the lane with exit 78 and
  `slow lane: setup failed: <n> of <N> shards could not set up the lane ...` with the setup's last line. It
  is neither capacity (waiting for room would not help, so no `.capacity` file) nor the change; the lane is
  not run on the session pod either, and the rules that share the head run fail with that line. `checks.sh` and
  the gate both give their lanes the run's own image (looked up from its root action), because the
  supervisor's `FLYTE_INTERNAL_IMAGE` is the plain Flyte image, which has no git.
- **Command lanes.** Any project's `checks.sh` can send a command to a child with
  `"$AGENT_FACTORY_TEST_LANE" --junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/<lane>.xml" -- <command>`. The command
  is a lane of one shard in one child of `AGENT_FACTORY_LANE_CPUS` CPUs, with the same ladder (spot, then
  on-demand), the same four outcomes, the same records and the same budget; `--on-demand` is accepted and
  ignored. Nothing of it runs on the session pod when a run is attached. Pass `--junit` into
  `$AGENT_FACTORY_PYTEST_JUNIT_DIR` for the pre-review to read its capacity outcome: without `--junit` the
  launcher still exits 69 but leaves no file, and the pre-review reads that as checks failed. A command that
  writes no junit leaves an empty file, which is "no junit", not a run of no tests.
- **Child died before reporting.** A child that dies before it reports for any reason other than OOM or its
  runtime is retried once and then is capacity, because the launcher cannot tell a reclaimed node from a
  broken image, and the capacity path's retry with backoff is the safer default.
- **Launcher failed.** A launcher that ends with no valid completion record and neither capacity nor setup
  (exit 74 the tree could not be uploaded, 75 it could not run in a child, a crash, 127 it could not start,
  a missing or invalid record) while a run is attached fails the lane without running it here: the note is
  `child lane not run (<reason>); not run here either`, the lane's code is the launcher's (1 when that is 0-5),
  and it is neither capacity nor a pass. The failed upload also leaves the record `{"outcome": "upload"}`,
  and a lane that could not set up one saying `setup`: the supervisor's bounce for either says the lane could
  not set up its environment or upload the tree (infrastructure), not that tests failed, and neither is
  waited for as capacity.
- **Lost watch.** When the run's watch drops, the launcher polls the child's phase. A child that ended
  `TIMED_OUT` (not for its queue bound, which is capacity) is handled exactly as `MaxRuntimeExceededError`
  is above, and one killed for its memory exactly as `OOMError`; any other end is a child that died before
  reporting.
- **Load.** A limit hit while a lane ran on the session pod is the pod's load; the gate's output says the
  lane `ran here` and why. A lane runs on the session pod only when no run is attached (CI, a laptop) or the
  project has no slow marker, `uv.lock` or `tests/`. In a run whose image could not be looked up, no lane is
  run at all: the launcher exits 75 and the gate's run says `not run here either`. In a run, the launcher
  `checks.sh` gets passes `--no-local` too, so one that cannot attach exits 75 rather than run the lane here, and `factory-improvement/checks.sh`, which asks its fast lane twice with `--no-local`, does not then run it here either (it does with no run attached). `AGENT_FACTORY_LANE_RUN_HERE=1` turns all of these back into a labelled run here.

Every shard's tail ends with `test-lane: wall <s>s, peak memory <MiB>, <n> workers`, and the launcher prints
one line per shard with its child action's name, whether it ran on spot or on-demand, its seconds and its
code. That is where to look when tuning `AGENT_FACTORY_LANE_CPUS` or the memory request.

## OSS Flyte

The launcher uses only the SDK's public surface that already worked on OSS Flyte: a raw-container task,
`flyte.Resources` with a request and a limit, `flyte.Timeout` (`max_queued_time`, `max_runtime`) and the
`MaxQueuedTimeExceededError`, `MaxRuntimeExceededError` and `OOMError` it raises, and `remote.upload_file`. See
[oss-flyte-compat.md](oss-flyte-compat.md).

## How to verify each criterion

Each row names the tests that pin the criterion (their bodies were read, not guessed from their names) and, where the criterion is a source fact, a command. Test ids are relative to this folder. Criteria 18-22 are live runs: no unit test can stand in for them.

| # | Criterion (short) | Evidence |
|---|---|---|
| 1 | No `cpu=8` child; shards of `AGENT_FACTORY_LANE_CPUS` (default 2), request 3/4, limit all | `grep -n 'cpu=8' src/flyte_agent_factory/_slow_lane.py` finds nothing (exit 1)<br>`tests/test_slow_lane.py::test_a_shards_cpu_request_is_three_quarters_of_its_limit`<br>`tests/test_slow_lane.py::test_the_lane_task_is_built_per_size_and_runtime_and_registered_in_its_own_environment`<br>`tests/test_slow_lane.py::test_the_knobs_default_and_are_read_at_call_time`<br>`tests/test_slow_lane.py::test_the_cpus_knob_sizes_the_shards` |
| 2 | Every file in exactly one shard; split by test only past the longest child | `tests/test_lane_shards.py::test_a_plan_runs_every_file_exactly_once_and_never_splits_one`<br>`tests/test_lane_shards.py::test_the_shards_select_exactly_the_tests_the_unsharded_lane_selects`<br>`tests/test_lane_shards.py::test_the_parts_of_a_split_file_select_each_of_its_tests_once`<br>`tests/test_slow_lane.py::test_a_file_longer_than_any_child_could_run_is_split_by_test_across_shards` |
| 3 | Shard count from last durations, else seed, else size; target and cap | `tests/test_lane_shards.py::test_the_shard_count_covers_the_estimate_within_the_target_and_the_cap`<br>`tests/test_lane_shards.py::test_a_file_without_a_recorded_duration_is_weighed_by_its_size`<br>`tests/test_slow_lane.py::test_the_shards_reports_are_merged_and_their_durations_remembered`<br>`tests/test_slow_lane.py::test_the_repositorys_seed_tells_a_cold_cache_which_file_is_long` |
| 4 | Shard junits merge into the caller's path; exit code is pytest's over all shards | `tests/test_lane_shards.py::test_merged_junit_holds_every_case_of_every_shard_for_the_gates_parser`<br>`tests/test_slow_lane.py::test_the_lanes_code_is_pytests_over_the_shards_and_completion_is_recorded`<br>`tests/test_slow_lane.py::test_a_shard_that_failed_beside_an_unplaced_shard_fails_the_lane_and_names_the_test` |
| 5 | `-n auto` follows the cgroup `cpu.max` quota | `tests/test_lane_shards.py::test_cpu_allowance_is_the_affinity_cut_by_the_cgroup_quota`<br>`tests/test_slow_lane.py::test_the_lane_script_starts_one_xdist_worker_per_cpu_of_the_cgroup_quota`<br>`tests/test_slow_lane.py::test_checks_sh_starts_one_xdist_worker_per_cpu_of_the_quota_not_per_cpu_of_the_node`<br>`tests/test_gate_speed.py::test_local_pytest_is_limited_to_the_cpu_allowance` |
| 6 | At most 2 min spot, then 3 min on-demand; no lane on the session pod while a run is attached | `tests/test_slow_lane.py::test_the_spot_wait_is_bounded_short_and_the_request_fits_a_spot_node`<br>`tests/test_gate_speed.py::test_a_lane_without_capacity_is_not_run_here`<br>`tests/test_gate_speed.py::test_a_lane_the_launcher_did_not_run_fails_without_running_here_and_says_why`<br>`tests/test_test_lane_any_project.py::test_opted_in_a_launcher_in_a_run_runs_here_what_it_cannot_send_away` |
| 7 | A shard never placed: exit 69 and a one-line `<junit>.capacity` | `tests/test_slow_lane.py::test_a_shard_with_no_capacity_ends_the_lane_with_69_and_a_reason_and_no_completion`<br>`tests/test_slow_lane.py::test_without_a_junit_path_a_lane_with_no_capacity_still_exits_69_and_says_why`<br>`tests/test_slow_lane.py::test_a_capacity_file_of_an_earlier_run_is_removed_at_the_start` |
| 8 | The gate shows `no capacity`: neither PASS nor a project failure | `tests/test_gate_speed.py::test_pr_005_and_eng_004_report_no_capacity_and_it_blocks`<br>`tests/test_gate_speed.py::test_a_lane_without_capacity_is_not_run_here`<br>`tests/test_gate_speed.py::test_a_real_failure_in_the_other_lane_is_still_a_failure` |
| 9 | Capacity is not a preflight bounce; retries after 5/10/20 min; escalates after the 4th | `tests/test_capacity_wait.py::test_a_capacity_only_pre_review_retries_by_itself_and_then_reviews`<br>`tests/test_capacity_wait.py::test_three_capacity_retries_escalate_to_a_human_with_capacity_wording`<br>`tests/test_capacity_wait.py::test_a_capacity_outcome_in_a_mixed_pre_review_bounces_as_today`<br>`tests/test_capacity_wait.py::test_the_gate_reporting_no_capacity_waits_too` |
| 10 | Attempt runtimes come from the 780 s / 1100 s budget; the 900 s and 1200 s limits are unchanged | source check 10 below<br>`tests/test_slow_lane.py::test_a_shards_runtime_is_what_the_budget_has_left_after_its_queue_bound`<br>`tests/test_slow_lane.py::test_a_late_on_demand_retry_gets_only_what_the_budget_has_left`<br>`tests/test_gate_speed.py::test_the_launcher_gets_a_wall_budget_below_the_test_timeout`<br>`tests/test_gate_speed.py::test_checks_sh_gets_the_lane_budget_and_the_xdist_worker_count` |
| 11 | A failing test fails the shard, lane, gate and pre-review, is named in the junit, and beats a capacity outcome | `tests/test_slow_lane.py::test_the_lanes_verdict_is_the_last_thing_it_prints_and_names_the_failed_test`<br>`tests/test_slow_lane.py::test_a_shard_that_failed_beside_an_unplaced_shard_fails_the_lane_and_names_the_test`<br>`tests/test_gate_speed.py::test_a_real_failure_in_the_other_lane_is_still_a_failure`<br>`tests/test_gate_speed.py::test_a_lane_that_ran_and_failed_beside_a_capacity_file_is_an_ordinary_failure` |
| 12 | Out of runtime on spot, or OOM, is the change; out on-demand after a spot wait is capacity; setup is exit 78 | `tests/test_slow_lane.py::test_a_shard_that_runs_out_of_its_own_time_is_a_failure_of_the_change_not_of_capacity`<br>`tests/test_slow_lane.py::test_a_shard_killed_for_memory_is_a_failure_of_the_change_not_of_capacity`<br>`tests/test_slow_lane.py::test_a_shard_that_ran_out_of_a_runtime_its_spot_wait_shortened_is_capacity_not_the_change`<br>`tests/test_slow_lane.py::test_a_setup_failure_is_retried_once_on_demand_then_is_a_setup_failure_not_capacity` |
| 13 | `<junit>.result` tells change, capacity, setup and "never reported" apart; no record and no reason file is a failure | `tests/test_slow_lane.py::test_the_lanes_code_is_pytests_over_the_shards_and_completion_is_recorded`<br>`tests/test_slow_lane.py::test_a_shard_that_ran_out_leaves_an_outcome_record_and_the_junit_of_what_ran`<br>`tests/test_gate_speed.py::test_a_lane_whose_change_ran_out_is_a_failure_and_is_not_run_here`<br>`tests/test_gate_speed.py::test_a_failing_checks_script_without_a_capacity_file_is_an_ordinary_failure` |
| 14 | A lane run on the session pod without an attached run is labelled `ran here` | `tests/test_gate_speed.py::test_outside_a_run_both_lanes_run_here_and_say_why` |
| 15 | Any project's checks.sh can send a command lane through `$AGENT_FACTORY_TEST_LANE` | `tests/test_test_lane_any_project.py::test_a_root_layout_repo_and_a_workspace_project_get_the_same_lane`<br>`tests/test_test_lane_any_project.py::test_the_launcher_sends_a_command_to_the_child_when_a_run_is_attached`<br>`tests/test_test_lane_any_project.py::test_a_command_the_child_could_not_place_is_capacity_and_never_runs_here`<br>`tests/test_test_lane_any_project.py::test_every_ending_of_a_command_is_the_one_a_shard_has` |
| 16 | Only OSS-compatible SDK surface; `docs/oss-flyte-compat.md` updated | source checks 16 below<br>`tests/test_slow_lane.py::test_the_lane_task_is_a_raw_container_registered_in_its_own_environment`<br>No test pins that the launcher stays inside the documented surface; the commands show it, the test pins the raw-container task. |
| 17 | No base test deleted, none skipped, no waiver added | source checks 17 below<br>`tests/test_lane_shards.py::test_the_shards_select_exactly_the_tests_the_unsharded_lane_selects`<br>Not a test: whether an assertion was weakened can only be read from `git diff 06d9663^ -- tests`. |
| 18 | Gate head run, 2-CPU shards, passes within 1200 s | Live: `test-lanes-evidence/gate-2cpu.txt`, written by `python3 ops/lane_evidence.py <scenario>` (see "Measured on this cluster") |
| 19 | Gate head run, 4-CPU shards, passes within 1200 s | Live: `test-lanes-evidence/gate-4cpu.txt`, written by `python3 ops/lane_evidence.py <scenario>` (see "Measured on this cluster") |
| 20 | One failing test: the 2-CPU gate run fails, names the test, no capacity outcome | Live: `test-lanes-evidence/gate-2cpu-failing.txt`, written by `python3 ops/lane_evidence.py <scenario>` (see "Measured on this cluster") |
| 21 | Supervisor's `checks.sh` step passes within 900 s at 2 and 4 CPUs | Live: `test-lanes-evidence/checks-2cpu.txt` and `test-lanes-evidence/checks-4cpu.txt`, written by `python3 ops/lane_evidence.py <scenario>` (see "Measured on this cluster") |
| 22 | 64-CPU shards: `no capacity`, 0 tests run on the session pod | Live: `test-lanes-evidence/gate-64cpu.txt`, written by `python3 ops/lane_evidence.py <scenario>` (see "Measured on this cluster") |
| 23 | Each of 18-22 has a committed evidence file from a committed script | `docs/test-lanes-evidence/*.txt`; the script `ops/lane_evidence.py` is pinned by `tests/test_lane_evidence.py` (what each scenario runs, at which size, in a scratch clone, and what its file records) |

Source checks, run from this folder:

```sh
# 10: the 1200 s and 900 s limits are where they were, and unchanged since the import base
grep -nE '^TEST_TIMEOUT = |^_CHECKS_TIMEOUT_S = ' src/flyte_agent_factory/_gate_base.py src/flyte_agent_factory/_supervise_support.py
git diff 06d9663^ -U0 -- src/flyte_agent_factory/_gate_base.py src/flyte_agent_factory/_supervise_support.py \
  | grep -E '^[-+].*(TEST_TIMEOUT =|_CHECKS_TIMEOUT_S =)'    # prints nothing

# 16: the SDK surface the launcher imports, the doc rows that classify it, and that the doc changed
grep -nE '^(import|from) (flyte|httpx|obstore|connectrpc)' src/flyte_agent_factory/_slow_lane.py
grep -n '_slow_lane' docs/oss-flyte-compat.md
git diff 06d9663^ --stat -- docs/oss-flyte-compat.md

# 17: every `def test_` of the import base still exists in its file, and none was marked skip or xfail
for f in $(git ls-tree -r --name-only 06d9663^ tests/ | grep '\.py$'); do
  comm -23 <(git show "06d9663^:./$f" | grep -oE 'def test_\w+' | sort -u) \
           <(grep -oE 'def test_\w+' "$f" 2>/dev/null | sort -u) | sed "s|^|$f: |"
done                                                       # prints nothing
git diff 06d9663^ -U0 -- tests | grep -E '^\+[^+].*(mark\.skip|mark\.xfail|pytest\.skip|skipif)'   # prints nothing
```

Every test cited above, in one command (about 20 s):

```sh
PYTHONPATH=src uv run python -m pytest -q -p no:randomly -n 0 \
  tests/test_slow_lane.py::test_a_shards_cpu_request_is_three_quarters_of_its_limit \
  tests/test_slow_lane.py::test_the_lane_task_is_built_per_size_and_runtime_and_registered_in_its_own_environment \
  tests/test_slow_lane.py::test_the_knobs_default_and_are_read_at_call_time \
  tests/test_slow_lane.py::test_the_cpus_knob_sizes_the_shards \
  tests/test_lane_shards.py::test_a_plan_runs_every_file_exactly_once_and_never_splits_one \
  tests/test_lane_shards.py::test_the_shards_select_exactly_the_tests_the_unsharded_lane_selects \
  tests/test_lane_shards.py::test_the_parts_of_a_split_file_select_each_of_its_tests_once \
  tests/test_slow_lane.py::test_a_file_longer_than_any_child_could_run_is_split_by_test_across_shards \
  tests/test_lane_shards.py::test_the_shard_count_covers_the_estimate_within_the_target_and_the_cap \
  tests/test_lane_shards.py::test_a_file_without_a_recorded_duration_is_weighed_by_its_size \
  tests/test_slow_lane.py::test_the_shards_reports_are_merged_and_their_durations_remembered \
  tests/test_slow_lane.py::test_the_repositorys_seed_tells_a_cold_cache_which_file_is_long \
  tests/test_lane_shards.py::test_merged_junit_holds_every_case_of_every_shard_for_the_gates_parser \
  tests/test_slow_lane.py::test_the_lanes_code_is_pytests_over_the_shards_and_completion_is_recorded \
  tests/test_slow_lane.py::test_a_shard_that_failed_beside_an_unplaced_shard_fails_the_lane_and_names_the_test \
  tests/test_lane_shards.py::test_cpu_allowance_is_the_affinity_cut_by_the_cgroup_quota \
  tests/test_slow_lane.py::test_the_lane_script_starts_one_xdist_worker_per_cpu_of_the_cgroup_quota \
  tests/test_slow_lane.py::test_checks_sh_starts_one_xdist_worker_per_cpu_of_the_quota_not_per_cpu_of_the_node \
  tests/test_gate_speed.py::test_local_pytest_is_limited_to_the_cpu_allowance \
  tests/test_slow_lane.py::test_the_spot_wait_is_bounded_short_and_the_request_fits_a_spot_node \
  tests/test_gate_speed.py::test_a_lane_without_capacity_is_not_run_here \
  tests/test_gate_speed.py::test_a_lane_the_launcher_did_not_run_fails_without_running_here_and_says_why \
  tests/test_test_lane_any_project.py::test_opted_in_a_launcher_in_a_run_runs_here_what_it_cannot_send_away \
  tests/test_slow_lane.py::test_a_shard_with_no_capacity_ends_the_lane_with_69_and_a_reason_and_no_completion \
  tests/test_slow_lane.py::test_without_a_junit_path_a_lane_with_no_capacity_still_exits_69_and_says_why \
  tests/test_slow_lane.py::test_a_capacity_file_of_an_earlier_run_is_removed_at_the_start \
  tests/test_gate_speed.py::test_pr_005_and_eng_004_report_no_capacity_and_it_blocks \
  tests/test_gate_speed.py::test_a_real_failure_in_the_other_lane_is_still_a_failure \
  tests/test_capacity_wait.py::test_a_capacity_only_pre_review_retries_by_itself_and_then_reviews \
  tests/test_capacity_wait.py::test_three_capacity_retries_escalate_to_a_human_with_capacity_wording \
  tests/test_capacity_wait.py::test_a_capacity_outcome_in_a_mixed_pre_review_bounces_as_today \
  tests/test_capacity_wait.py::test_the_gate_reporting_no_capacity_waits_too \
  tests/test_slow_lane.py::test_a_shards_runtime_is_what_the_budget_has_left_after_its_queue_bound \
  tests/test_slow_lane.py::test_a_late_on_demand_retry_gets_only_what_the_budget_has_left \
  tests/test_gate_speed.py::test_the_launcher_gets_a_wall_budget_below_the_test_timeout \
  tests/test_gate_speed.py::test_checks_sh_gets_the_lane_budget_and_the_xdist_worker_count \
  tests/test_slow_lane.py::test_the_lanes_verdict_is_the_last_thing_it_prints_and_names_the_failed_test \
  tests/test_gate_speed.py::test_a_lane_that_ran_and_failed_beside_a_capacity_file_is_an_ordinary_failure \
  tests/test_slow_lane.py::test_a_shard_that_runs_out_of_its_own_time_is_a_failure_of_the_change_not_of_capacity \
  tests/test_slow_lane.py::test_a_shard_killed_for_memory_is_a_failure_of_the_change_not_of_capacity \
  tests/test_slow_lane.py::test_a_shard_that_ran_out_of_a_runtime_its_spot_wait_shortened_is_capacity_not_the_change \
  tests/test_slow_lane.py::test_a_setup_failure_is_retried_once_on_demand_then_is_a_setup_failure_not_capacity \
  tests/test_slow_lane.py::test_a_shard_that_ran_out_leaves_an_outcome_record_and_the_junit_of_what_ran \
  tests/test_gate_speed.py::test_a_lane_whose_change_ran_out_is_a_failure_and_is_not_run_here \
  tests/test_gate_speed.py::test_a_failing_checks_script_without_a_capacity_file_is_an_ordinary_failure \
  tests/test_gate_speed.py::test_outside_a_run_both_lanes_run_here_and_say_why \
  tests/test_test_lane_any_project.py::test_a_root_layout_repo_and_a_workspace_project_get_the_same_lane \
  tests/test_test_lane_any_project.py::test_the_launcher_sends_a_command_to_the_child_when_a_run_is_attached \
  tests/test_test_lane_any_project.py::test_a_command_the_child_could_not_place_is_capacity_and_never_runs_here \
  tests/test_test_lane_any_project.py::test_every_ending_of_a_command_is_the_one_a_shard_has \
  tests/test_slow_lane.py::test_the_lane_task_is_a_raw_container_registered_in_its_own_environment
```

## Measured on this cluster

Measured on 2026-11-12/13 (the earlier rows; re-measured below) against the engine repository (7590 tests, 8180 after a later merge of main), from a child action of a
factory run on Union, with `AGENT_FACTORY_LANE_CPUS` set to the size under test. Nothing was skipped: the
merged junit holds every test of both lanes.

| Run | Limit | Wall | Outcome |
|---|---|---|---|
| Gate head run, 4-CPU shards | 1200 s | 531 s | PASS, 7586 cases; slow-lane shards 107, 162 and 336 s |
| Gate head run, 2-CPU shards | 1200 s | 683 s | PASS, 7587 cases; fast lane 4 shards of 197-286 s at about 1.5 GiB each, slow lane 6 shards, the longest (the Terraform benchmark file) 544 s on-demand |
| Gate head run, 2-CPU shards, one deliberately failing test added | 1200 s | 660 s | FAIL, and the junit names the failing test |
| Supervisor's `checks.sh` step running `factory-improvement/checks.sh`, 2-CPU shards | 900 s | 537 s | PASS, 7594 cases; lanes on the run's image, longest slow shard 413 s of pytest |
| Supervisor's `checks.sh` step, 4-CPU shards, after the round-1 fixes | 900 s | 498 s | PASS, 8176 cases, no capacity outcome |
| Gate head run, 2-CPU shards, one deliberately failing test, after the round-1 fixes | 1200 s | 581 s | FAIL, 8177 cases; the junit names the one failing test, no capacity outcome |
| Supervisor's `checks.sh` step, 2-CPU shards, after the round-2 fixes | 900 s | 550 s | PASS, 8194 cases, 9 shards all on spot, no capacity outcome |
| Supervisor's `checks.sh` step, 2-CPU shards, spot nearly empty (8 of 10 shards found none), after the round-3 fixes | 900 s | 546 s | PASS, no capacity outcome; the Terraform shard went on-demand first and ran 503 s |
| Supervisor's `checks.sh` step, 2-CPU shards, spot nearly empty (7 of 9 shards found none), Terraform file split by test, after the round-4 fixes | 900 s | 565 s | PASS, no capacity outcome; all 35 Terraform cases ran, longest slow shard 347 s on spot, both verdicts at the end of the output |
| Gate head run, 64-CPU shards (no node that size) | 1200 s | 342 s | `no capacity` for both lanes, 0 tests run on the session pod |

Re-measured on 2026-11-14 at commit `6f68aa4` (the round-1 fixes of agent-workspace#17: no lane runs on the
session pod while a run is attached), all from run `small-node-checks-w-2-0-cers88` on this cluster. Each row
names the child actions of its longest shard in each lane; the junit of every passing row holds all 8228
cases of both lanes (fast 8067, slow 161).

| Run (commit `6f68aa4`) | Limit | Wall | Outcome |
|---|---|---|---|
| Supervisor's `checks.sh` step, 2-CPU shards | 900 s | 682 s | PASS, 8228 cases, no capacity outcome; fast 5 shards (longest 255 s, `2kwxmjmyjm5yii7hfjgsbk088`), slow 6 shards (longest 283 s, `eckj3z34ohgw7mxipt9piqhdb`), most on-demand after spot found none; then ruff, docstring rules and mypy passed |
| Supervisor's `checks.sh` step, 4-CPU shards | 900 s | 312 s | PASS, 8228 cases, all 5 shards on spot (longest 258 s, `6jlqvaiktz1dx1wljnvsvu5gw`) |
| Gate head run, 2-CPU shards | 1200 s | 1009 s | PASS, 8228 cases, 11 shards all on-demand after 2 min on spot; the Terraform file whole in one shard, 702 s of pytest (`37bqzyvlg4480yuyagg4tg2yt`): the gate's 960 s child leaves it under the split threshold |
| Gate head run, 4-CPU shards | 1200 s | 216 s | PASS, 8228 cases, 6 shards all on spot (longest 183 s, `6yf01s843ne11tq3toze7z0fp`) |
| Gate head run, 2-CPU shards, one deliberately failing test added (local commit, not pushed) | 1200 s | 410 s | FAIL, 8229 cases; the junit names `tests.test_zz_demo_failure::test_a_deliberately_failing_test_for_the_demo`, the only failure; no capacity outcome |
| Gate head run, 64-CPU shards (no node that size) | 1200 s | 327 s | `no capacity` for both lanes after 2 min spot + 3 min on-demand each; 0 tests run on the session pod |

Re-measured on 2026-11-14 at commit `80c25bc`: the round-1 fixes, `--no-local` on the deployed launcher, the
wording of a short retry after a lost child, and the `AGENT_FACTORY_LANE_RUN_HERE` opt-in (unset here). All rows
are from run `small-node-checks-w-2-0-cers88`. Each row names the child action of its longest shard. The junit of
every passing row holds all 8261 cases of both lanes.

| Run (commit `80c25bc`) | Limit | Wall | Outcome |
|---|---|---|---|
| Supervisor's `checks.sh` step, 2-CPU shards | 900 s | 425 s | PASS, 8261 cases, no capacity outcome; one lane's 3 shards on spot, the other's 6 on-demand after spot found none (longest 327 s, `aymg6vesga77p7d9rf8fdszpy`); then ruff, docstring rules and mypy passed |
| Supervisor's `checks.sh` step, 4-CPU shards | 900 s | 434 s | PASS, 8261 cases, all 5 shards on spot (longest 383 s, `3dydi7njf3d4rh90gcl0mivq4`) |
| Gate head run, 2-CPU shards | 1200 s | 683 s | PASS, 8261 cases, 9 shards, most on-demand after spot found none (longest 502 s, `339cz4y9dqv2pzpj5fn5qp0su`) |
| Gate head run, 4-CPU shards | 1200 s | 237 s | PASS, 8261 cases, 5 shards all on spot (longest 202 s, `af84m1ld0310tlat1te6i3i4s`) |
| Gate head run, 2-CPU shards, one deliberately failing test added (local commit, not pushed) | 1200 s | 302 s | FAIL, 8262 cases, no capacity outcome. The failing shard's verdict line: `shard 1/3 (spot, child action 9ae4f061ia51enxk3jhkhtopl): exit 1: 1 failed, 2932 passed` / `FAILED tests/test_zz_demo_failure.py::test_a_deliberately_failing_test_for_the_demo`, the only failure in the junit. Longest shard: `shard 2/6 spot child action 2e6k0t5wphj46fkd238hiuqw1 289s code 0`. The failing shard's action shows `SUCCEEDED` on the cluster by design: the shard script hands pytest's code back in its output, not as the task's exit |
| Gate head run, 64-CPU shards (no node that size) | 1200 s | 327 s | `no capacity` for both lanes after 2 min spot + 3 min on-demand each, all four children `TIMED_OUT` (`MaxQueuedTimeExceededError`): first lane spot `29uxltlp41ai3dou71txmzhxv`, on-demand `c1xj4256zyguil5l81cipe6x7`; second lane spot `b2h0n4h8vooafckcmszshlt9a`, on-demand `3z3qktq380n3e7e3vz84xrbgg`; 0 tests run on the session pod |

At `1d528d1` the same six rows passed too: the `checks.sh` step at 2 and 4 CPUs took 310 s and 378 s, the gate
at 2 and 4 CPUs 497 s and 345 s. The failing-test run failed in 410 s, and the 64-CPU run reported `no capacity`
after 329 s.

Three earlier 2-CPU `checks.sh` runs at `b0630eb`, `48c256e` and `1d528d1` ended `no capacity` (exit 69),
and none ran tests on the session pod:

- One on-demand retry was not placed in time.
- One spot child was lost (`OutputUploadFailed`), which led to the `1d528d1` wording fix.
- One on-demand retry was placed in 4 s on a node about twice as slow as its siblings. It ran 393 s of pytest
  against their 201 s, then timed out at the 420 s left after the spot wait (`93wb2sbg8a3waklatw1wbl8ma`).

The `4-CPU` step at `b0630eb` failed only on `ruff format`, fixed in `9c15b08`.

Re-measured on 2026-11-14 at commit `c0926fa` (code unchanged since `80c25bc`) by `ops/lane_evidence.py`, in
run `small-node-checks-w-3-0-2gj89m`. Each row's evidence file, committed under
[test-lanes-evidence/](test-lanes-evidence/), holds the commit, the wall time, the outcome, the junit's case
count and failures, any capacity reason, whether anything ran on the session pod, the child actions launched,
and every lane verdict and shard line. Anyone in a run can redo a row with
`python3 ops/lane_evidence.py <scenario>` from this folder: it clones the commit into a scratch directory and
runs the supervisor's `_run_declared_checks` or the gate's `_run_head_pytest` exactly as the pre-review does.

| Scenario (evidence file) | Limit | Wall | Outcome |
|---|---|---|---|
| `checks-2cpu`: supervisor's `checks.sh` step, 2-CPU shards | 900 s | 391 s | PASS, 8261 cases, all shards on spot, no capacity outcome, nothing run on the session pod; ruff, docstring rules and mypy passed |
| `checks-4cpu`: supervisor's `checks.sh` step, 4-CPU shards | 900 s | 365 s | PASS, 8261 cases, all shards on spot, no capacity outcome |
| `gate-2cpu`: gate head run, 2-CPU shards | 1200 s | 584 s | PASS, 8261 cases, some shards on-demand after spot found none |
| `gate-4cpu`: gate head run, 4-CPU shards | 1200 s | 327 s | PASS, 8261 cases, all shards on spot |
| `gate-2cpu-failing`: gate head run, 2-CPU shards, one failing test committed in the scratch clone | 1200 s | 285 s | FAIL (exit 1), 8262 cases, 1 failure: `tests.test_zz_demo_failure::test_a_deliberately_failing_test_for_the_demo`; no capacity outcome |
| `gate-64cpu`: gate head run, 64-CPU shards (no node that size) | 1200 s | 331 s | `NO_CAPACITY` for both lanes after 2 min spot + 3 min on-demand each, 4 child actions launched and none placed, 0 junit cases, nothing run on the session pod |

After `tests/test_lane_evidence.py` was added, a 2-CPU `checks.sh` step at `b9a2d19` failed: one fast shard
stopped at 98% of its tests until its 600 s ran out (`4143p334x9xa3deyhonzojydb`, reported as the change, not
capacity, and nothing ran on the session pod). The new file was the only change in that shard, and one of its
tests ran a nested pytest under the xdist worker; that test now checks the failing test in-process, and its git
calls get no stdin. At `bd8255d` the `checks.sh` step then passed at 2 CPUs in 412 s and at 4 CPUs in 344 s, 8269
cases each (the 8261 above and the 8 new tests), no capacity outcome.

The long pole at 2 CPUs is `tests/test_bench_task_tf_bucket.py` (about 830 test-seconds, which its own cap of
two concurrent Terraform checks per container keeps from going faster with more workers). Kept whole it was one
shard of 410-540 s in its child: it fit the 600 s spot runtime `checks.sh`'s budget leaves (the budget's clock
starts before the upload, and `runtime_s` rounds down to whole minutes after taking off the 120 s queue bound;
the live task names `test-lane-600s` confirm it) but not reliably the 420 s left on-demand after a spot wait,
nor, once it had grown, the 540 s on-demand first. It now weighs more than 85% of the 600 s any `checks.sh`
child can have, so it runs as two parts of about 415 test-seconds in different shards, each under the 480 s
left after a full spot wait, and each tries spot first.

Pre-review round 4 failed with exit 1 and no visible cause: with spot nearly empty the Terraform shard went
on-demand first in `test-lane-540s-on-demand` and was `TIMED_OUT` at 540 s, its first attempt, so the lane
reported the change (exit 1) correctly, but the slow lane's lines had scrolled out of the supervisor's
12000-byte tail behind the fast lane's output. Its recorded duration had grown to 831 test-seconds. Hence
the two changes above: the verdict comes last, and a file that could outrun the longest child is split
rather than left to time out.
