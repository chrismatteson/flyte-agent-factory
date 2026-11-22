"""Step runner: timed steps, redacted output, skip-after-failure, teardown that always runs.

Copied from projects/oss-setup-selftest/selftest/runner.py (last changed in commit
6738fd3f6e414436d75065ee61080b5fc0f658b6), unchanged apart from formatting. This project cannot import across
project roots, and that source must stay unedited, so the code is copied rather than imported.

Runs inside the privileged child pod. Pure stdlib so it is testable offline.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import IO, Any

PASS, FAIL, SKIPPED, STOPPED = "pass", "fail", "skipped", "stopped"
EVIDENCE_CHARS = 1500
LOG_CHARS = 30000
# buildkit progress (`#12 3.4 ...`): kept in a log only when it reports an error.
BUILD_PROGRESS = re.compile(r"^#\d+ ")
ERROR_LINE = re.compile(r"error|exception|traceback|failed|fatal|refus|denied|not found|timed out", re.I)
REDACTED = "[REDACTED]"
logger = logging.getLogger(__name__)

# (name, step function, secret it needs by name or "")
StepSpec = tuple[str, Callable[[], object], str]


class StepFailed(Exception):
    """A step failed with a message worth putting in the report verbatim."""


@dataclass
class Step:
    """One step's record, as stored in the run JSON."""

    name: str
    result: str = SKIPPED
    seconds: float = 0.0
    evidence: str = ""
    human: str = ""  # why a human decision was needed; empty if none
    secret: str = ""  # which secret (by name) the step needed; empty if none
    gaps: list[str] = field(default_factory=list)  # doc gaps found, with the workaround used
    log: str = ""  # a failed command's condensed, redacted output (not rendered in the report)

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a plain dict."""
        return asdict(self)


class Redactor:
    """Removes secret values, and their common encodings, from any text."""

    def __init__(self, secrets: Iterable[str] = ()):
        """Redact each of `secrets`."""
        self._needles: list[str] = []
        for s in secrets:
            self.add(s)

    def add(self, secret: str) -> None:
        """Redact `secret`, its base64 forms and, if long, every 12-character window of it."""
        secret = secret.strip()
        if len(secret) < 8:  # too short to redact without mangling ordinary text; never a real token
            return
        forms = {
            secret,
            base64.b64encode(secret.encode()).decode(),
            base64.b64encode((secret + "\n").encode()).decode(),
        }
        # Partial leaks are leaks too: any 12-char window of the secret is redacted.
        if len(secret) > 24:
            forms.update(secret[i : i + 12] for i in range(0, len(secret) - 11))
        self._needles = sorted(set(self._needles) | {f for f in forms if f}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        """Return `text` with every secret form replaced."""
        for n in self._needles:
            if n in text:
                text = text.replace(n, REDACTED)
        return text


def wrap_in_isolated_env(cmd: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Wrap a command in `env -i` with exactly `env`: nothing from the pod's environment leaks in.

    The child pod's environment carries the outer run's Union key and Flyte settings; the inner
    CLI must see none of them (the probe found `flyte` misbehaving on inherited FLYTE_* settings).
    """
    return ["env", "-i", *(f"{k}={v}" for k, v in sorted(env.items())), *cmd]


class _RedactingFilter(logging.Filter):
    # A step's traceback goes to the pod log; it passes the same redactor as the step records.
    def __init__(self, redact: Redactor):
        super().__init__()
        self._redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            record.exc_info, record.exc_text = None, None
        record.msg, record.args = self._redact(text), None
        return True


class Runner:
    """Runs timed steps, captures redacted output and records each result."""

    def __init__(
        self,
        redact: Redactor | None = None,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Use `redact` on all captured text, `log` for progress and `clock` for timing."""
        self.redact = redact or Redactor()
        logger.addFilter(_RedactingFilter(self.redact))
        self.steps: list[Step] = []
        self.log = log
        self.clock = clock
        self._current: Step | None = None
        self._out: list[str] = []
        self.failed = False
        self.stopped = False

    # -- steps -------------------------------------------------------------------------------
    def step(
        self,
        name: str,
        fn: Callable[[], object],
        *,
        secret: str = "",
        human: str = "",
        always: bool = False,
    ) -> Step:
        """Run one step: timed, its failure recorded, skipped after an earlier failure."""
        self._run_loop([(name, fn, secret)], human=human, always=always)
        return self.steps[-1]

    def run(self, plan: Sequence[StepSpec], final: StepSpec, stop_after: str | None = None) -> None:
        """Run `plan` in order, stopping after `stop_after`; `final` (teardown) always runs."""
        try:
            self._run_loop(plan, stop_after=stop_after)
        finally:
            self._run_loop([final], always=True)

    def _run_loop(
        self,
        plan: Sequence[StepSpec],
        *,
        human: str = "",
        always: bool = False,
        stop_after: str | None = None,
    ) -> None:
        for name, fn, secret in plan:
            st = Step(name=name, secret=secret, human=human)
            self.steps.append(st)
            if (self.failed or self.stopped) and not always:
                st.result = STOPPED if self.stopped and not self.failed else SKIPPED
                st.evidence = (
                    "not run: an earlier step failed" if self.failed else "not run: --stop-after reached"
                )
                continue
            self._current, self._out = st, []
            start = self.clock()
            self.log(f"== step {name}")
            try:
                note = fn()
            except Exception as e:
                # Step boundary: any failure becomes the step's recorded (redacted) evidence, its
                # traceback goes to the pod log, and later steps are skipped; teardown still runs.
                logger.exception("step %s failed", name)
                st.result = FAIL
                msg = f"{type(e).__name__}: {e}" if not isinstance(e, StepFailed) else str(e)
                st.evidence = self._evidence(msg)
                self.failed = self.failed or not always
            else:
                st.result = PASS
                st.evidence = self._evidence(str(note) if note else "")
            finally:
                st.seconds = round(self.clock() - start, 1)
                self._current = None
                self.log(f"== step {name}: {st.result} in {st.seconds}s")
            if name == stop_after:
                self.stopped = True

    def stop(self) -> None:
        """Mark later steps as stopped (`--stop-after`)."""
        self.stopped = True

    def gap(self, text: str) -> None:
        """Record a place where the docs were wrong or silent, and what was done instead."""
        assert self._current is not None, "gap() outside a step"
        self._current.gaps.append(self.redact(text))
        self.log(f"   GAP: {self.redact(text)}")

    def needs_human(self, why: str) -> None:
        """Record on the current step why a human decision was needed."""
        assert self._current is not None
        self._current.human = why

    def _evidence(self, head: str) -> str:
        # Redact whole, then cut. The head's first line (the error, e.g. `exit 1: <cmd>`) is always kept:
        # run 20261116T035358Z lost it to a cut from the front.
        head = self.redact(head.strip())
        if len(head) > EVIDENCE_CHARS:
            first, _, rest = head.partition("\n")
            head = (
                first[:EVIDENCE_CHARS] + "\n…\n" + rest[-(EVIDENCE_CHARS - min(len(first), EVIDENCE_CHARS)) :]
            )
        tail = self.redact("".join(self._out))[-EVIDENCE_CHARS:].strip()
        return "\n--- output tail ---\n".join(p for p in (head, tail) if p)

    # -- commands ----------------------------------------------------------------------------
    def sh(
        self,
        cmd: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 600,
        check: bool = True,
        stdin_path: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command; output is redacted before it is kept or printed.

        `env` given means the command runs under `env -i` with exactly that environment.
        """
        argv = wrap_in_isolated_env(cmd, env) if env is not None else list(cmd)
        shown = self.redact(" ".join(cmd))
        self.log(f"$ {shown}")
        stdin: IO[bytes] | int = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        try:
            p = subprocess.run(
                argv,
                cwd=cwd,
                stdin=stdin,
                capture_output=True,
                timeout=timeout,
                env=None if env is not None else dict(os.environ),
            )
        except subprocess.TimeoutExpired as e:
            out = self.redact(((e.stdout or b"") + (e.stderr or b"")).decode(errors="replace"))
            self._out.append(f"$ {shown}\n{out}\n")
            raise StepFailed(f"timed out after {timeout:.0f}s: {shown}") from None
        finally:
            if not isinstance(stdin, int):
                stdin.close()
        out = self.redact(p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace"))
        self._out.append(f"$ {shown}\n{out}\n")
        for line in out.splitlines()[-15:]:
            self.log(f"   {line}")
        if check and p.returncode != 0:
            # The error is often mid-stream (stdout is followed by stderr; several deploy children
            # interleave), so every error-looking line is kept as well as the tail.
            lines = [
                ln
                for ln in out.splitlines()
                if not BUILD_PROGRESS.match(ln) or re.search(r"\bERROR\b|error:", ln)
            ]
            if self._current is not None:
                self._current.log = "\n".join(lines)[-LOG_CHARS:]
            errors = "\n".join(
                [ln for ln in lines if ERROR_LINE.search(ln) and not BUILD_PROGRESS.match(ln)][-40:]
            )
            raise StepFailed(
                f"exit {p.returncode}: {shown}\n--- error lines ---\n{errors}"
            )  # the tail is added by _evidence
        return subprocess.CompletedProcess(argv, p.returncode, out, "")

    def results(self) -> list[dict[str, Any]]:
        """Return every step record as a dict."""
        return [s.to_dict() for s in self.steps]
