"""Deploy the whole factory once, acting as the deployer's own Union API key.

The auto-deployer does this for every merged commit. Run this by hand when it cannot: an install whose
running deployer predates the profile's `[union]` table starts, but every deploy child it launches has no key
and, in a pod, no CLI login. This is the supported one-off. It runs the same two stages as the auto-deployer
(`_deploy.check_in_tree`, then `_deploy.deploy_in_tree`: slots, factory, apps) from this checkout of the
engine, with the profile read from a local checkout of the direction repository. Afterwards the new
deployer takes over.

Run it where the deployer's key is in a variable (a pod, or any machine); `--key-env` names that variable.
The key Union injects into every pod is removed from the children's environment and never used.

    export AGENT_FACTORY_UNION_KEY=<the deployer_key's API key>    # a pod mounts it from the secret
    python ops/deploy_as_key.py --direction ~/direction --check    # plan only; changes nothing
    python ops/deploy_as_key.py --direction ~/direction            # deploy
    python ops/deploy_as_key.py --direction ~/direction --profile profiles/work.toml --key-env MY_KEY_VAR

It must run from committed checkouts (the report names their commits), so it refuses uncommitted changes
to the engine (and untracked files under its `src/`) and to the profile. Once the check has run, the last
lines are a report: `deployed: ...`, `not deployed: ...` and, after a failure, `reason: ...`; `--check` ends
with `check only: ...`. When the check could not list the secrets (the key may not work), both modes stop
with exit 1 (`--check` ends with `check failed: ...`): the deployer goes on in that case because it retries
every tick, but this command is run once by a person, who should rerun it once the key works. Exit codes:
0 deployed (or `--check` passed), 1 a stage or the check failed, 2 refused before anything ran, 3 the
check found Flyte secrets that do not exist yet.

It sets none of the values only the deployer passes its deploy (the push mode its isolation canary chose,
the tier identities), so the environments deploy without App push (`app-read`) and `factory.status` shows the
tiers' client ids as unknown. The new deployer's first tick redeploys the same commits with both: this
command is not a deployer run, so the deployer does not count its deploy as one it made.

On an OSS backend with no Union keys there is no key to act as: use `ops/deploy_work.py` and
`ops/deploy_factory.py` there (they act as your CLI login and do not deploy the browser apps).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Run under `uv run`, which does not install this project (`package = false`): without this the import
# below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _deploy, _profile, _profile_oss, _pull_requests, _union_identity

ENGINE = Path(__file__).resolve().parents[1]


class Refused(Exception):
    """Nothing has run: the arguments or the checkouts cannot describe a deploy."""


def run_git(repo: Path, *args: str) -> str:
    """Return the output of `git -C repo args`; 60 s is generous for `rev-parse`/`status` on a checkout."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise Refused(f"{repo} is not a git checkout with a commit (git {args[0]}: {detail})") from None


def list_uncommitted(repo: Path, *paths: str, untracked: bool = False) -> str:
    """`git status` of `paths` in `repo`: what is deployed but would not be described by its commit."""
    flag = "--untracked-files=all" if untracked else "--untracked-files=no"
    return run_git(repo, "status", "--porcelain", flag, "--", *paths)


def build_child_env(base: dict[str, str], profile: _profile.Profile, key_env: str) -> dict[str, str]:
    """Return the environment of the deploy children: `base` acting as the key in `key_env`, nothing else.

    `strip_hand_deploy_keys` drops the injected key (and a stray `DEPLOY_KEY_ENV`); the profile selection is
    dropped because the profile is passed as text. As in the deployer's own `_child_env`, so are the GitHub
    App's credentials and every other tier's key (under both its names), which a factory pod
    may carry. So are the values only the deployer sets (`DEPLOYER_ONLY_ENV`: the push mode its isolation
    canary chose, and the tier identities): the deploy child bakes them into every environment, and this
    command has run no canary. The key stays under its own name, which is where
    `_union_identity.init_for_script` reads it.
    """
    env = _deploy.strip_hand_deploy_keys(base)
    hidden = {
        _profile.PROFILE_ENV,
        _profile.TOML_ENV,
        *_profile.DEPLOYER_ONLY_ENV,
        *_union_identity.OWN_IDENTITY_ENV,
        *_pull_requests.credential_env_vars(profile.github.private_key_secret, base),
    }
    for row in _union_identity.describe(profile):  # the SDK-prefixed second name of every tier's secret
        hidden.add(_pull_requests.prefixed_key_env_var(row["secret"], base))
    hidden -= {key_env}
    tier_key_prefix = (
        f"{_union_identity.KEY_ENV}_"  # `<KEY_ENV>_<TIER>`: the deployer mounts every tier's key
    )
    for name in list(env):
        if name in hidden or (name.startswith(tier_key_prefix) and name != key_env):
            del env[name]
    env[_deploy.DEPLOY_KEY_ENV] = key_env
    env[_union_identity.TIER_ENV] = "deployer"  # so a refusal names the tier and the secret
    env[_union_identity.SECRET_ENV] = profile.union.key_for("deployer")
    return env


def print_report(planned: list[str] | None, deployed: list[str], reason: str = "") -> None:
    """Print the final lines. `deployed` is what the stages report, `planned` what the check announced.

    `planned` is None when the check did not complete: there was no plan, so nothing can be said to be
    left over, and "none" would read as success.
    """
    print(f"deployed: {', '.join(deployed) or 'none'}")
    if planned is None:
        print("not deployed: all (the check did not complete, so nothing was planned)")
    else:
        print(f"not deployed: {', '.join(sorted(set(planned) - set(deployed))) or 'none'}")
    if reason:
        print(f"reason: {reason}")


def main(argv: list[str] | None = None) -> int:
    """Check, then deploy, the engine in `--engine` with the profile in `--direction`; see the docstring."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--direction", required=True, type=Path, help="a local checkout of the direction repository"
    )
    ap.add_argument("--profile", default="profile.toml", help="the profile's path inside it")
    ap.add_argument("--engine", type=Path, default=ENGINE, help="the engine checkout (default: this one)")
    ap.add_argument(
        "--key-env",
        default=_union_identity.KEY_ENV,
        help="the variable holding the profile's deployer_key (default: %(default)s)",
    )
    ap.add_argument("--check", action="store_true", help="run the check stage only; change nothing")
    args = ap.parse_args(argv)

    if not os.environ.get(args.key_env):
        print(
            f"refused: ${args.key_env} is not set: it must hold the profile's deployer_key", file=sys.stderr
        )
        return 2
    try:
        profile = _profile.load_from_direction(args.direction, args.profile, tolerate_unknown=True)
    except _profile.ProfileError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if _profile_oss.keyless(profile):
        print(
            "refused: this profile has no Union keys (an OSS backend), so there is no key to act as; "
            "use ops/deploy_work.py and ops/deploy_factory.py",
            file=sys.stderr,
        )
        return 2
    try:
        ref = run_git(args.engine, "rev-parse", "HEAD")
        direction_sha = run_git(args.direction, "rev-parse", "HEAD")
        # Untracked code under src/ is deployed too, and the profile is read from the working tree.
        dirty = [
            str(repo)
            for repo, found in (
                (
                    args.engine,
                    list_uncommitted(args.engine) or list_uncommitted(args.engine, "src", untracked=True),
                ),
                (args.direction, list_uncommitted(args.direction, args.profile, untracked=True)),
            )
            if found
        ]
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if dirty:
        print(
            f"refused: {' and '.join(dirty)} has uncommitted changes, so its commit would not describe what "
            "is deployed: commit or stash them",
            file=sys.stderr,
        )
        return 2
    where = f"{args.profile} at {direction_sha}"
    env = build_child_env(dict(os.environ), profile, args.key_env)
    src = args.engine / "src"
    # tree=None: the direction profile carries its own standards, as for the deployer.
    text = profile.deployment_text()
    project, domain = profile.backend.project, profile.backend.domain

    planned: list[str] | None = None
    deployed: list[str] = []
    warnings: list[str] = []
    try:
        planned = _deploy.check_in_tree(src, text, where, tree=None, base_env=env, warnings=warnings)
        for line in warnings:  # e.g. the secrets could not be listed: the key may not work
            print(f"check: {line}", file=sys.stderr)
        print(f"planned: {', '.join(planned)}", flush=True)
        unverified = [line for line in warnings if line.startswith(_deploy.SECRETS_NOT_VERIFIED)]
        if unverified:
            # The check has not shown the key works. The deployer goes on (it retries every tick); this is
            # run once by a person, who should rerun it once the key works.
            if args.check:
                print(f"check failed: would deploy {', '.join(planned)}; nothing deployed")
                print(f"reason: {unverified[0]}")
            else:
                print_report(planned, deployed, unverified[0])
            return 1
        if args.check:
            print(f"check only: would deploy {', '.join(planned)}; nothing deployed")
            return 0
        _deploy.deploy_in_tree(
            src,
            text,
            where,
            tree=None,
            project=project,
            domain=domain,
            ref=ref,
            base_env=env,
            deployed=deployed,
        )
    except _deploy.MissingSecrets as exc:
        print(f"missing secrets: {exc}", file=sys.stderr)
        for secret, envs in sorted(exc.secrets.items()):
            print(f"  {secret} (needed by {', '.join(envs)})", file=sys.stderr)
        print_report(planned, deployed, str(exc))
        return _deploy.MISSING_SECRETS_EXIT
    except _deploy.DeployError as exc:
        print_report(planned, deployed, str(exc))
        return 1
    missing = sorted(set(planned) - set(deployed))
    print_report(planned, deployed, f"deploy of {ref} did not deploy {', '.join(missing)}" if missing else "")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
