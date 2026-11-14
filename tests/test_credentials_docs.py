"""The docs, the skill and the profile reference agree on the credential model (#179, #183)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def flat(path: str) -> str:
    return " ".join((ROOT / path).read_text().split())


def headings(path: str) -> set[str]:
    """GitHub anchors: lower-case, spaces to `-`, punctuation dropped."""
    anchors = set()
    for line in (ROOT / path).read_text().splitlines():
        if match := re.match(r"#{1,6}\s+(.*?)\s*#*$", line):
            title = re.sub(r"[^\w\- ]", "", match.group(1).lower().replace("`", ""))
            anchors.add(title.strip().replace(" ", "-"))
    return anchors


def doc_files() -> list[Path]:
    files = [*(ROOT / "docs").rglob("*.md"), *(ROOT / "skills/agent-factory").rglob("*.md")]
    return [*files, ROOT / "profiles/README.md"]


def test_setup_doc_has_the_union_keys_and_migration_sections():
    raw = (ROOT / "docs/setup.md").read_text()
    assert "\n## Union keys\n" in raw and "\n## Migrating an existing install\n" in raw
    text = flat("docs/setup.md")
    for phrase in ("`project_key`", "`deployer_key`", "`python ops/bootstrap.py --union-keys`"):
        assert phrase in text, phrase
    assert "`contributor` on the one project and domain; nothing org-wide" in text
    assert "a launch into the staging domain was refused" in text
    assert "listing projects org-wide was refused" in text
    assert 'Remove `push = "app"` to return to deploy keys' in text


def test_github_app_doc_states_the_push_modes_and_the_canary_gate():
    raw = (ROOT / "docs/github-app.md").read_text()
    assert "\n## Workflows\n" in raw
    text = flat("docs/github-app.md")
    assert "Set `[github] workflows = true`" in text
    assert "`deploy_key` (default)" in text and "| `app` |" in text
    assert "reports `isolated`" in text and "Two GitHub credentials remain for now" in text
    assert "Deploy-key-only mode** (no App) is a documented fallback with **no CI visibility**" in text
    assert "install the App on that repository" in text


def test_profile_reference_documents_union_and_github_push():
    text = flat("profiles/README.md")
    for key in ("project_key", "deployer_key", "session_key", "queue_key"):
        assert f"`{key}`" in text, key
    assert "`[union]` `project_key`, `deployer_key`" in text
    assert "`[github]` `push`, `workflows`" in text
    assert "only where the GitHub App is not installed" in text
    assert "direction moved: old -> new at <sha>" in text


def test_skill_setup_reference_names_union_keys_and_the_app_default():
    text = flat("skills/agent-factory/references/setup.md")
    assert "`--union-keys`" in text
    assert "Your own GitHub App.** The guided-setup default, read-only on code" in text
    assert "reports `isolated`" in text


def test_no_doc_tells_the_reader_to_set_the_injected_key():
    for path in doc_files():
        text = path.read_text()
        assert not re.search(r"export .*EAGER", text), path
        assert not re.search(r"EAGER_API_KEY\s*=", text), path
        assert "--deploy-key" not in text, path


def test_no_doc_says_the_credential_model_is_undecided():
    for path in doc_files():
        text = path.read_text()
        assert "#179 decides" not in text and "issue #179" not in text, path


def test_anchors_linked_from_source_exist_in_the_docs():
    found = set()
    for source in (ROOT / "src").rglob("*.py"):
        found.update(re.findall(r"docs/(setup|github-app)\.md#([\w-]+)", source.read_text()))
    assert ("setup", "union-keys") in found and ("github-app", "workflows") in found
    for doc, anchor in found:
        assert anchor in headings(f"docs/{doc}.md"), f"docs/{doc}.md has no heading for #{anchor}"


def test_no_doc_says_sessions_cannot_read_the_app_key_while_a_session_launched_pod_can_mount_it():
    """The App key is out of a session's environment, not out of its reach: the docs say which (#51)."""
    for path in doc_files():
        text = " ".join(path.read_text().split())
        for sentence in re.split(r"(?<=[.;:])\s", text):
            if "App" in sentence and re.search(
                r"(sessions?|workers) (and delegate workers )?cannot read it\b", sentence
            ):
                raise AssertionError(f"{path.relative_to(ROOT)}: {sentence}")
    for doc in ("docs/setup.md", "docs/github-app.md", "profiles/README.md"):
        assert "a session can launch a task that mounts" in flat(doc), doc


def test_reading_a_private_repo_by_the_app_is_described_as_read_only():
    setup, app = flat("docs/setup.md"), flat("docs/github-app.md")
    assert "cloned with a contents-read token for that one repository" in setup
    assert "pushing to it still needs its deploy key" in setup
    assert "The token reads; it cannot push." in app


def test_docs_say_the_verifier_is_advisory_and_never_call_the_app_the_default_credential():
    for path in ("docs/github-app.md", "docs/branch-protection.md"):
        text = flat(path)
        assert "advisory" in text and "isolation canary" in text and "reports `isolated`" in text, path
    assert "the human merge remains the gate" in flat("docs/branch-protection.md")
    assert "App key reachable from sessions" in flat("docs/github-app.md")
    for doc in doc_files():
        assert "The App is the default GitHub credential" not in " ".join(doc.read_text().split()), doc
    for name in ("README.md", "profiles/README.md", "skills/agent-factory/references/setup.md"):
        assert "default GitHub credential" not in flat(name), name


def test_the_exposure_is_temporary_and_points_at_the_one_credential_migration():
    text = flat("docs/github-app.md")
    assert "The exposure is temporary" in text
    assert "(setup.md#migrating-an-existing-install)" in text
    assert "## Migrating an existing install" in (ROOT / "docs/setup.md").read_text()


def test_the_profile_reference_says_a_move_the_engine_refuses_waits_for_a_commit_in_the_new_repo():
    # Review round 1 (C12, C35): the engine refusing the new repo's profile is not a counted attempt either.
    text = flat("profiles/README.md")
    assert (
        "A profile in the new repo that the engine being deployed refuses (an unknown key, for instance)"
        " waits the same way, and deploys on the first tick after the fixing commit in the new repo." in text
    )
