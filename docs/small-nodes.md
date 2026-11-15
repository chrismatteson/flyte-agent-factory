# Small nodes

The factory runs on one 4 CPU / 16 GiB node: a single GKE `e2-standard-4` on an OSS Flyte cluster, the always-on
parts plus one live project, with room left for kube-system. This page has the arithmetic, what changed to make it
true, and how to raise it on a bigger cluster. Every number comes from `src/flyte_agent_factory/_resources.py`; the
tables below are its output.

## Minimum node

One node with 4 CPU, 16 GiB of memory and the default 100 GB GKE boot disk. The factory plans on a budget smaller than
the node:

| | Node | Allocatable | Factory budget |
|---|---|---|---|
| CPU | 4 | about 3.92 | 3 |
| Memory | 16 GiB | about 13 GiB | 12 GiB |
| Ephemeral disk | 100 GB | | 50 GiB |

GKE reserves part of an `e2-standard-4` for the kubelet and the OS, which leaves about 3.92 CPU and 13 GiB allocatable.
kube-system's DaemonSets (networking, logging, metrics) request part of that again (read the real figure with
`kubectl describe node`), so the factory budget is 3 CPU and 12 GiB of *requests*. The disk budget leaves half the boot disk for the OS, the images and the logs.

The budget adds up requests, because requests are what the scheduler places. Limits are what a pod may burst to; they
are not summed and a limit above the node's size is harmless (it never schedules anything).

## The arithmetic

Two scenarios, for the default profile (`profiles/example.toml`; regenerate with
`PYTHONPATH=src python -m flyte_agent_factory._resources --profile <name>`):

- **idle**: every always-on app the profile enables, and every scheduled tick the profile enables running at the same
  time (the worst case): `factory` (sweep and gardening), `factory-deployer`, `factory-reconcile`, `factory-loop`,
  `factory-issues`, `factory-bench`, and one `gardening-scan`.
- **one project live**: idle, plus one slot pod and its children: one test-lane shard, or one sub-agent, whichever
  asks for more. The checks' two lanes run as shards submitted at once ([test-lanes.md](test-lanes.md)); one shard
  must fit beside the slot and the rest wait for room (see [Test-lane shards](#test-lane-shards)). Further sub-agents
  wait likewise.

| Component | Environment | CPU request / limit | Memory request / limit | Disk request |
|---|---|---|---|---|
| **always on** | | | | |
| mcp | factory-mcp | none | none | none |
| ticks | factory | 100m / 1 | 256Mi / 1Gi | none |
| deployer | factory-deployer | 250m / 2 | 512Mi / 2Gi | none |
| ticks | factory-reconcile | 100m / 1 | 256Mi / 1Gi | none |
| scan | gardening-scan | 250m / 2 | 512Mi / 4Gi | none |
| **one live project** | | | | |
| slot | slot | 500m / 4 | 1536Mi / 8Gi | 20Gi |
| lanes | test-lanes | 1500m / 2 | 3Gi / 5Gi | none |

| Scenario | CPU | Memory | Ephemeral disk |
|---|---|---|---|
| idle | 0.70 | 1.50 Gi | 0 Gi |
| one project live | 2.70 | 6.00 Gi | 20 Gi |
| budget | 3.00 | 12.00 Gi | 50 Gi |

Lane shards that fit at once beside one live slot: 1.

With every optional component on (webhook with issue intake, board, bench, loop, one personal browser) the totals are
1.00 CPU / 2.50 Gi idle and 3.00 CPU / 7.00 Gi with one project live, still with room for one shard at a time; `tests/test_resources.py` asserts both profiles
fit.

Decisions in the count:

- **Setup tasks** (`factory-setup`: `gcp_bootstrap`, `codex_bootstrap_login`) are not counted. The owner runs them once
  while installing, not as part of a project's run, and at 250m / 256Mi one fits in the headroom the totals leave.
- **Gardening scans** run one at a time (`_factory.scan_targets` holds a semaphore of 1): a weekly gardening run with
  several targets would otherwise start a pod per target at once. Each scan waits its turn, so a run with many targets
  takes longer.
- **Short tasks the MCP app starts** (status and queue calls on the `factory` environment, 100m / 256Mi each) are
  not counted one by one: they last seconds, and the gap between the budget and allocatable covers a few at once.
- **A second project** is not counted: the budget is for one live project. Each further one adds a slot pod and its
  children (2 CPU / 4.5 Gi with one lane shard running): the cap in the profile (`cap`, `[concurrency]`) should stay at
  1 on this node, or raise the resources for a bigger one.
- **The benchmark** (`[bench]`) is heavy and optional. `factory-bench` is a polling tick and is counted;
  `factory-bench-check` (hidden checks with pytest, scikit-learn and Terraform) and the case slots it runs are not in the
  budget. Run a benchmark on a bigger cluster.
- **Not part of the footprint**: the probe scripts in `src/*.py` (outside the package), and `voice.py` (a GPU
  environment, optional).
- **The shared browser app** (`claude-browser`, deployed by hand with `flyte serve`) uses the same `browser` component
  but is not enabled by a profile, so the budget leaves it out; each personal browser identity is counted.

## What changed

| Component | Environments | Was | Now (request / limit) | Why |
|---|---|---|---|---|
| `slot` | the slot's `agent.py` env | 1-4 CPU, 2-12 Gi, disk 120Gi | 500m-4 CPU, 1536Mi-8Gi, disk 20Gi | Measured 0.27 CPU average, ~0.5 GiB anonymous memory (Evidence). 120Gi could not be placed on a 100 GB boot disk. |
| `subagent` | the slot's sub-agent env | 1-4 CPU, 2-8 Gi, disk 50Gi | 500m-4 CPU, 1536Mi-8Gi, disk 10Gi | As the slot; the workspace arrives as an input. |
| `setup` | `factory-setup` | 1 CPU, 2Gi (request only) | 250m-1 CPU, 256Mi-1Gi | A few API calls. |
| `mcp` | `factory-mcp` app | SDK default: 1 CPU, 512Mi | none (BestEffort) | 104 MiB idle. |
| `webhook` | `factory-webhook` app | 1 CPU, 512Mi | none (BestEffort) | A FastAPI process, ~80 MiB. |
| `browser` | `claude-browser`, the personal browsers | 1-4 CPU, 2-8 Gi, disk 20Gi, shm 2Gi | no CPU request, 256Mi-4Gi, disk 10Gi, shm 2Gi | Chromium is unbounded, so it keeps a limit, and a limit forces a request. |
| `ticks` | `factory`, `factory-reconcile`, `factory-loop`, `factory-issues`, `factory-bench` | 1 CPU, 1Gi (`factory-bench`: 2Gi), request only | 100m-1 CPU, 256Mi-1Gi | A tick is the Flyte runtime plus one import and a few HTTP calls. |
| `deployer` | `factory-deployer` | 1 CPU, 2Gi | 250m-2 CPU, 512Mi-2Gi | A tick, plus subprocesses that import the agent and submit an image build. |
| `scan` | `gardening-scan` | 2 CPU, 4Gi | 250m-2 CPU, 512Mi-4Gi | One scan at a time. |
| `lanes` | each test-lane shard (`_slow_lane`, `_lane_shards`) | one lane of 8 CPU, 8Gi (request only), before #12 sharded it | per shard of `AGENT_FACTORY_LANE_CPUS` (default 2): 3/4 of the CPUs to all of them, 1.5 GiB per CPU (rounded up) to 2 per CPU plus 1 GiB; 1500m-2 CPU, 3Gi-5Gi by default | #12's shard sizing, unchanged; `[resources.lanes]` applies on top. The budget counts one shard. |
| `bench` | `factory-bench-check` | 1 CPU, 2Gi | unchanged | Heavy, optional, not in the budget. |

A single value in a Flyte `Resources` is a request only, with no limit. That is how the old values for `factory`,
`factory-deployer` and the lanes asked for their whole size up front; the new ones ask for a small request and may
burst to a limit.

The slot's disk is a request only too, so exceeding it is not an eviction by itself; it only decides where the pod can
be placed. The LJSpeech eviction that once raised it from 30Gi was node disk pressure.

## Test-lane shards

The checks' two pytest lanes run as shards, each a child action of `AGENT_FACTORY_LANE_CPUS` CPUs (default 2), sized
by `_resources.lane_shard(cpus)`: a CPU request of 3/4 of `cpus` with `cpus` as the limit (a request of a node's whole
size never places on a node of that size), and a memory request of 1.5 GiB per CPU rounded up with 2 GiB per CPU plus
1 as the limit. `_slow_lane.make_lane_resources(cpus)` returns that, and a profile's `[resources.lanes]` table applies
on top of a shard of any size: a key the table sets (`cpu`, `memory`, `disk`) replaces the shard's at every size, a key
it leaves out keeps the formula's. `PYTHONPATH=src python -m flyte_agent_factory._resources` shows the result for one
default-sized shard.

The launcher submits every shard of both lanes at once, but the budget counts **one** shard beside the slot, not every
shard: a shard that finds no room waits for one (on spot up to 2 minutes, then on-demand up to 3) before it is a
capacity outcome, which is not a failure of the change. The budget's last line, `Lane shards that fit at once beside
one live slot`, is how many run side by side; on the default 4-CPU node it is 1:

    3.00 CPU budget - 0.70 idle - 0.50 slot = 1.80 CPU free; 1.80 // 1.50 per shard = 1
    12.00 GiB budget - 1.50 idle - 1.50 slot = 9.00 GiB free; 9.00 // 3 per shard = 3

so CPU binds. With every optional component on, idle is 1.00 CPU and 1.50 CPU is free: still one shard. Smaller
shards (`AGENT_FACTORY_LANE_CPUS=1`, 750m each) fit two at a time on the same node; a bigger cluster runs all of
them at once.

The lane launcher (`_slow_lane`) may import only test-harness modules, so it reads the profile text a pod carries
(`AGENT_FACTORY_PROFILE_TOML`) through `_resources` and `_profile_resources`, which are two of them; run from a
laptop, with no such text, it uses the defaults.

## An app with no requests

`factory-mcp` and `factory-webhook` ask for nothing. Flyte then writes no resources into the pod spec, and Kubernetes
makes the pod **BestEffort**. Two things can fill the gap, and the engine does neither:

- a namespace **LimitRange** with `default` and `defaultRequest` (check with
  `kubectl get limitrange -n <namespace>`; on Union and OSS Flyte alike the engine creates none);
- Knative's `config-defaults` (`revision-cpu-request` and the like), unset by default.

If the cluster has either, the app gets those values instead; set `[resources.mcp]` to override them.

**The trade-off.** A BestEffort pod has an OOM score adjustment of 1000, so under node memory pressure it is killed
and evicted first, before any pod with requests. That is what is wanted of `factory-mcp` and `factory-webhook`: they
restart in seconds, hold no state, and should not be what starves a slot. The browser is the opposite case: Chromium is
unbounded and must not take the node down, so it keeps a 4Gi limit. Kubernetes sets a container's request to its limit
when only a limit is given, so the browser also gets an explicit 256Mi request, or it would be placed as a 4Gi pod.

## Raising resources on a bigger cluster

Add a `[resources.<component>]` table to the profile (see `profiles/example.toml` and `profiles/README.md`). `cpu` and
`memory` are one quantity (a request) or `[request, limit]`; `disk` is one quantity (a request). A table replaces only
the keys it sets:

```toml
[resources.slot]
cpu = ["2", "8"]
memory = ["4Gi", "16Gi"]
disk = "50Gi"

[resources.mcp]          # give factory-mcp a guaranteed share instead of BestEffort
memory = ["256Mi", "512Mi"]
```

The profile's text goes into every pod, so a pod sees the same values the deploy used. A change takes effect on the next
deploy of that component. Unknown components or keys, quantities that do not parse (`"4 cores"`), non-positive ones and
a request above its limit are rejected when the profile is read.

`PYTHONPATH=src python -m flyte_agent_factory._resources --profile <name>` prints the budget for the new values; it is checked
against the 3 CPU / 12 GiB / 50 GiB budget only for the defaults, so on a bigger node read the totals yourself.

`ops/deploy_factory.py` deploys `--ref` (default `origin/main`) from a clean worktree of that commit, not the working
tree: commit and push a profile or code change before deploying it. See [setup.md](setup.md#deploying).

## Evidence

Measured on a 16-core Xeon 8124M, fresh process, median of 3 (RSS / CPU time):

| Import | Peak RSS | CPU |
|---|---|---|
| `python -c pass` | 13.9 MiB | |
| `import flyte` | 26.5 MiB | 0.24 s |
| `_factory` | 71.7 MiB | 0.85 s |
| `_reconcile` | 71.4 MiB | 0.85 s |
| `_deployer` | 45.8 MiB | 0.49 s |
| `_loop_flow` | 72.6 MiB | 0.86 s |
| `_bench_flow` | 72.6 MiB | 0.88 s |
| `_factory_mcp` | 87.9 MiB | 1.34 s |
| `_webhook` | 78.9 MiB | 1.05 s |
| `browser` | 57.0 MiB | 0.67 s |
| `agent` | 75.6 MiB | 0.95 s |

- **factory-mcp**, served locally with uvicorn and idle for 20 s: RSS equals its peak, 104 MiB, and 1.47 s of CPU in
  total, all of it startup.
- **A live slot pod** on Union (this project's own, with a 4-CPU and a 12Gi limit), over the first ~10 minutes of an
  active Claude Code session: 172 CPU-seconds in 626 s of wall time (about 0.27 CPU on average, about 0.13 over the
  first 341 s). The cgroup's `memory.peak` was 3.47 GiB, of which 1.6 GiB was reclaimable page cache from restoring
  the workspace; anonymous memory was about 510 MiB. The Flyte runtime process (`a0`, which imports
  `flyte_agent_factory.agent`) peaked at 933 MiB (VmHWM) and the `claude` process at 417 MiB. The workspace (`$HOME`)
  was 2.1 GiB for a multi-project workspace; the engine's venv 452 MiB and its checkout 827 MiB.
- **Ticks**: a tick is the Flyte runtime, one module import and a few HTTP calls, so its floor is 100-300 MiB and it is
  CPU-bound only during the import (under 1.5 s). The deployer also runs subprocesses that import the agent and submit
  a remote image build, which is why it asks for more.

These are single measurements of one session and one machine, not a guarantee; the limits are the margin.
