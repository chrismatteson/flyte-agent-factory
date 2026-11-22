# Security policy

## Reporting a vulnerability

Report it privately through GitHub security advisories: open the repository's **Security** tab and choose
**Report a vulnerability**. Please do not open a public issue or pull request for it.

Include what you found, how to reproduce it, and which version or commit you tested. We will acknowledge the report and
keep you informed until it is fixed.

## Supported versions

Only the latest `main` (and the latest release, once there are releases) receives security fixes. Reproduce on it
before reporting, and upgrade to it to pick up a fix.

## Never put credentials in issues

Do not paste tokens, API keys, Claude or Codex login files, private keys, deploy keys or full environment dumps into
an issue, pull request, log excerpt or screenshot. If you did, revoke the credential first, then tell us. When you
share configuration, name the secrets and leave their values out.

## Flyte SDK telemetry

The Flyte SDK reports crashes and counts operations (every run, task deploy, app deploy and trigger deploy) to a
hosted Sentry run by the SDK's maintainers. A crash report includes the local variables of each stack frame, and a
factory pod holds tokens in its environment and variables. So the factory turns the reporting off: every
environment it deploys, and the deploy child, gets `DO_NOT_TRACK=1`, whatever the shell that deployed it carried.

To send the SDK's crash reports and usage counters, put this in the profile and deploy again:

```toml
[telemetry]
flyte_sdk = true
```

The value must be `true` or `false`; the profile is refused with any other value or key in `[telemetry]`. Leaving
the table out means off. `DO_NOT_TRACK` is set from the profile only, never from the environment of the process
that deploys.
