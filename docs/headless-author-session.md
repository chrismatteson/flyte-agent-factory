# The structured author session

Issue: private#219. Before this, the Claude author was the interactive TUI on a pty: the supervisor typed
into it, read its screen, and matched screen text to find usage limits, permission menus and whether it was
still working. Now it is `claude -p` with stream-json in and out, one long-lived process on pipes, and every
one of those decisions reads a structured event. Discovery, with the documentation and terms consulted and a
live probe, is [headless-author-discovery.md](headless-author-discovery.md).

## Shape

`_author._author_argv` puts `_stream_process.STREAM_FLAGS` after `claude`:

```
claude -p --input-format stream-json --output-format stream-json --verbose \
  --permission-prompt-tool stdio --include-hook-events --name <project> --permission-mode <mode> ...
```

`_author._start_session` runs it as `_stream_process.StreamClaudeProcess`. That class keeps the attribute
names the supervisor already reads from `ClaudeProcess` and `CodexAuthor` (`tail`, `returncode`,
`limit_hit_at`, `limit_text`, `last_output`, `last_submission`, `sent_texts`, `login_expired`, `menu`, ...),
so `_supervise`, `_inbox` and `AuthorSession` drive all three engines the same way. Everything it sets comes
from an event:

| What the supervisor needs | Was (TUI on a pty) | Now (stream-json) |
| --- | --- | --- |
| Direction mid-conversation | typed; over 400 characters or with newlines, written to `.claude-flyte/inbox/` and a pointer typed | a `user` message on stdin, whole; one sent during a turn is queued by Claude Code and answered next |
| Usage limit and its reset | screen text matched by `_LIMIT_HIT` / `_LIMIT_RESET`, reset parsed from a clock time | `rate_limit_event` with `status: rejected` and `resetsAt` (Unix seconds) as `limit_reset_at`; `assistant.error == "rate_limit"` too. `_usage.session_reset_at` / `session_refusal_until` read the number, and every event's windows also go to the meter (`_usage.ingest_author_rate_limit`, source `cli-event`). Detection is always structured; the reset time is parsed from text only when no event carried one (an `assistant` `rate_limit` error before any rejected `rate_limit_event`, the TUI fallback, Codex) |
| Permission prompts | a menu found on screen (`_process.permission_prompt`), answered with Escape | a `can_use_tool` control request, decided by `_permission_policy.Policy` (deny with guidance; on the third denial of a tool in 30 minutes, deny and interrupt the turn); `system/permission_denied` for settings rules, reported as a timeline row |
| Hooks and policy | enforced by Claude Code | the same; `--include-hook-events` puts `hook_started` / `hook_response` in the session log |
| Liveness | the last time the screen changed | in flight from our message until its `result` (an hour of silence ends that, so a hung request reads as idle); idle is the time of the last event |
| Turn ends, errors | screen idle | `result`: `subtype`, `is_error`, `terminal_reason`, `num_turns` |
| Expired login | screen text | `authentication_failed` on an `assistant` message or a `system/api_retry` |
| Cost, tokens, turns | tokens summed from the transcript, no cost | each `result` appended to `state/author-turns.jsonl` (checkpointed; `at` is Unix seconds); `_usage.read_author_ledger` sums turns and tokens and takes each conversation's latest `total_cost_usd`, which the report shows as a list-price estimate (the session runs on the subscription). Each row is also bucketed by its `at` into the hourly records `factory.usage` estimates a credential's week from (`_usage_hours.add_author_turn`). Transcripts are still read for sub-agent helpers and for any conversation the ledger does not cover |
| `/stop` | `/exit` typed | `control_request` `interrupt` if a turn is running, then end of input, then SIGTERM / SIGKILL of the process group |
| Restart | `--resume <id>` in the checkpointed config dir | the same flags; the stream session keeps the conversation (probe and live run). A background task the previous process left running can be reported first, as a turn nobody sent; its events mark the session busy until its `result` |
| Sub-agents | Agent tool | the same; `task_started` / `task_notification` are logged |
| `/compact` before review | typed | sent as a user message, which `-p` accepts |

## What runs only on the TUI fallback

`AGENT_FACTORY_CLAUDE_TUI=1` runs the old TUI path (`_process.ClaudeProcess`) instead, for one release, so a
Claude Code build that breaks the stream protocol can be worked around without a code change. Only that path
uses:

- the prompt rules in `_process.py` (trust, theme and permission menus found on screen, and the supervisor's
  menu handling in `_supervise.py`, which runs only when `proc.menu` is set; `StreamClaudeProcess.menu` is
  always `None`);
- the limit-text regexes in `_process.py` and reading the reset out of `limit_text` (`_usage._reset_at`
  remains for Codex, whose refusals carry only text, and for reviewer refusals);
- the inbox-file workaround (`_process._type_message`).

## Gaps

- **Claude app.** Remote Control is interactive-only, so a person cannot type into the stream session from the
  Claude app, and the inbox watcher's "Claude app" turns (interactive `cli` entries) do not occur. Direction
  reaches the session through the inbox, `ask-human` answers and the Union console, as before; the live view
  is the report's `[session]` lines, read from the transcript as before (the live run shows `-p` writes it, its
  user entries marked `entrypoint: sdk-cli`). Set `AGENT_FACTORY_CLAUDE_TUI=1` for a
  run that must be reachable from the Claude app.
- **Codex.** `CodexAuthor` was already headless (`codex exec --json`) and meets the same contract, with the
  differences in [engines.md](engines.md#what-each-engine-supports-as-coordinator): no structured reset time
  (its refusal text is parsed), no mid-turn steering, no `/compact`, and no author token totals in the report.

## Tests and evidence

- `tests/test_stream_process.py` runs `StreamClaudeProcess` against a fake stream-json CLI: long multi-line
  direction, mid-turn queueing, permission requests, limits and resets, expired logins, result accounting
  and the ledger, liveness, stop, and a refused `--resume`.
- `tests/test_permission_policy.py`, `tests/test_stream_author_wiring.py`: the policy, the argv and fallback
  switch, the ledger and the structured reset helpers.
- `ops/probes/live_author_session.py`, submitted by `ops/probes/run_live_author_session.py`, runs this
  engine's `StreamClaudeProcess` with the argv `_author._author_argv` builds against the real CLI on a slot's
  setup-token. Its output is kept in `docs/headless-author-live-run.txt`.
