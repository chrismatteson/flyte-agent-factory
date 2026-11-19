# Process reaping in the factory's pods

Issue #34: a slot's PID 1 did not collect orphaned processes, so zombies piled up over a long session, every scan of
`/proc` got slower, and a timing-sensitive engine test failed for a reason unrelated to the change under test.

## What PID 1 was, and why it never reaped

A Flyte task pod's container command is `a0 ...` (`flyte._bin.runtime:main`, see `task_serde._get_urun_container`:
`command=[]`, `args=["a0", ...]`), and the factory images set no `ENTRYPOINT`. So PID 1 was the `a0` Python process,
which waits only for the children it started itself. Any process whose parent exits is re-parented to PID 1 and,
once it exits, stays a zombie for the life of the pod.

| Pod | PID 1 before | PID 1 now |
|---|---|---|
| Slot (`agent.project`), sub-agent, setup, factory, deployer, webhook, MCP, bench, loop, reconcile, child actions on an `agent_image` | `a0` | `tini -s -- a0 ...` |
| Bench check (`_bench_check`, which builds its own image with `with_init`) | `a0` | `tini -s -- a0 ...` |
| Test lane (`_slow_lane` `ContainerTask`) | `bash -c <lane script>` | `tini -s -- bash -c <lane script>` |
| Browser app (`browser.py` `AppEnvironment`) | `/bin/bash /opt/browser/start.sh` | `tini -s -- /bin/bash /opt/browser/start.sh` |

Flyte's own entrypoint (`a0`) does not reap, and neither does the actor bridge of a reusable container: both wait
only for the processes they started. The engine runs no reusable containers (`agent.py`: the actor bridge is not
in the image), so no factory pod has the actor bridge as PID 1. If one ever does, tini runs with `-s`
(`PR_SET_CHILD_SUBREAPER`), so it still collects every orphan of the task's process tree.

## The fix

- `_images.with_init` installs Debian's `tini` package and sets the image env `_F_EXEC_WRAPPER="tini -s --"`. The
  flyte SDK's runtime (`flyte/_bin/runtime.py::_maybe_reexec_under_wrapper`, flyte 2.13.x) re-execs `a0` under the
  command in that variable; exec keeps the PID, so tini becomes PID 1 and `a0` its child. tini reaps every orphan
  and forwards signals to `a0`. Both `agent_image` and `browser_image` get it before `run_as_flyte`, so every image
  still ends as the unprivileged `flyte` user (#30/#32). tini needs no privilege.
- The test lane and browser app set their container command themselves, so it starts with the same tini command.
- It works the same on open-source Flyte: it is an apt package, an env variable the open-source SDK reads, and a
  container command. Nothing is Union-specific.
- A test (`tests/test_reaping.py`) pins the SDK hook, so an SDK upgrade that drops `_F_EXEC_WRAPPER` fails a test
  instead of silently bringing the zombies back.

**One behaviour change.** A process that is PID 1 ignores any signal it has no handler for, so `a0` used to ignore
the kubelet's SIGTERM and run on until the SIGKILL at the end of the grace period. Under tini, `a0` is not PID 1 and
SIGTERM ends it at once. Neither the engine nor `a0` handles SIGTERM, so the pod was always going to be killed; it
now stops sooner. The browser app's `personal.py` does handle SIGTERM, and now receives it through tini.

## Deploying this change

The test lane and the browser apps name `tini` in their container command, so the image they run on must carry
it. On an image built before this change, the container cannot start: the command is not found (locally,
`subprocess.run(_slow_lane.LANE_COMMAND[:3] + ["true"])` without tini on PATH raises
`FileNotFoundError: [Errno 2] No such file or directory: 'tini'`). There is deliberately no fallback to a bare
`bash`: it would hide a missing init, which is the defect this change fixes.

- **The lane.** Its command comes from the engine that launches it (`_gate_tests.write_lane_launcher` runs the
  launching engine's own `_slow_lane`), and its image is the run's own (`_gate_tests._run_image`), the image that
  engine was deployed with. `agent_image()` gained the tini layer, so its hash changes and a deploy rebuilds it
  with the code: after a deploy the two match. They differ only where an engine with this change launches lanes
  on an image built before it, for example a long-lived slot started before the deploy that then runs the new
  engine, or a `checks.sh` that sets `FLYTE_INTERNAL_IMAGE` to an older image. Every lane shard then fails at
  container start. Relaunch the slot (or point the variable at a current image).
- **The browser apps.** `deploy_personal_browser` builds `browser_image()` with the app, so redeploying the app
  is enough; an app left running from before the deploy keeps its old image and old command, and is unaffected.
- **Order.** Deploy the engine (image and code together), then redeploy the personal browser apps. Runs already
  in flight keep the image and code they started with, so they are unaffected; they get tini when relaunched.

## Where the orphans came from (rates)

This project's own slot pod is a measurement: it was started from an image built before this change, so `a0` is
its PID 1. On 2026-11-18, after about 17 hours (PID 1's start time against `/proc/uptime`), it held 2995 zombies
whose parent is PID 1, about 2.9 a minute, by command name: `sh` 2686, `tee` 134, `sleep` 109, `python3` 37,
`git` 16, `timeout` 8, `python` 3, `sed` 1, `bash` 1. The sources, each counted under a subreaper that does not
wait (the control harness of `tests/test_reaping.py`) or in a slot pod on 2026-11-17:

- **Background jobs from the Bash tool.** Claude Code runs each Bash-tool command in a shell that exits when the
  command returns. Anything it started in the background (`cmd &`, `(cmd &)`, `nohup ... &`, a dev server, a
  `sleep`-based wait) is re-parented to PID 1 and becomes one zombie when it exits. Two `sleep 1 &` commands
  left exactly two zombies under PID 1. This is one zombie per backgrounded process, which is the main source in a
  long session: an agent that polls with background jobs leaves several per minute. ~3200 zombies over the reported
  ten hours is about 5 per minute.
- **Process-group kills.** `_gate_base._kill_group` (a gate command past its limit) and `_checkpoint._reap` (a stuck
  checkpoint push) send SIGKILL to the whole group and wait only for the direct child; each grandchild that was
  running becomes a zombie under PID 1. `tests/test_gates.py::test_a_timed_out_command_is_killed_with_everything_it_started`
  already accepted that state (`"Z"` in its `stat`). Measured: a gate command `sh -c "sleep 100 & sleep 100 &
  sleep 100 & wait"` timed out by `_gate_base._run` left 3 zombies, one per running grandchild. It happens only on
  a timeout: this session's gate logs record 0 timed-out commands, so it contributed none of the 2995.
- **Daemonising tools** run by the engine or the agent leave one zombie per detach. Measured: `git gc --auto` with
  `gc.autoDetach=true`, triggered by three packs over `gc.autoPackLimit=1`, left 1 zombie. Git triggers it at most
  once per command that crosses the threshold (6700 loose objects or 50 packs by default), so it is a small share
  of the 16 `git` zombies above at most.

The callers are not changed: an init that reaps is the general fix, and the callers above are doing nothing wrong
for a normal Unix process tree.

## Effects measured

- **The `/proc` scan.** The load clock (`_load_clock.sample_group`) reads `/proc/<pid>/stat` for every process in
  the pod. On this pod it took 4-9 ms with a clean table, and 0.64 s (zombies in another process group) to 1.17 s
  (zombies in the scanned group) with 3200 zombies. The clock takes its first sample before it starts polling, so
  `test_a_command_is_judged_by_the_load_clock_with_its_own_limit`, whose command sleeps 0.3 s, can finish before the
  first poll and record no limit, which was the reported failure. Whether it fails also depends on how fast the node
  reads `/proc`: on the probe's node below it still passed with 3200 zombies.
- **Whether the load clock should scan all of `/proc`.** Yes, and it is unchanged: load is a property of the pod
  (every process competes for the same CPUs), so the clock samples the whole table, and with tini reaping the
  table holds only live processes. Narrowing the scan to the command's group would hide exactly the burden #34 is
  about; the gate instead polls before its first wait (below), so a slow scan cannot cost a command its verdict.
- **The pids limit.** A zombie keeps its PID and counts in the cgroup's `pids.current`. Twenty background `sleep`s
  raised `pids.current` from 62 to 87, with 22 zombies under PID 1. The slot pod's `pids.max` was 18751, so a long
  enough session would eventually be unable to start any process (`fork: Resource temporarily unavailable`).

## The timing-sensitive gate

Reaping removes the zombies, but a pod may still have a large process table (an old pod, or many live processes),
so `_gate_base._run` now polls the load clock before it first waits for the command. A command is then judged at
least once even when the clock's first scan of `/proc` outlasts it, and
`test_a_command_is_judged_by_the_load_clock_with_its_own_limit` gives the same verdict in a pod with 1500 zombies
as in a fresh one (it failed there before, under a parallel test run). `tests/test_reaping.py` pins this with a
clock whose first scan takes 0.6 s.

## Proof in a real pod

`ops/probes/reaping.py` runs one task on the slot image (`agent_image()`, with the `with_init` layer). It starts
3200 orphans the way the Bash tool does (`sh -c "sleep 0.2 &"`), counts zombies, times one scan of `/proc` by the load
clock, and runs a 0.3 s gate command under the load clock as
`test_a_command_is_judged_by_the_load_clock_with_its_own_limit` does. Then, in the same pod, it runs
`tests/test_load_clock.py` and that gate test with pytest. `--control` clears `_F_EXEC_WRAPPER` for the task, so `a0`
is PID 1 again.

Results on 2026-11-18, from `uv run python ops/probes/reaping.py --in-run` and `... --in-run --control`, submitted as
child actions of run `slot-reap-zombies-101-0-d6gln6` (Union, spot CPU node, uid 1000):

| Run | Action | PID 1 | Zombies after 3200 orphans | One `/proc` scan | Gate command | pytest in the pod |
|---|---|---|---|---|---|---|
| Slot image as built | `d63xggiqetc78t7hlexvp1jay` | `tini` | 0 | 0.7 ms | polled with its limit, passed | 16 passed |
| `--control` (no wrapper) | `d1tcmu254b1qrdiru5mo6jtjp` | `a0` | 3200 | 313.8 ms | polled with its limit, passed | 16 passed |

The lines each printed, verbatim:

```
PASS {"uid": 1000, "before": "PID 1 tini, 2 processes, 0 zombies", "after": "PID 1 tini, 2 processes, 0 zombies", "scan_ms": 0.7, "gate": "result (0, ''), polled with [30]", "tests": "16 passed, 6 warnings in 2.31s"}
FAIL {"uid": 1000, "before": "PID 1 a0, 1 processes, 0 zombies", "after": "PID 1 a0, 3201 processes, 3200 zombies", "scan_ms": 313.8, "gate": "result (0, ''), polled with [30]", "tests": "16 passed, 6 warnings in 2.31s"}
```

The control prints `FAIL` because its zombies remain, as expected. Its tests pass on this node too: how fast the node
reads `/proc` decides whether 3200 zombies fail the load-clock test, while the reaping does not depend on it. The
scan, 450 times slower with the zombies, is the cost the reaping removes.

## Visibility

`_proc_health.snapshot()` reads the process table: what PID 1 is, how many processes, how many zombies.

- The slot writes it to its timeline when it starts (a 🧹 row), so the run report shows what PID 1 is, and writes
  a ⚠️ row once if the zombie count reaches `ZOMBIES_WORTH_NAMING` (50) while it runs.
- The pre-review row adds it when the pod has that many zombies or PID 1 is not an init.
- A gate command that times out names the zombie count in its message when the pod has that many, so a slow pod is
  told from a slow test.
