"""A digest of the source a module statically imports within this package: what a pod running it can execute.

The factory stage ships one code bundle of everything it imported, so the bundle (and the `--tgz` it puts in
an app's command) changes on every deploy. An app is restarted by any spec change, and a restart of
`factory-mcp` drops the chat connector; this digest lets an app's spec change only when code it can actually
run changed.

Only static imports are followed, the same ones the bundler follows (AGENTS.md: a module loaded by name at
runtime is missing in the container anyway). Imports under `if TYPE_CHECKING:` never run and are skipped.
Only `.py` files are hashed: a data file an app reads is not in the digest (the bundle carries modules only).
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

PACKAGE = "flyte_agent_factory"


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _find_imports(node: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Return every import under `node`, function-level ones included, `if TYPE_CHECKING:` bodies excluded."""
    found: list[ast.Import | ast.ImportFrom] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):  # an import is a statement; skipping expressions is most of the tree
            continue
        if isinstance(child, ast.Import | ast.ImportFrom):
            found.append(child)
        elif isinstance(child, ast.If) and _is_type_checking(child.test):
            for other in child.orelse:  # the else branch does run
                found.extend(
                    [other] if isinstance(other, ast.Import | ast.ImportFrom) else _find_imports(other)
                )
        else:
            found.extend(_find_imports(child))
    return found


def _resolve_targets(node: ast.Import | ast.ImportFrom) -> set[str]:
    """Return the names of this package's modules `node` may import (`x` for `flyte_agent_factory.x`)."""
    if isinstance(node, ast.Import):
        parts = [a.name.split(".") for a in node.names]
        return {p[1] for p in parts if p[0] == PACKAGE and len(p) > 1}
    module = node.module.split(".") if node.module else []
    if node.level == 0:
        if module[:1] != [PACKAGE]:
            return set()
        module = module[1:]
    if module:  # `from .x import y`, `from flyte_agent_factory.x import y`
        return {module[0]}
    # `from . import x`, `from flyte_agent_factory import x`: each name may be a submodule.
    return {a.name for a in node.names}


def find_import_closure(package_dir: Path, module: str) -> list[Path]:
    """Return the files of `package_dir` that `module` imports statically: itself, `__init__.py` included."""
    # `__init__` is seeded as a module to read, not as seen: its own imports are code the package runs too.
    todo, seen = [module, "__init__"], set[str]()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = package_dir / f"{name}.py"
        if not path.is_file():  # an attribute imported from the package, not a submodule
            continue
        seen.add(name)
        for node in _find_imports(ast.parse(path.read_bytes(), filename=str(path))):
            todo.extend(_resolve_targets(node) - seen)
    return sorted(package_dir / f"{name}.py" for name in seen)


def hash_import_closure(package_dir: Path, module: str) -> str:
    """Return a sha256 over the sorted (relative path, bytes) of `find_import_closure(...)`."""
    h = hashlib.sha256()
    for path in find_import_closure(package_dir, module):
        rel = path.relative_to(package_dir).as_posix().encode()
        h.update(len(rel).to_bytes(4, "big") + rel)
        data = path.read_bytes()
        h.update(len(data).to_bytes(8, "big") + data)
    return h.hexdigest()
