"""Which `projects/<folder>/` a launch is judged in, when that is not the launch's name.

A launch name is an identity (harness directory, branch, base commit, commit identity). The folder is
where its files are judged. It comes only from the launch itself: a `Project folder:` line in the brief
passed at launch. Never from a file in the working tree, which the project can edit.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import flyte.errors

from . import _gate_scope, _session_config, _timeline

#: A list item or bold key is still the line: `- **Project folder:** projects/x/`. Only the first word after
#: the key is the value, so a trailing remark (`projects/x/ (the imported engine)`) is not part of it.
_BRIEF_LINE = re.compile(
    r"^[ \t]*(?:(?:[-*]|\d+\.)[ \t]+)?(?:\*\*)?Project folder:(?:\*\*)?[ \t]*(\S*)",
    re.MULTILINE | re.IGNORECASE,
)


def validate(folder: str) -> str:
    """Return `folder` if it is one safe path segment to put under `projects/`; else raise ValueError."""
    # Exactly one plain name: `engine/.`, `./engine` or `engine/` would be a second spelling of a folder,
    # which no folder-keyed comparison (scope, promote.toml protection) would recognise.
    if not folder or "/" in folder or "\\" in folder or folder in {".", ".."}:
        raise ValueError(
            f"project folder {folder!r} must be a single directory name under projects/ "
            "(no '/', '..', '.' or absolute path)"
        )
    return folder


def read_folder_from_brief(brief: str) -> str:
    """Return `<x>` from the first `Project folder: projects/<x>/` line of `brief`; "" without one."""
    match = _BRIEF_LINE.search(brief)
    if match is None:
        return ""
    value = match.group(1).strip("`*")
    segment = value.removeprefix("projects/").removesuffix("/")
    if not value.startswith("projects/"):
        raise ValueError(f"project folder {value!r} must look like projects/<name>/")
    return validate(segment)


def route(name: str, brief: str, timeline: _timeline._Timeline) -> str:
    """Return the folder launch `name` is judged in ("" for its name), once its workspace is cloned.

    A launch that cannot be routed is refused: a bad `Project folder:` line, a routed folder missing from a
    workspace, or an ambiguous one (`explain_launch_refusal`). Refused with `NonRecoverableError`, as a
    ValueError would be retried and a launch's brief is the same on every attempt.
    """
    if _session_config._layout() != "workspace":
        return ""  # only a workspace has `projects/<folder>/`; any other layout ignores the line
    try:
        folder = read_folder_from_brief(brief)
        refused = explain_launch_refusal(_session_config.WORKSPACE, name, folder, brief)
    except ValueError as exc:
        refused = str(exc)
    if refused:
        timeline.row("🚧", refused)
        raise flyte.errors.NonRecoverableError(refused)
    return folder


def explain_launch_refusal(workspace: Path, name: str, folder: str, brief: str) -> str:
    """Return why a workspace launch must be refused once its clone exists, or "" when it may run.

    A routed folder that is not in the clone would be judged empty, the very failure routing is for, so it
    is refused unless it is the launch's own name (a new project). Without a routed folder, see
    `explain_ambiguity`.
    """
    if not folder:
        return explain_ambiguity(workspace, name, brief)
    if folder != name and not (workspace / "projects" / folder).is_dir():
        return (
            f"the launch's project folder projects/{folder}/ does not exist in the workspace, so the gate "
            f"would judge an empty folder. Fix the `Project folder:` line, "
            f"or create projects/{folder}/ on the default branch first."
        )
    return ""


def explain_ambiguity(workspace: Path, name: str, brief: str) -> str:
    """Return why a launch with no explicit folder cannot be routed, or "" when it can.

    The gate would judge `projects/<name>/`. When that does not exist but the brief's `May edit:` paths
    name another existing project folder, the work is almost certainly there, and judging an empty
    folder would fail it for nothing: refuse so the launcher says which folder is meant.
    """
    if (workspace / "projects" / name).is_dir():
        return ""
    for path in _gate_scope.list_may_edit_paths(brief):
        parts = PurePosixPath(path).parts
        if len(parts) > 1 and parts[0] == "projects" and (workspace / "projects" / parts[1]).is_dir():
            other = parts[1]
            return (
                f"projects/{name}/ does not exist, but the brief's `May edit:` paths name projects/{other}/. "
                f"The gate would judge the empty projects/{name}/ and scope-fail every edit. Add "
                f"`Project folder: projects/{other}/` to the brief to work in that folder, or "
                f"`Project folder: projects/{name}/` if this really is a new project."
            )
    return ""
