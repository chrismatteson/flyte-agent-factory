"""A direction commit that changes nothing the deploy reads is not deployed. Offline."""

import ast
import builtins
import io
import os
from pathlib import Path

import pytest

from flyte_agent_factory import _deploy, _deploy_reads, _deployer, _profile
from tests.test_deployer import DIRECTION_SHA, OLD_DIRECTION_SHA, OLD_SHA, SHA, make_direction_profile
from tests.test_deployer import directed as directed
from tests.test_deployer import world as world

PROFILE_PATH = "profiles/work.toml"
BASE_FILES = ("engineering", "security")  # what a standards dir must hold by default


def with_standards(world, *, dir: str = "", brand: str = "") -> None:
    """The pod profile and the direction repo's copy both name this [standards]; the files exist there."""
    table = (
        "\n[standards]\n" + (f'dir = "{dir}"\n' if dir else "") + (f'brand = "{brand}"\n' if brand else "")
    )
    world.profile = _profile.parse(make_direction_profile(f"file://{world.state}").text + table, "work+std")
    world.ship_direction_profile(world.profile.text)
    for name in (*(f"{dir}/{base}.md" for base in BASE_FILES if dir), brand):
        if name:
            file = world.direction_tree / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("x\n")


def deployed_at_old_direction(world) -> None:
    world.record(SHA, OLD_DIRECTION_SHA)


# --- ticks -------------------------------------------------------------------------------------


async def test_ten_direction_heads_that_touch_nothing_read_deploy_nothing(directed):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed("operator/STATE.md", "projects/x/y.py")
    for i in range(10):
        directed.direction_gh.head_sha = f"{i + 1:040x}"
        message = await directed.tick()
        assert "changes nothing the deploy reads" in message
        assert OLD_DIRECTION_SHA in message and "2 files changed" in message
    assert directed.submissions == [] and not directed.deployed
    assert directed.fetched == []  # no CI check and no clone either


async def test_the_reason_names_the_read_set_and_how_to_force(directed):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed("operator/STATE.md")
    assert await directed.tick() == (
        f"direction {DIRECTION_SHA} changes nothing the deploy reads since {OLD_DIRECTION_SHA}: "
        f"1 file changed, none of {PROFILE_PATH}, profiles/standards/..., standards/...; "
        "not deploying (force: deploy_if_changed(force=True))"
    )


@pytest.mark.parametrize(
    "path", [PROFILE_PATH, "standards/x.md", "standards/languages/python.md", "profiles/standards/x.md"]
)
async def test_a_read_path_deploys(directed, path):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed("operator/STATE.md", path)
    assert (await directed.tick()).startswith(f"deployed {SHA} with direction {DIRECTION_SHA}")


@pytest.mark.parametrize(
    ("dir", "path"),
    [
        ("rules", "profiles/rules/rules.md"),  # beside the profile
        ("rules", "rules/rules.md"),  # relative to the root
        ("rules/", "rules/new.md"),  # a trailing slash and a deeper path
    ],
)
async def test_an_explicit_standards_dir_deploys_in_either_place(world, dir, path):
    with_standards(world, dir=dir.rstrip("/"))
    deployed_at_old_direction(world)
    world.compare_default = world.changed(path)
    assert (await world.tick()).startswith("deployed ")


async def test_the_brand_file_deploys(world):
    with_standards(world, brand="brand/b.md")
    deployed_at_old_direction(world)
    world.compare_default = world.changed("profiles/brand/b.md")
    assert (await world.tick()).startswith("deployed ")


async def test_a_file_beside_an_explicit_standards_dir_does_not(world):
    with_standards(world, dir="rules", brand="brand/b.md")
    deployed_at_old_direction(world)
    world.compare_default = world.changed("rules-extra/x.md", "brand/b.md.bak", "standards/x.md")
    assert "changes nothing" in await world.tick()


async def test_a_rename_out_of_standards_deploys(directed):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed(("standards/x.md", "docs/x.md"))
    assert (await directed.tick()).startswith("deployed ")


async def test_an_engine_change_deploys_an_unread_only_direction_change(directed):
    directed.record(OLD_SHA, OLD_DIRECTION_SHA)
    directed.compare_default = directed.changed("operator/STATE.md")
    assert (await directed.tick()).startswith(f"deployed {SHA} with direction {DIRECTION_SHA}")
    assert directed.compare_calls == []


@pytest.mark.parametrize(
    "answer",
    [
        404,
        500,
        {"status": "diverged", "files": []},
        {"status": "behind", "files": []},
        {"status": "identical", "files": []},
        {"status": "ahead"},  # no file list at all
        {"status": "ahead", "files": None},
    ],
    ids=["404", "500", "diverged", "behind", "identical", "no-files", "null-files"],
)
async def test_a_comparison_that_proves_nothing_deploys(directed, answer):
    deployed_at_old_direction(directed)
    directed.compare_default = answer
    assert (await directed.tick()).startswith("deployed ")


def link(tree: Path, name: str, target: str) -> None:
    """`name` becomes a link to `target` (relative to the link), taking over what was at `name`."""
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    real = path.parent / target
    real.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.rename(real)
    elif not real.exists():
        real.write_text("x\n")
    path.symlink_to(target)


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("standards/engineering.md", "../shared/eng.md"),  # a file in the standards dir
        ("standards", "real-std"),  # the standards dir itself
        ("profiles", "configs"),  # a directory holding the profile
    ],
)
async def test_a_link_in_a_read_path_deploys_whatever_the_comparison_names(directed, name, target):
    # The deploy follows links, so a change to their target (here `shared/`, `real-std/`, `configs/`) is
    # read through a path the comparison does not name.
    deployed_at_old_direction(directed)
    for base in BASE_FILES:
        (directed.direction_tree / "standards").mkdir(exist_ok=True)
        (directed.direction_tree / f"standards/{base}.md").write_text("x\n")
    link(directed.direction_tree, name, target)
    directed.compare_default = directed.changed(f"{target.removeprefix('../')}/x.md")
    assert (await directed.tick()).startswith("deployed ")


async def test_a_linked_profile_deploys(directed):
    deployed_at_old_direction(directed)
    (directed.direction_tree / "configs").mkdir()
    link(directed.direction_tree, PROFILE_PATH, "../configs/prod.toml")
    directed.compare_default = directed.changed("configs/prod.toml")
    assert (await directed.tick()).startswith("deployed ")


async def test_a_linked_brand_deploys(world):
    with_standards(world, brand="brand.md")
    deployed_at_old_direction(world)
    (world.direction_tree / "assets").mkdir()
    link(world.direction_tree, "brand.md", "assets/brand.md")
    world.compare_default = world.changed("assets/brand.md")
    assert (await world.tick()).startswith("deployed ")


async def test_a_link_outside_the_read_paths_still_skips(directed):
    deployed_at_old_direction(directed)
    link(directed.direction_tree, "operator/latest.md", "STATE.md")
    link(directed.direction_tree, "projects/current", "x")
    directed.compare_default = directed.changed("operator/STATE.md")
    assert "changes nothing the deploy reads" in await directed.tick()


@pytest.mark.parametrize(
    "answer", [500, 404, {"sha": "t", "tree": [], "truncated": True}, {"sha": "t"}], ids=str
)
async def test_a_tree_that_cannot_rule_out_links_deploys(directed, answer):
    # The standards directory is listed whole to find links in it; a list that fails or is cut short cannot.
    deployed_at_old_direction(directed)
    (directed.direction_tree / "standards").mkdir()
    for base in BASE_FILES:
        (directed.direction_tree / f"standards/{base}.md").write_text("x\n")
    directed.compare_default = directed.changed("operator/STATE.md")
    directed.subtree_answer = answer
    assert (await directed.tick()).startswith("deployed ")


async def test_a_state_md_commit_in_a_direction_repo_too_large_to_list_whole_is_skipped(directed):
    # A real direction repo is past what GitHub lists in one recursive tree (7 MB, ~100k entries), so only
    # the read paths' own trees are listed, never the repository's.
    deployed_at_old_direction(directed)
    (directed.direction_tree / "standards").mkdir()
    for base in BASE_FILES:
        (directed.direction_tree / f"standards/{base}.md").write_text("x\n")
    directed.tree_answer = {"sha": "t", "tree": [], "truncated": True}
    directed.compare_default = directed.changed("operator/STATE.md")
    assert "changes nothing the deploy reads" in await directed.tick()
    assert DIRECTION_SHA not in directed.recursive_calls
    assert len(directed.recursive_calls) == 1  # the standards tree, and nothing else


@pytest.mark.parametrize("count", [300, 800])
async def test_a_range_too_large_to_list_is_still_skipped_when_nothing_read_changed(directed, count):
    # GitHub lists at most 300 files of a comparison; the proof compares the read paths' objects instead.
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed(*(f"projects/new-import/f{i}.py" for i in range(count)))
    message = await directed.tick()
    assert "changes nothing the deploy reads" in message, message
    assert "100 or more files changed" in message


async def test_a_state_md_commit_after_skipped_project_commits_is_skipped(directed):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed(
        *(f"projects/p/f{i}.py" for i in range(299)), "operator/STATE.md"
    )
    message = await directed.tick()
    assert "changes nothing the deploy reads" in message, message


async def test_a_read_path_changed_in_a_range_too_large_to_list_deploys(directed):
    deployed_at_old_direction(directed)
    names = [f"projects/new-import/f{i}.py" for i in range(800)]
    directed.compare_default = directed.changed(*names, "standards/late.md")
    assert (await directed.tick()).startswith("deployed ")


async def test_a_read_path_is_compared_by_object_and_not_by_the_file_list(directed):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed(
        *(f"operator/{i}.md" for i in range(250)), "standards/late.md"
    )
    assert (await directed.tick()).startswith("deployed ")  # the `standards` tree differs
    assert len(directed.compare_calls) == 1  # one page of the comparison, however long the range


async def test_a_read_path_that_is_absent_at_both_commits_is_unchanged(world):
    with_standards(world, dir="rules")
    deployed_at_old_direction(world)
    # `[standards] dir` is read beside the profile (`profiles/rules`) or at the root; only `rules` exists.
    world.compare_default = world.changed("projects/x/y.py")
    assert "changes nothing the deploy reads" in await world.tick()


async def test_a_standards_dir_that_is_the_whole_repository_deploys_any_change(world):
    with_standards(world, dir=".")
    for base in BASE_FILES:  # "." beside the profile is `profiles`, which wins when it exists
        (world.direction_tree / "profiles" / f"{base}.md").write_text("x\n")
    deployed_at_old_direction(world)
    world.compare_default = world.changed("projects/x/y.py")
    assert (await world.tick()).startswith("deployed ")


async def test_a_read_path_that_appears_at_the_head_deploys(world):
    with_standards(world, dir="rules")
    deployed_at_old_direction(world)
    world.added_since_deployed = {f"rules/{base}.md" for base in BASE_FILES}
    world.compare_default = world.changed(*world.added_since_deployed)
    assert (await world.tick()).startswith("deployed ")


@pytest.mark.parametrize("dir", ["/etc/rules", "~/rules", "../rules", "a/../../rules"])
async def test_an_unknowable_standards_dir_deploys(world, dir):
    world.profile = _profile.parse(
        make_direction_profile(f"file://{world.state}").text + f'\n[standards]\ndir = "{dir}"\n', "work+std"
    )
    world.ship_direction_profile(
        make_direction_profile(f"file://{world.state}").text
    )  # a tree the deploy can read
    deployed_at_old_direction(world)
    world.compare_default = world.changed("operator/STATE.md")
    assert _deploy_reads.list_read_paths(world.profile) is None
    assert (await world.tick()).startswith("deployed ")
    assert world.compare_calls == []


async def test_force_deploys_an_unread_only_change(directed):
    deployed_at_old_direction(directed)
    directed.compare_default = directed.changed("operator/STATE.md")
    message = await _deployer.run_once(directed.profile, "run-me", directed.app, directed.clock(), force=True)
    assert message.startswith("submitted")
    assert directed.compare_calls == []


async def test_a_first_deploy_and_an_unchanged_direction_never_compare(directed):
    directed.record(SHA, "")
    await directed.tick()
    assert directed.compare_calls == []
    assert directed.tree_calls == []


# --- run_pair ----------------------------------------------------------------------------------


def move_heads_during_deploy(world, monkeypatch, new_head: str) -> None:
    def push_during_deploy(profile, src, **kw):
        world.factory_calls.append({"profile": profile, "src": src, **kw})
        world.direction_gh.head_sha = new_head
        return 0

    monkeypatch.setattr(_deploy, "deploy_factory", push_during_deploy)


async def test_heads_that_move_only_in_unread_files_cause_no_second_deploy(directed, monkeypatch):
    move_heads_during_deploy(directed, monkeypatch, OLD_DIRECTION_SHA)
    directed.compares[(DIRECTION_SHA, OLD_DIRECTION_SHA)] = directed.changed("operator/STATE.md")
    await directed.tick()
    assert len(directed.factory_calls) == 1
    assert directed.last["direction_sha"] == DIRECTION_SHA
    assert any("changes nothing the deploy reads" in line for line in directed.said)


async def test_heads_that_move_in_a_read_path_are_followed(directed, monkeypatch):
    move_heads_during_deploy(directed, monkeypatch, OLD_DIRECTION_SHA)
    directed.compares[(DIRECTION_SHA, OLD_DIRECTION_SHA)] = directed.changed("standards/x.md")
    await directed.tick()
    assert len(directed.factory_calls) == 2
    assert directed.last["direction_sha"] == OLD_DIRECTION_SHA


async def test_a_follow_up_is_judged_on_the_read_set_of_the_profile_just_deployed(directed, monkeypatch):
    # The pod profile reads standards/; the commit it deploys moves [standards] dir to rules/. A push during
    # the deploy that touches rules/ changes what the new release reads, so it is followed.
    directed.ship_direction_profile(directed.profile.text + '\n[standards]\ndir = "rules"\n')
    for base in BASE_FILES:
        (directed.direction_tree / "rules" / f"{base}.md").parent.mkdir(parents=True, exist_ok=True)
        (directed.direction_tree / "rules" / f"{base}.md").write_text("x\n")
    move_heads_during_deploy(directed, monkeypatch, OLD_DIRECTION_SHA)
    directed.compares[(DIRECTION_SHA, OLD_DIRECTION_SHA)] = directed.changed("rules/engineering.md")
    await directed.tick()
    assert len(directed.factory_calls) == 2
    assert directed.last["direction_sha"] == OLD_DIRECTION_SHA


# --- the read set ------------------------------------------------------------------------------

OPERATOR = "operator/profile.toml"


@pytest.mark.parametrize(
    ("profile", "standards"),
    [
        (PROFILE_PATH, ""),
        ("profile.toml", ""),
        (OPERATOR, ""),
        (OPERATOR, 'dir = "rules"'),  # beside the profile
        (OPERATOR, 'dir = "rules-at-root"'),  # only at the root
        ("profile.toml", 'dir = "rules"'),
        (OPERATOR, 'dir = "rules"\nbrand = "brand.md"'),
        (OPERATOR, 'brand = "brand-at-root.md"'),
        (PROFILE_PATH, 'dir = "./rules/"'),
    ],
)
def test_everything_the_deploy_reads_from_a_direction_tree_is_in_the_read_set(
    tmp_path, monkeypatch, profile, standards
):
    tree = tmp_path / "tree"
    base = make_direction_profile("file:///s").text.replace(
        '[direction]\nrepo = "git@github.com:acme/direction.git"\n',
        f'[direction]\nrepo = "git@github.com:acme/direction.git"\nprofile = "{profile}"\n',
    )
    text = base + (f"\n[standards]\n{standards}\n" if standards else "")
    beside = Path(profile).parent
    files = {
        profile: text,
        **{f"standards/{b}.md": "default" for b in BASE_FILES},
        "operator/STATE.md": "state",
        "projects/x/y.py": "print()",
        **{f"{d}/{b}.md": "x" for d in ("rules", "rules-at-root") for b in BASE_FILES},
        "rules/languages/python.md": "py",
        "brand.md": "brand",
        "brand-at-root.md": "brand",
        **{str(beside / f"rules/{b}.md"): "b" for b in BASE_FILES},
        str(beside / "brand.md"): "brand",
    }
    for name, content in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(content)
    # "rules-at-root" only exists at the root: from beside the profile it does not resolve.
    opened = recording_opens(monkeypatch, tree)
    loaded = _profile.load_from_direction(tree, profile, tolerate_unknown=True)
    loaded.deployment_text()
    reads = _deploy_reads.list_read_paths(_profile.parse(text, "x"))
    assert reads is not None
    root = tree.resolve()
    seen = {
        Path(p).resolve().relative_to(root).as_posix()
        for p in opened
        if Path(p).resolve().is_relative_to(root)
    }
    assert profile in seen
    unmatched = {p for p in seen if not _deploy_reads._is_read(p, reads)}
    assert not unmatched, f"read but not in {reads}: {sorted(unmatched)}"
    assert not _deploy_reads._is_read("operator/STATE.md", reads) or profile == "operator/STATE.md"
    assert not _deploy_reads._is_read("projects/x/y.py", reads)


def recording_opens(monkeypatch, tree: Path) -> list[str]:
    """Record every path opened by the usual ways of reading a file (a descriptor has no path to name)."""
    opened: list[str] = []

    def record(file) -> None:
        if isinstance(file, str | os.PathLike):
            opened.append(os.fspath(file))

    real_open, real_os_open = builtins.open, os.open
    real_path_open, real_read_text, real_read_bytes = Path.open, Path.read_text, Path.read_bytes

    def open_(file, *args, **kwargs):
        record(file)
        return real_open(file, *args, **kwargs)

    def os_open(path, *args, **kwargs):
        record(path)
        return real_os_open(path, *args, **kwargs)

    def path_open(self, *args, **kwargs):
        record(self)
        return real_path_open(self, *args, **kwargs)

    def read_text(self, *args, **kwargs):
        record(self)
        return real_read_text(self, *args, **kwargs)

    def read_bytes(self):
        record(self)
        return real_read_bytes(self)

    monkeypatch.setattr(builtins, "open", open_)
    monkeypatch.setattr(io, "open", open_)
    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return opened


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("", (PROFILE_PATH, "profiles/standards", "standards")),
        ('[standards]\ndir = "r"', (PROFILE_PATH, "profiles/r", "r")),
        (
            '[standards]\ndir = "r"\nbrand = "b.md"',
            (PROFILE_PATH, "profiles/r", "r", "profiles/b.md", "b.md"),
        ),
        ('[standards]\ndir = "."', (PROFILE_PATH, "profiles", ".")),
        ('[standards]\ndir = "./r/"', (PROFILE_PATH, "profiles/r", "r")),
    ],
)
def test_the_read_set_names_both_places_a_relative_path_resolves(table, expected):
    profile = _profile.parse(make_direction_profile("file:///s").text + "\n" + table, "x")
    assert _deploy_reads.list_read_paths(profile) == expected


@pytest.mark.parametrize(
    "table",
    [
        'dir = "/abs"',
        'dir = "~/s"',
        'dir = "../s"',
        'brand = "/b.md"',
        'brand = "../b.md"',
        # `sub/..` exists only while `sub` does, and `sub` is not read.
        'dir = "a/../r/"',
        'brand = "sub/../b.md"',
    ],
)
def test_an_absolute_home_or_escaping_path_makes_the_read_set_unknown(table):
    profile = _profile.parse(make_direction_profile("file:///s").text + f"\n[standards]\n{table}\n", "x")
    assert _deploy_reads.list_read_paths(profile) is None


def test_no_direction_has_no_read_set():
    assert (
        _deploy_reads.list_read_paths(
            _profile.parse(make_direction_profile("file:///s").text.split("\n[direction]")[0], "x")
        )
        is None
    )


# --- every direction read is covered -----------------------------------------------------------


def call_sites(tree: ast.Module, name: str) -> dict[str, list[ast.Call]]:
    """Calls of `name` (a function or a method) by the function that makes them."""
    found: dict[str, list[ast.Call]] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for call in ast.walk(function):
            target = call.func if isinstance(call, ast.Call) else None
            called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            if called == name:
                found.setdefault(function.name, []).append(cast_call(call))
    return found


def cast_call(node: ast.AST) -> ast.Call:
    assert isinstance(node, ast.Call)
    return node


def test_the_deployer_reads_the_direction_repo_only_where_the_read_set_looks():
    tree = ast.parse(Path(_deployer.__file__).read_text())
    # A direction clone, by its URL builder: `_deploy_sha`'s fetch is the engine's (`_clone_url`).
    assert set(call_sites(tree, "_app_clone_url")) == {"_clone_url", "_read_direction"}
    assert set(call_sites(tree, "fetched_tree")) == {"_read_direction", "_deploy_sha"}
    for function, calls in call_sites(tree, "fetched_tree").items():
        (url,) = (c.args[0] for c in calls)
        assert isinstance(url, ast.Call) and isinstance(url.func, ast.Name)
        assert url.func.id == {"_read_direction": "_app_clone_url", "_deploy_sha": "_clone_url"}[function]
    assert set(call_sites(tree, "load_from_direction")) == {"_read_direction"}
    # The contents API, which reads one file of the direction repo: the profile.
    assert set(call_sites(tree, "read_file")) == {"_move_head"}
    # The profile files and standards the deploy reads are the ones the read set names.
    assert "read_file(_github.parse_repo(direction.repo), direction.profile" in ast.unparse(tree)
