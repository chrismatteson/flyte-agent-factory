"""No engine code reads the key Union injects into every pod: each tier acts as its own key (#183)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests._eager_guard import offences, scan

ROOT = Path(__file__).resolve().parents[1]


def test_no_engine_code_acts_as_the_injected_key():
    assert scan(ROOT) == []


@pytest.mark.parametrize(
    "snippet",
    [
        'import os\nkey = os.environ["_UNION_EAGER_API_KEY"]',
        'import os\nkey = os.getenv("EAGER_API_KEY", "")',
        'NAMES = ("FLYTE_API_KEY", "_UNION_EAGER_API_KEY")',
        'from flyte._internal.controllers import create_controller\ncreate_controller("rust")',
        'from flyte._internal.controllers import create_controller\ncreate_controller(ct="rust")',
        "import flyte\nflyte.init_in_cluster()",
        "from flyte import init_in_cluster\nkwargs = init_in_cluster(project='p')",
        "from flyte import init_in_cluster as iic\nkwargs = iic()",
        "import flyte as f\nkwargs = f.init_in_cluster()",
        "import flyte as f\ngo = f.init_in_cluster\nkwargs = go()",
        "import flyte\nkwargs = getattr(flyte, 'init_in_cluster')()",
        'from flyte._internal.controllers import create_controller as cc\ncc("rust")',
        'from flyte._internal import controllers as c\nc.create_controller(ct="rust")',
        'CHILD = """\nfrom flyte._internal.controllers import create_controller\n'
        'create_controller("rust")\n"""',
        "PROLOGUE = f'import flyte\\nflyte.init_in_cluster(project={1!r})\\n'",
    ],
)
def test_the_guard_trips_on_a_planted_read(snippet):
    assert offences(snippet, "planted.py"), snippet


def test_the_guard_leaves_the_sanctioned_shapes_alone():
    source = textwrap.dedent(
        '''
        """Mentions _UNION_EAGER_API_KEY in a docstring, which is prose."""
        import os
        from flyte._internal.controllers import create_controller
        from flyte_agent_factory import _union_identity

        def f():
            """And EAGER_API_KEY here."""
            for name in _union_identity.INJECTED_VARS:  # _UNION_EAGER_API_KEY in a comment
                os.environ.pop(name, None)
            create_controller("remote", **_union_identity.controller_kwargs())
        '''
    )
    assert offences(source, "fine.py") == []


def test_a_shell_script_naming_the_injected_key_is_caught(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.sh").write_text(
        '# _UNION_EAGER_API_KEY in a comment is fine\nexport K="$EAGER_API_KEY"\n'
    )
    assert scan(tmp_path, ("scripts",)) == ["scripts/x.sh:2: names the injected key EAGER_API_KEY"]


def test_the_allow_list_is_only_the_identity_module():
    # `_union_identity` lists the injected names (to remove them) and wraps `init_in_cluster`.
    source = (ROOT / "src/flyte_agent_factory/_union_identity.py").read_text()
    assert offences(source, "_union_identity.py"), "the allow-listed module should be the one that needs it"
