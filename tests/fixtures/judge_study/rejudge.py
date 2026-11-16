"""A fake of the study's `rejudge`: the same names and semantics for what the harness reuses, no Flyte."""

import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPS = (1, 2)
USAGE_MARK = "\n@@USAGE@@"
IMAGE = "fake-image"
prepared: list[tuple[str, Path]] = []


def _stamp_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def make_decision_stem(decision_id: str) -> str:
    return decision_id.replace(":", "__")


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)


def build_rep_file(rejudge_dir: Path, decision_id: str, rep: int) -> Path:
    return rejudge_dir / make_decision_stem(decision_id) / f"rep{rep}.json"


def build_rep_path(results: Path, decision_id: str, rep: int) -> Path:
    return build_rep_file(results / "rejudge", decision_id, rep)


def prepare_workspace(source: Path, head_sha: str, dest: Path) -> None:
    prepared.append((head_sha, dest))
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)


def split_usage(text: str) -> tuple[str, dict]:
    clean, _, rec = text.partition(USAGE_MARK)
    return clean, json.loads(rec) if rec else {}


def price_usage(rec: dict) -> float | None:
    return rec.get("usd")


def install_launch_context() -> str:
    return IMAGE


async def _call_with_retry(call, prompt: str, workdir: Path) -> tuple[str, list[str]]:
    return await call(prompt, workdir)


async def _call_waiting_for_capacity(call, prompt: str, workdir: Path) -> tuple[str, list[str]]:
    return await call(prompt, workdir)


def _abandon(budget: "Budget", spent_on_attempts: float) -> bool:
    budget.unrecorded += spent_on_attempts
    budget.write()
    return False


def order_runs(rows: list[dict], results: Path) -> list[dict]:
    def key(row: dict) -> tuple[bool, str]:
        have = [build_rep_path(results, row["decision_id"], rep).exists() for rep in REPS]
        return not (any(have) and not all(have)), hashlib.sha256(row["decision_id"].encode()).hexdigest()

    return sorted(rows, key=key)


class Budget:
    def __init__(self, cap: float, path: Path, spent: float = 0.0, unrecorded: float = 0.0) -> None:
        self.cap, self.path = cap, path
        self.spent, self.unrecorded, self.reserved = spent + unrecorded, unrecorded, 0.0
        self.stopped: str | None = None

    def admit(self, amount: float) -> bool:
        if self.stopped is not None:
            return False
        return self.reserve(amount)

    def reserve(self, amount: float) -> bool:
        if self.spent + self.reserved + amount > self.cap:
            if self.stopped is None:
                self.stopped = f"would pass the ${self.cap:.2f} cap"
                self.write()
            return False
        self.reserved += amount
        return True

    def fits(self) -> bool:
        if self.spent + self.reserved <= self.cap:
            return True
        if self.stopped is None:
            self.stopped = f"passes the ${self.cap:.2f} cap"
            self.write()
        return False

    def settle(self, reserved: float, actual: float) -> None:
        self.reserved = max(0.0, self.reserved - reserved)
        self.spent += actual
        self.write()

    def write(self) -> None:
        write_json_atomic(
            self.path,
            {
                "total_usd": round(self.spent, 4),
                "unrecorded_usd": round(self.unrecorded, 4),
                "reserved_usd": round(self.reserved, 4),
                "cap_usd": self.cap,
                "stopped": self.stopped,
            },
        )


def load_budget(results: Path, cap: float) -> Budget:
    files = (results / "rejudge").glob("*/rep*.json")
    spent = sum(float(json.loads(p.read_text()).get("usd") or 0.0) for p in files)
    unrecorded = 0.0
    try:
        unrecorded = float(json.loads((results / "spend.json").read_text()).get("unrecorded_usd") or 0.0)
    except FileNotFoundError:
        pass
    return Budget(cap, results / "spend.json", spent, unrecorded)
