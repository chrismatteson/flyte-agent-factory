# Headless author session: discovery report

Issue: private#219. Question: can the author session run through a structured headless interface on the
slot's subscription setup-token, with everything the TUI-on-a-pty does today?

Sources: Anthropic's documentation and terms, cited line by line in
[headless-author-discovery-docs.md](headless-author-discovery-docs.md), and a live probe on 2026-11-18 of the
image's pinned Claude Code (2.1.287) authenticated with the slot's `CLAUDE_CODE_OAUTH_TOKEN`
(`ops/probes/stream_json_probe.py`, run as a child action; its event log is
[headless-author-probe-2026-11-18.txt](headless-author-probe-2026-11-18.txt)).

## Verdict

**Use `claude -p --input-format stream-json --output-format stream-json --verbose` as one long-lived process,
with `--permission-prompt-tool stdio` for permission callbacks.** It is the same binary, the same
setup-token and the same quota as today, and the probe shows every path the factory needs.

The Python Agent SDK (`ClaudeSDKClient`) was the other candidate. It spawns this same CLI with these same
flags and speaks the same control protocol, so it would add a dependency and a second process lifecycle
without adding a capability. The factory already owns spawning (unprivileged user, the token handed over
through a pipe, process-group stop), and keeps that.

## Authentication and terms (setup-token, subscription quota)

- `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` is documented for non-interactive use: "for CI
  pipelines and scripts where browser login isn't available". With a plan login, "`claude -p` and Agent SDK
  usage still draw from your plan's usage limits". No page says headless or SDK use needs an API key.
- The restriction in the docs is on third parties offering claude.ai login, or routing other people's usage
  through plan credentials. This factory is owner-installed: the owner's own token runs the owner's own work
  on the owner's own infrastructure, which is the individual-use case. That is unchanged by this work: the
  TUI used the same token for the same thing.
- Requirements this places on the implementation: no `--bare` (it never reads OAuth credentials);
  `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` absent from the child's environment (they outrank the OAuth
  token, and in `-p` a set API key is always used); the token never written to disk. The existing
  `_secret_env` scrub and `_auth.token_fd` already do all three.
- Live: the probe's `system/init` reports `apiKeySource: "none"`, so the turn ran on the setup-token. The
  result's `total_cost_usd` is the client's estimate of list price, not a charge against a subscription.

## What the probe showed, per requirement

| Requirement | Evidence (probe section) |
|---|---|
| Persistent session, direction mid-conversation | One process took a first message, then a second on stdin, as two turns with `result_index` 0 and 1 ("deny + second turn"). A message written during a running turn was queued and answered as the next turn ("mid-turn message"). Multi-line text went in unchanged: no inbox-file workaround is needed. |
| Permission decisions in code | With `--permission-mode default --permission-prompt-tool stdio`, a Bash call arrived as `control_request` `can_use_tool` with `tool_name`, `input` and a `request_id`; replying `{"behavior":"deny","message":...}` blocked it (the file was not written) and the result listed it in `permission_denials`. |
| Policy (deny rules, hooks) under the factory's default bypass mode | A settings `deny` rule blocked `rm` in `bypassPermissions`, reported as `system/permission_denied`. A `PreToolUse` hook exiting 2 blocked a Bash call, reported as `system/hook_started` / `hook_response` with `--include-hook-events`. |
| Usage limits with machine-readable reset times | Every turn carries a `rate_limit_event` with `rate_limit_info.status` (`allowed` / `allowed_warning` / `rejected`), `resetsAt` (Unix seconds: `1794994800`), `rateLimitType` and per-window `utilization`. A refused call is also flagged on the assistant message (`error: "rate_limit"`) and the result (`is_error: true`). |
| Errors, turn ends | Each turn ends with one `result`: `subtype`, `is_error`, `terminal_reason`, `api_error_status`. An expired login is `error: "authentication_failed"` on the assistant message (seen locally with no token). |
| Liveness | Turn start is our own write; turn end is `result`; between them `assistant`, `user` (tool results), `system/thinking_tokens`, `task_started` / `task_updated` / `task_notification` (sub-agents and background shells) arrive as structured events. |
| Cost, tokens, turns | `result.total_cost_usd`, `usage` (input, output, cache read and creation tokens), `num_turns`, `duration_ms` per turn. |
| Interrupt (`/stop` mid-turn) | `control_request` `interrupt` answered `{"still_queued": []}`; the process kept running and took the next turn. |
| Resume after a pod restart | A new process with `--resume <session_id>` answered from the earlier conversation; so did `--continue`. Both keep the same `session_id`. |
| Sub-agents | An `Agent` call ran a sub-agent (`task_started` with `subagent_type`, `task_notification` with its usage) and handed back to the parent. |
| Workspace trust prompt | Not shown in `-p`; nothing to answer. |

## Gaps and how they are handled

- **Human attach / live view.** Nobody attaches to the factory's TUI: the operator's live view is the report,
  whose `[session]` lines `_session_log.TranscriptTail` reads from the transcript, and `-p` writes the same
  transcript, so that view is kept. What does not carry over is the Claude app: Remote Control, through which
  a person can type into an interactive session, is interactive-only (the Remote Control and headless pages
  never offer it for `-p` or the SDK), and the inbox watcher's "Claude app" turns are interactive `cli` entries
  by definition (`_inbox._human_turns`). In the stream session, direction comes through the inbox, `ask-human`
  answers and the Union console; the TUI fallback keeps the Claude app for anyone who needs it. Recorded as
  the one gap in [headless-author-session.md](headless-author-session.md).
- **Codex.** `CodexAuthor` already runs headless on `codex exec --json` and meets the same contract
  (structured turn ends, usage-limit events, thread resume). Its gaps are documented in `docs/engines.md`.
- **Slash commands.** In `-p`, `/compact` is still accepted as a user message (the init event lists it in
  `slash_commands`). Interactive-only commands are not used by the supervisor.
- **Fallback.** The pty path stays behind `AGENT_FACTORY_CLAUDE_TUI=1` for one release, so a Claude Code
  build that breaks the stream protocol can be worked around without a code change.
