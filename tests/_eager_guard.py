"""Find code that would act as the key Union injects into every pod instead of its tier's own key (#183).

Three shapes are offences: naming an injected variable (`_UNION_EAGER_API_KEY`, `EAGER_API_KEY`) in code,
building the rust controller (it reads the injected key itself), and using `init_in_cluster` directly (it
falls back to the injected key when `FLYTE_AUTH_TYPE` is unset), under whatever alias either is reached by.
`_union_identity` is the one module allowed to do any of them: it lists the injected names so it can remove
them, and wraps `init_in_cluster` once the tier key is in place. Docstrings and comments may mention the
names; code may not.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

INJECTED = re.compile(r"(?<![A-Z_])(_UNION_EAGER_API_KEY|EAGER_API_KEY)\b")
#: The same two calls inside a string: generated scripts (deploy children, probe subprocesses) are code too.
EMBEDDED = (
    (
        re.compile(r"create_controller\(\s*(?:ct\s*=\s*)?[\"']rust"),
        "builds the rust controller in a generated script",
    ),
    (re.compile(r"\binit_in_cluster\("), "calls init_in_cluster in a generated script"),
)

#: Repo-relative files allowed to name the injected variables or call `init_in_cluster`.
ALLOWED = frozenset({"src/flyte_agent_factory/_union_identity.py"})


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


#: The functions that can act as the injected key, under whatever local name a file imports them as.
_WATCHED = ("init_in_cluster", "create_controller")


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map each name bound by `from ... import init_in_cluster/create_controller [as x]` to its function."""
    return {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in _WATCHED
    }


def _resolve(node: ast.expr, aliases: dict[str, str]) -> str:
    """The watched function `node` refers to, through an import alias (`iic`) or a module alias (`f.x`)."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    return _name(node)


def offences(source: str, label: str) -> list[str]:
    """`label:line: what` for each offence in `source`."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    aliases = _aliases(tree)
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if match := INJECTED.search(node.value):
                found.append(f"{label}:{line}: names the injected key {match.group(1)}")
            found += [f"{label}:{line}: {what}" for pattern, what in EMBEDDED if pattern.search(node.value)]
            if node.value == "init_in_cluster":  # getattr(flyte, "init_in_cluster")
                found.append(f"{label}:{line}: looks up init_in_cluster by name outside _union_identity")
        elif isinstance(node, ast.ImportFrom) and any(a.name == "init_in_cluster" for a in node.names):
            found.append(f"{label}:{line}: imports init_in_cluster outside _union_identity")
        elif isinstance(node, ast.Name | ast.Attribute) and _resolve(node, aliases) == "init_in_cluster":
            # Any reference, called or not: `f.init_in_cluster`, `iic()`, `go = flyte.init_in_cluster`.
            found.append(f"{label}:{line}: uses init_in_cluster outside _union_identity")
        elif isinstance(node, ast.Call) and _resolve(node.func, aliases) == "create_controller":
            args = [*node.args[:1], *(k.value for k in node.keywords if k.arg == "ct")]
            if any(isinstance(a, ast.Constant) and a.value == "rust" for a in args):
                found.append(f"{label}:{line}: builds the rust controller, which acts as the injected key")
    return found


def scan(root: Path, dirs: tuple[str, ...] = ("src", "ops", "scripts")) -> list[str]:
    """Every offence in the engine's Python and shell sources under `root`."""
    found = []
    for top in dirs:
        for path in sorted((root / top).rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel in ALLOWED or not path.is_file():
                continue
            if path.suffix == ".py":
                found += offences(path.read_text(), rel)
            elif path.suffix == ".sh":
                for n, text in enumerate(path.read_text().splitlines(), 1):
                    code = text.split("#", 1)[0]
                    if match := INJECTED.search(code):
                        found.append(f"{rel}:{n}: names the injected key {match.group(1)}")
    return found
