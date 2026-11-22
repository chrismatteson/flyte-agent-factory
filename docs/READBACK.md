# Readback: four-cpu-node (#15) re-applied on #12's sharded slow lane

This project is upstream main ee14bf2 (which has #12) with #15's whole change re-applied. It is meant to become
the new content of PR #15 (`four-cpu-node` on the owner's fork, squashed at 1af2181 on 83d2150).

## What was carried over unchanged

For every file #15 touches except the four that conflict with #12 and `_resources.py` / `tests/test_resources.py`,
the added and removed lines here are exactly #15's (compare `git diff 83d2150 1af2181 -- <file>` with this
project's diff from its import). That covers the small CPU requests with the old values as limits for every task and
app (`agent.py`, `_factory.py`, `_deploy.py`, `_deployer.py`, `_reconcile.py`, `_loop_flow.py`, `_bench_flow.py`,
`_bench_check.py`, `_factory_mcp.py`, `_webhook.py`, `browser.py`), the `[resources]` profile table
(`_profile.py`, `_profile_resources.py`, `profiles/`), `_tick_guard.py` with its tests, and the docs
(`small-nodes.md`, `triggers.md`, `setup.md`, `architecture.md`, `oss-flyte-compat.md`, `docs/README.md`).

## How the two designs were reconciled

**Shard sizing.** #12's formula moved from `_slow_lane.make_lane_resources` into `_resources.lane_shard(cpus)`,
unchanged: CPU request 3/4 of `cpus` and limit `cpus`; memory request ceil(1.5 x cpus) GiB and limit (2 x cpus + 1)
GiB. `make_lane_resources(cpus)` now returns `_resources.for_lane_shard(profile_text, cpus).to_flyte()`: without a
`[resources.lanes]` table that is exactly `lane_shard(cpus)`; with one, each key the table sets (`cpu`, `memory`,
`disk`) replaces the shard's at every shard size and keys it leaves out keep the formula. `_memory_limit`, which
out-of-memory messages name, reads the same spec. The launcher still imports only test-harness modules: it reads the
pod's `AGENT_FACTORY_PROFILE_TOML` text, not `_profile`.

`_resources.DEFAULTS["lanes"]` is no longer #15's single `750m / 8, 2Gi / 8Gi` lane but one default-sized shard,
`lane_shard(LANE_CPUS_DEFAULT)`, and `_slow_lane.CPUS_DEFAULT` is `_resources.LANE_CPUS_DEFAULT` (2), so the budget
counts the same shard the launcher submits.

**Budget.** #15 counted two lanes beside the slot (the checks' fast and slow lanes, 750m each). #12 runs each lane as
up to 8 shards submitted at once; a shard that finds no room waits (2 min spot, 3 min on-demand) and is then a
capacity outcome, not a failure. So `budget()` now counts **one shard** beside the slot (or one sub-agent, whichever
is larger), and `Budget.shards_at_once` reports how many shards fit together; the markdown prints it.

**Harness lists.** `_gates._TEST_HARNESS` keeps `_slow_lane.py` and `_lane_shards.py` and adds `_resources.py` and
`_profile_resources.py` (the launcher now imports them). `agent.py`'s `env.include` keeps `_lane_shards.py` and
`_slow_lane.py` and adds `_tick_guard.py`; `_resources` and `_profile_resources` ship because `agent.py` imports them
eagerly. `tests/test_gate_speed.py` checks both: every package module the launcher imports is harness (#12's test),
and every one ships in the slot (new).

## Budget arithmetic (defaults, one 4-CPU / 16 GiB node)

Budget: 3 CPU, 12 GiB of requests, 50 GiB ephemeral disk (allocatable on an `e2-standard-4` is about 3.92 CPU /
13 GiB; the difference is kube-system's and headroom). Output of
`PYTHONPATH=src python -m flyte_agent_factory._resources --profile example`:

| Line | CPU request | Memory request | Disk |
|---|---|---|---|
| factory-mcp | 0.10 | 0.25 Gi | 0 |
| factory tick | 0.10 | 0.25 Gi | 0 |
| factory-deployer | 0.25 | 0.50 Gi | 0 |
| factory-reconcile tick | 0.10 | 0.25 Gi | 0 |
| gardening-scan (one at a time) | 0.15 | 0.50 Gi | 0 |
| **idle** | **0.70** | **1.75 Gi** | **0** |
| slot | 0.50 | 1.50 Gi | 20 Gi |
| one 2-CPU lane shard | 1.50 | 3.00 Gi | 0 |
| **one project live** | **2.70** | **6.25 Gi** | **20 Gi** |

Shards at once beside the idle factory and one slot: CPU 3.00 - 0.70 - 0.50 = 1.80 free, 1.80 // 1.50 = 1; memory
12.00 - 1.75 - 1.50 = 8.75 free, 8.75 // 3 = 2. CPU binds: **1 shard at a time**; the others wait their turn.

With every optional component on (webhook with issue intake, board, loop, bench tick, one personal browser): idle
1.00 CPU / 2.75 Gi / 10 Gi, one project live **3.00 CPU / 7.25 Gi / 30 Gi**, exactly at the CPU budget, still 1
shard at a time. Both are asserted in `tests/test_resources.py`; the default table is asserted to appear verbatim in
`docs/small-nodes.md` (new test `test_the_docs_show_the_default_profiles_budget_as_it_prints`).

For comparison, #15's count with unsharded lanes was 2.70 CPU / 7.00 Gi (two 750m / 2Gi lanes); the shard requests
more CPU per child (1.5) but only one has to fit.

**Trade-off to know.** With one shard at a time on a 4-CPU node, a lane split into many shards runs them in turn,
and a shard that waits more than its five minutes of queue bounds is reported as capacity (#12's outcome, not a test
failure). `AGENT_FACTORY_LANE_CPUS=1` (750m / 2Gi shards) fits two at a time; a `[resources.lanes]` table can lower
the request further. Neither is the default, because #12 measured its defaults on this cluster.

## Tests adjusted where the designs meet

No test was removed, skipped, marked xfail or loosened. Three places changed because both sides assert the same thing
differently:

1. `tests/test_slow_lane.py` (#12), four assertions: a shard's CPU limit is now the string `"2"` (`"4"`, `str(cpus)`)
   instead of the integer. #15's `Spec` holds every quantity as a Kubernetes string, and #15's own tests assert
   string pairs such as `("2", "16")`; the values are identical and Flyte serialises both the same. Request and
   memory assertions are unchanged.
2. `tests/test_resources.py` (#15): `test_one_live_project_counts_a_slot_and_its_checks_two_lanes` became
   `test_one_live_project_counts_a_slot_and_one_lane_shard`, because #12 replaced the two lanes with shards; it now
   asserts the live set is the slot plus one shard equal to `lane_shard(2)` (1.5 CPU, 3 GiB). New beside it:
   `test_the_default_node_holds_at_least_one_shard_and_the_budget_says_how_many`.
3. `tests/test_resources.py::test_the_budget_prints_as_markdown` (#15) gained one assertion (the shards line).

One fix to the base, not to either design: `tests/test_cpu_budget.py::test_the_xdist_hook_answers_with_the_budget`
fails at ee14bf2 whenever it runs under `checks.sh`, because #12's `checks.sh` exports `PYTEST_XDIST_AUTO_NUM_WORKERS`
and the hook rightly returns `None` when that is set. The test now unsets the variable first (`monkeypatch.delenv`),
so it tests what it says in any environment; its assertion is unchanged.

Added: `test_the_profiles_lanes_table_applies_on_top_of_every_shard_size`,
`test_the_lane_default_shard_size_is_the_one_the_budget_counts` (test_slow_lane.py),
`test_the_slot_ships_every_package_module_the_slow_lane_launcher_imports` (test_gate_speed.py) and the docs test
above.

## How to check

From the project root (`projects/four-cpu-node-r2/` in the workspace):

    bash factory-improvement/checks.sh                         # fast lane and lint
    AGENT_FACTORY_RUN_SLOW=1 bash factory-improvement/checks.sh  # both lanes
