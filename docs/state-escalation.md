# State storage: measurements and when to go beyond the bucket

The factory's state ([state-store.md](state-store.md)) is on a git branch, in a bucket, or on a volume. This page
records what each costs under concurrent writers, and says which measurements, at which thresholds, would justify a
database (Postgres) backend later. **There is no Postgres backend now**, and nothing here builds one: the bucket
with conditional writes is the production default, and these thresholds are the agreed tripwire for revisiting that.
The bucket's setup is [terraform-factory-bucket.md](terraform-factory-bucket.md).

## Which backend

- **bucket** is the recommended production default: a dedicated bucket, compare-and-swap by conditional writes,
  no dependence on a git host's rate limits or round trip.
- **git** is the zero-setup trial: nothing to provision, but every write is a fetch and a push to the git host.
- **volume** is for open-source, single-node use: a directory and `flock`.

## The benchmark

`ops/state_bench.py` runs N writer threads against a backend for D seconds. Each writer loops over the factory's
mix: a marker (a plain write of a key only it writes), the dispatch lease (`_queue.take_lease`, then
`release_lease` if it got it) and a reserve read-modify-write (`transact` on one shared counter). Every call is
timed. The table's columns:

| Column | Meaning |
|---|---|
| calls, failures | calls made, and calls that raised (a retry budget used up, a git or S3 error) |
| lost updates | increments that returned but are missing from the final counter: always 0 unless the compare-and-swap is broken |
| p50 / p95 / max s | latency of one write call, retries and backoff included |
| lost races / write | refused conditional writes (bucket: `store.stats.lost_races`) or rejected pushes (git: `StateStore.lost_races`), per write that landed |
| objects / hour | state objects (bucket, volume) or commits (git) written per hour at the measured rate; each bucket write also puts one audit record, not counted here |
| list ms (keys) | one `list("")` after the run, and how many keys it returned |

```sh
PYTHONPATH=src uv run python ops/state_bench.py                       # defaults: 1, 4, 12 writers, 8 s each, ~2 minutes
PYTHONPATH=src uv run python ops/state_bench.py --backends git --git-delay-ms 150
```

Method and limits:

- Twelve writers is the number of slots. Writers are threads, each with its own store (own git cache, own S3
  client); the limits are the machine's CPU and the GIL, so the rates compare backends and are not a forecast.
- `git` is a local bare remote in a temporary directory. `--git-delay-ms` sleeps that long before every fetch and
  push, to **mimic** the round trip to a git host; it is a model, not a measurement of GitHub.
- `s3` is moto's `ThreadedMotoServer` on localhost: it enforces `If-None-Match` and `If-Match` like S3, but it is
  one Python process, so its latency says what the protocol costs and is not S3's (a real PUT is typically tens of
  milliseconds; add the network).
- `memory` is obstore's MemoryStore with its calls serialised (obstore 0.11 deadlocks when threads put and list at
  once, which `ops/state_bench.py` reproduces without any factory code); it is the floor for the bucket store's own
  logic. Use it for tests, never for state.
- No test or run here touches a real bucket.

Machine: AMD EPYC 7571, 4 vCPU, 15 GB, Linux 6.12, Python 3.12.15, git 2.39.5, obstore 0.11.1, moto 5.2.5.
Run on 2026-11-15, 8 s per row, retry budget of 200 in the benchmark (the engine's default is 25; rerun at 25 gave the same 0 failures / 0 lost updates).

### Results

Local backends, no injected delay:

| backend | writers | calls | failures | lost updates | p50 s | p95 s | max s | lost races / write | objects / hour | list ms (keys) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| git | 1 | 54 | 0 | 0 | 0.119 | 0.235 | 0.248 | 0.00 | 31,436 | 28.3 (19) |
| git | 4 | 54 | 0 | 0 | 0.158 | 3.990 | 9.411 | 0.54 | 26,074 | 28.8 (19) |
| git | 12 | 57 | 0 | 0 | 0.818 | 9.117 | 12.109 | 3.40 | 13,494 | 28.2 (20) |
| s3 (moto) | 1 | 174 | 0 | 0 | 0.039 | 0.083 | 0.089 | 0.00 | 104,232 | 35.4 (60) |
| s3 (moto) | 4 | 186 | 0 | 0 | 0.152 | 0.366 | 0.734 | 0.03 | 80,824 | 31.1 (64) |
| s3 (moto) | 12 | 165 | 0 | 0 | 0.352 | 1.633 | 8.134 | 0.70 | 48,364 | 28.6 (57) |
| memory | 1 | 2370 | 0 | 0 | 0.003 | 0.008 | 0.064 | 0.00 | 1,420,917 | 4.6 (792) |
| memory | 4 | 2280 | 0 | 0 | 0.011 | 0.038 | 0.377 | 0.02 | 1,000,026 | 5.5 (762) |
| memory | 12 | 2145 | 0 | 0 | 0.022 | 0.141 | 3.828 | 0.26 | 716,814 | 7.4 (717) |
| volume | 1 | 348 | 0 | 0 | 0.020 | 0.052 | 0.062 | 0.00 | 206,718 | 21.1 (118) |
| volume | 4 | 315 | 0 | 0 | 0.093 | 0.247 | 0.342 | 0.06 | 148,298 | 19.3 (107) |
| volume | 12 | 285 | 0 | 0 | 0.323 | 0.798 | 3.313 | 0.20 | 96,029 | 20.7 (97) |

Git with 150 ms before each fetch and push (a model of a remote git host):

| backend | writers | calls | failures | lost updates | p50 s | p95 s | max s | lost races / write | objects / hour | list ms (keys) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| git | 1 | 15 | 0 | 0 | 0.416 | 0.832 | 0.843 | 0.00 | 8,705 | 177.5 (6) |
| git | 4 | 33 | 0 | 0 | 1.027 | 2.564 | 11.907 | 1.15 | 9,320 | 175.8 (12) |
| git | 12 | 51 | 0 | 0 | 1.708 | 10.689 | 17.351 | 3.48 | 7,715 | 180.1 (18) |

What it shows:

- No failures and no lost updates anywhere: the compare-and-swap holds on every backend at 12 writers.
- Git serialises all writers on one ref: its total rate *falls* as writers are added (31k to 13k commits/hour
  locally, with 3.4 rejected pushes per write at 12 writers) and a write's p95 reaches 9 s. With a remote round
  trip the single-writer rate is already about 9k commits/hour. That is what "zero-setup trial" costs.
- The bucket contends per key, not per branch: at 12 writers on a local fake S3 a write's p95 is 1.6 s and a write
  loses 0.7 races on average, against 9 s and 3.4 for git. Writes to different keys do not conflict; only the lease and
  the counter do.
- The volume backend is the fastest durable one on one machine, and the only one that cannot be shared by pods on
  different nodes.
- `list` costs about 30 ms for a few dozen keys on moto and is flat there: this run does not show how it grows. A
  list reads every key under `state/`, so it grows with the key count; the thresholds below set the limit and the
  production check measures it.

## When Postgres would be justified

A database backend is a new thing to provision, secure, back up and operate, and the bucket needs none of that, so
the bar is a measured problem on the bucket in production, sustained, not a peak seen once. Each threshold is
checked over a rolling hour of normal operation (with the 12 slots busy), and **two of the four, or any one for a
day, reopens the question**.

| Measurement | Threshold | Why this number |
|---|---|---|
| Write latency p95 | above **2 s** sustained for an hour | A bucket write is a few tens of milliseconds plus the retries; 2 s is about ten times a healthy S3 round trip with a couple of retries, and above the benchmark's 12-writer p95 on a fake S3 (1.6 s), which is the contended worst case. The dispatch lease and the reserve are on the admission path, so seconds added there delay every launch |
| Lost races per write | above **1.0** at peak (12 slots busy) | The benchmark has 0.7 with 12 writers hammering the same two keys in a tight loop, far more than the factory does. Over 1.0 means the average write is retried at least once even at the factory's own rate, i.e. the shared keys (lease, reserve, queue) are the hot spot and per-key compare-and-swap no longer separates writers. The retry budget is 25 attempts (full jitter, at most about 40 s of sleeping); a rate near 1.0 sits far from running it out, and a `StateError` ("retries ran out") is itself a trigger |
| State objects written per hour | above **25,000** sustained | Half of the 12-writer ceiling measured here (48k/hour on a fake S3 in one process): headroom is gone well before the ceiling. In money it is state PUTs plus an equal number of audit PUTs, 50,000 PUTs/hour at S3's $0.005 per 1,000 is about $180 a month before LIST and GET requests, which is where a managed Postgres starts to cost less than the request bill |
| `list("state/")` latency, as keys grow | p95 above **1 s**, or more than **10,000** keys | A listing returns 1,000 keys a page, so 10,000 keys is ten sequential requests, about a second at 100 ms each, and `snapshot()` lists on every transaction. The factory prunes finished work, so growth past this means something is not being cleaned up (fix that first) or the load is real |

How to measure each in production, with what exists today and nothing new to deploy:

- **Objects per hour and write rate.** Every write that changed something puts one record under
  `audit/<UTC date>/<ns timestamp>-<id>.json` with `at`, `message`, `keys`, `action`, `size` and `host`.
  Count the records per hour (`aws s3 ls s3://BUCKET/audit/<date>/ | ...`, or the bucket's request metrics) for the
  write rate, and the distinct `host` values in an hour for the number of concurrent writers. `store.stats.writes`
  and `objects_written` are the same count per process.
- **Lost races per write.** Only `store.stats.lost_races` / `store.stats.writes` has it (the audit records a write
  that landed, not one that was refused). Read it from a process that has been running, or run
  `ops/state_bench.py` against a *scratch* bucket from the same network to get the number under controlled load.
  On S3 the bucket's request metrics (CloudWatch, filtered to the `state/` prefix) also show 412 and 409 responses
  per hour, which is the same event seen from the server.
- **Write latency p95.** The benchmark gives it for a scratch bucket from the pod's network; in production the
  S3 request metrics' `FirstByteLatency` and `TotalRequestLatency` for the prefix are the closest, and the
  timestamp of an audit record against the `at` of the request that caused it (queue markers carry theirs) is the
  end-to-end figure.
- **Request cost.** The bucket's request metrics (PUT, GET, LIST counts per prefix) times the price list; the
  threshold above is the point where that line exceeds what a small managed Postgres costs. Compare it with the
  bill, not the estimate.
- **List latency as keys grow.** Time `store.list("")` from a pod, and count its result (`len`): the state
  prefix's key count is the table's last column, and `aws s3 ls --recursive s3://BUCKET/state/ | wc -l` is the same
  count from outside.

If a threshold is crossed, the first moves are cheaper than a database: prune finished queue and marker keys, spread
the hot keys (the lease and the reserve) so writers collide less, or raise the retry budget. Postgres is for the case
where those are spent.
