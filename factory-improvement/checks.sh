#!/usr/bin/env bash
# Checks for the factory-improvement project: the full offline suite and lint, from the repo root.
#
# The suite runs as two pytest processes at once, `not slow` and `slow` (the benchmark hidden checks:
# Terraform and pytest-in-pytest, about two thirds of the CPU); together they select every test. The slow lane
# runs only with AGENT_FACTORY_SLOW_LANE=child or AGENT_FACTORY_RUN_SLOW=1; CI's slow job always runs it. With
# AGENT_FACTORY_SLOW_LANE=child and a launcher (AGENT_FACTORY_TEST_LANE) both lanes go through it, in child
# actions, one per shard of test files: each shard tries spot, then on-demand. A lane that finds no capacity
# exits 69 and is not run on this pod. A lane the launcher could not send to a child action runs here only
# with no run attached, or with AGENT_FACTORY_LANE_RUN_HERE=1.
# AGENT_FACTORY_SLOW_LANE_HERE=1 runs the slow lane here from the start instead, for a caller with a time
# limit that a child failing mid-run would leave no room to run it in. Without a launcher both run here.
# With AGENT_FACTORY_PYTEST_JUNIT_DIR set (the pre-review does) each lane leaves a junit file there, which the
# standards gate reads instead of running the suite again. Extra arguments narrow the run, so then none is left.
set -euo pipefail
# The project root: the repository's, or a subdirectory of a workspace repository (projects/<name>/).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Read once and taken out of the environment: the suite's own tests of this script run it again.
slow_here=${AGENT_FACTORY_SLOW_LANE_HERE:-}
unset AGENT_FACTORY_SLOW_LANE_HERE

# pyproject's `-n auto` counts the node's CPUs (16 here), not the pod's cgroup quota: 16 workers on 4 CPUs. The
# quota is read by the module the lane launcher also uses (stdlib only, so the system python3 will do). If that
# fails the variable stays unset and pytest keeps its own default.
if [[ -z ${PYTEST_XDIST_AUTO_NUM_WORKERS:-} ]]; then
  workers=$(PYTHONPATH=src python3 -c 'from flyte_agent_factory import _lane_shards; print(_lane_shards.read_cpu_allowance())' 2>/dev/null) || workers=
  [[ -z $workers ]] || export PYTEST_XDIST_AUTO_NUM_WORKERS=$workers
fi

record=
if [[ -n "${AGENT_FACTORY_PYTEST_JUNIT_DIR:-}" && $# -eq 0 ]]; then
  record=1
  rm -f "$AGENT_FACTORY_PYTEST_JUNIT_DIR"/{fast,slow}.xml
  git rev-parse HEAD >"$AGENT_FACTORY_PYTEST_JUNIT_DIR/head"
fi

pytest_lane() {
  local lane=$1 marker=$2
  shift 2
  local junit=()
  # Exit 5 is "no tests selected": a narrowed run can leave one lane empty, which is not a failure.
  # On this 4-CPU pod the fast lane is the dearer one (about 900s here, against about 660s for the slow one), and
  # the two together here run past the checks' 900s limit, so the fast lane is the one that must get a child.
  if [[ $lane == fast && -n ${AGENT_FACTORY_TEST_LANE:-} && ${AGENT_FACTORY_SLOW_LANE:-} == child ]]; then
    [[ -z $record ]] || junit=(--junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    # The launcher sends each shard to spot, then on-demand, and exits 69 when a shard found no room: that fails
    # here, and is reported as no capacity, never run on this pod. --no-local makes it exit 75 instead of running
    # the lane here when it could not send it to a child action, so what happens then is this script's: with no
    # run (CI, a laptop) the lane runs here; in a run this pod is the one the lane was sent away from, so it is
    # not run here either, unless AGENT_FACTORY_LANE_RUN_HERE=1 opts in, as it does for the launcher.
    local code=75 attempt
    for attempt in 1 2; do
      code=0
      "$AGENT_FACTORY_TEST_LANE" --on-demand --no-local ${junit[@]+"${junit[@]}"} -m "$marker" "$@" || code=$?
      [[ $code -eq 75 ]] || break
      echo "fast lane: not sent to a child action on attempt $attempt" >&2
    done
    if [[ $code -eq 75 && -n ${FLYTE_INTERNAL_EXECUTION_ID:-} && ${AGENT_FACTORY_LANE_RUN_HERE:-} != 1 ]]; then
      echo "fast lane: not run here either: in a run, a lane the launcher could not send to a child action is" \
        "not run on this pod (AGENT_FACTORY_LANE_RUN_HERE=1 runs it here)" >&2
    elif [[ $code -eq 75 ]]; then
      echo "fast lane: running here, the launcher could not send it to a child action" >&2
      [[ -z $record ]] || junit=(--junitxml "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
      code=0
      PYTHONPATH=src uv run python -m pytest tests -q -m "$marker" ${junit[@]+"${junit[@]}"} "$@" || code=$?
    fi
    [[ $code -eq 0 || $code -eq 5 ]]
  elif [[ -z $slow_here && -n ${AGENT_FACTORY_TEST_LANE:-} && ${AGENT_FACTORY_SLOW_LANE:-} == child ]]; then
    [[ -z $record ]] || junit=(--junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    # The slow lane's shards go the same way; each attempt's runtime is taken from the lane's budget, so a shard
    # that waits out spot and then runs on-demand still ends inside the checks' limit.
    "$AGENT_FACTORY_TEST_LANE" ${junit[@]+"${junit[@]}"} "$@" || [[ $? -eq 5 ]]
  else
    [[ -z $record ]] || junit=(--junitxml "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    PYTHONPATH=src uv run python -m pytest tests -q -m "$marker" ${junit[@]+"${junit[@]}"} "$@" || [[ $? -eq 5 ]]
  fi
}

# Each lane's output is also kept, so that its verdict can be repeated at the end: whoever reads only the end of
# this log (the supervisor keeps its last 12 KB) still sees why a lane failed, whichever lane finished last.
lane_logs=$(mktemp -d)
trap 'rm -rf "$lane_logs"' EXIT
pytest_lane fast "not slow" "$@" > >(tee "$lane_logs/fast.log") &
fast=$!
# The slow lane (benchmark hidden checks: real Terraform, pytest-in-pytest) costs about two thirds of the suite's
# CPU. It runs when asked for (through the launcher, or here), and always in CI's slow job; beside a fast lane
# also running here, on a 4-CPU pod, it would hold these checks past their time limit.
slow=
if [[ ${AGENT_FACTORY_SLOW_LANE:-} == child || -n ${AGENT_FACTORY_RUN_SLOW:-} ]]; then
  pytest_lane slow slow "$@" > >(tee "$lane_logs/slow.log") &
  slow=$!
else
  echo "slow lane: not run here (CI's slow job runs it); AGENT_FACTORY_RUN_SLOW=1 or AGENT_FACTORY_SLOW_LANE=child runs it" >&2
fi
# A lane's verdict: the launcher's own block when it printed one, else pytest's failures and summary line.
verdict() {
  local lane=$1 code=$2 log=$lane_logs/$1.log
  echo "=== factory-improvement/checks.sh: $lane lane exit $code ==="
  [[ -s $log ]] || return 0
  if grep -q '^=== lane verdict' "$log"; then
    sed -n '/^=== lane verdict/,$p' "$log" | tail -n 40
  else
    grep -E '^(FAILED|ERROR) |^=+ .* in [0-9.]+s' "$log" | tail -n 20 || true
  fi
}
status=0 fast_code=0 slow_code=0
wait "$fast" || fast_code=$?
[[ -z $slow ]] || wait "$slow" || slow_code=$?
# The tee of each lane's output may still be writing after its lane is waited for.
wait 2>/dev/null || true
verdict fast "$fast_code"
[[ -z $slow ]] || verdict slow "$slow_code"
[[ $fast_code -eq 0 && $slow_code -eq 0 ]] || status=1
[[ $status -eq 0 ]] || exit 1

uv run --with ruff ruff check src tests ops .github
uv run --with ruff ruff format --check src tests ops .github
# The strict mypy baseline (pyproject [tool.mypy]); ruff check above includes the docstring rule (D, pep257).
uv run mypy .
# The chat skills ship as committed zips; a stale one (exit 1) or one with no version line (exit 2) fails here.
uv run python ops/package_skill.py --check skills/dist/agent-factory.zip
uv run python ops/package_skill.py --check skills/dist/since.zip
