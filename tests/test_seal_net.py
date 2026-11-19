import os
import pty
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from flyte_agent_factory import _seal_net

pytestmark = pytest.mark.usefixtures("seal_namespaces")

SRC = str(Path(_seal_net.__file__).resolve().parent.parent)


def _run(
    code, *, socket_path="/nonexistent.sock", port=18999, hide=(), readonly=(), pass_fds=(), env=None, **kw
):
    argv = _seal_net.launcher_argv(
        socket_path,
        port,
        kw.pop("index_url", None),
        kw.pop("exclude_newer", None),
        hide,
        readonly,
        kw.pop("writable", ()),
        kw.pop("private_tmp", None),
    )
    full_env = {**os.environ, "PYTHONPATH": SRC, **(env or {})}
    return subprocess.run(
        [*argv, sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=full_env,
        pass_fds=pass_fds,
        timeout=60,
        check=False,
        **kw,
    )


def test_available_is_cached_bool():
    assert _seal_net.available() is True
    assert _seal_net.available() is True


def test_a_non_dumpable_caller_probes_and_launches_sealed():
    # `project`, `subagent` and `sealed_plain` make themselves non-dumpable before sealing a role. A
    # forked child of such a process cannot write its own uid map, so a probe by fork refused every
    # sealed role in a deployed task; the probe and the launch must both work from there.
    argv = _seal_net.launcher_argv("/nonexistent.sock", 18999)
    code = (
        "import subprocess, sys\n"
        "from flyte_agent_factory import _auth, _seal_net\n"
        "_auth.set_non_dumpable()\n"
        "assert _seal_net.unavailable_reason() == '', _seal_net.unavailable_reason()\n"
        f"out = subprocess.run({argv!r} + [sys.executable, '-c', 'print(\"inside\")'],"
        " capture_output=True, text=True, check=True).stdout\n"
        "print(out.strip())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC},
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "inside"


def test_the_probe_reports_why_the_seal_is_impossible(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    assert "probe did not run" in _seal_net._probe()


def test_launcher_argv_shape(tmp_path):
    argv = _seal_net.launcher_argv(
        str(tmp_path / "s"), 1234, "http://127.0.0.1:1234/index/simple/", "2025-01-01T00:00:00Z", ["/a", "/b"]
    )
    assert argv[:3] == [sys.executable, "-m", "flyte_agent_factory._seal_net"]
    assert argv[-1] == "--"
    assert argv.count("--hide") == 2
    assert "--index-url" in argv
    assert "--exclude-newer" in argv
    bare = _seal_net.launcher_argv("s", 1, None, None, ())
    assert "--index-url" not in bare
    assert "--hide" not in bare
    assert "--readonly" not in bare
    assert _seal_net.launcher_argv("s", 1, readonly=["/a", "/b"]).count("--readonly") == 2


def test_direct_connect_and_dns_fail():
    code = (
        "import socket\n"
        "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=5); print('CONNECTED')\n"
        "except OSError as e: print('connect-failed', e.errno)\n"
        "try:\n    socket.getaddrinfo('github.com', 443); print('RESOLVED')\n"
        "except OSError: print('dns-failed')\n"
    )
    done = _run(code)
    assert done.returncode == 0, done.stderr
    assert "connect-failed" in done.stdout
    assert "dns-failed" in done.stdout
    assert "CONNECTED" not in done.stdout
    assert "RESOLVED" not in done.stdout


def test_uid_unchanged_and_exit_status_propagates():
    done = _run("import os, sys; print(os.getuid(), os.getgid()); sys.exit(7)")
    assert done.returncode == 7
    assert done.stdout.split() == [str(os.getuid()), str(os.getgid())]


def test_inherited_fd_survives_exec(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("s3cret")
    with secret.open() as handle:
        fd = handle.fileno()
        done = _run(f"import os; print(os.read({fd}, 10).decode())", pass_fds=(fd,))
    assert done.stdout.strip() == "s3cret", done.stderr


def test_controlling_terminal_survives():
    pid, master = pty.fork()
    if pid == 0:
        argv = _seal_net.launcher_argv("/nonexistent.sock", 18998, None, None, ())
        code = "import os; os.close(os.open('/dev/tty', os.O_RDWR)); print('tty-ok', os.isatty(0))"
        os.environ["PYTHONPATH"] = SRC
        os.execvp(argv[0], [*argv, sys.executable, "-c", code])
    chunks = b""
    while True:
        try:
            data = os.read(master, 1024)
        except OSError:
            break
        if not data:
            break
        chunks += data
    _, status = os.waitpid(pid, 0)
    assert b"tty-ok True" in chunks, chunks
    assert os.waitstatus_to_exitcode(status) == 0


def test_hide_dir_is_empty_inside_and_intact_outside(tmp_path):
    hidden = tmp_path / "gold"
    hidden.mkdir()
    (hidden / "answer.txt").write_text("42")
    done = _run(
        f"import os\nprint(sorted(os.listdir({str(hidden)!r})))\n"
        f"print(os.path.exists({str(tmp_path / 'nope')!r}))",
        hide=[str(hidden), str(tmp_path / "nope")],
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["[]", "False"]
    assert (hidden / "answer.txt").read_text() == "42"
    assert not (tmp_path / "nope").exists()


def test_hide_file_reads_as_empty_inside_and_is_intact_outside(tmp_path):
    secret = tmp_path / "answer.json"
    secret.write_text('{"fix": 42}')
    # /dev/null takes a write and discards it, read-only mount or not: the file still reads as empty.
    done = _run(
        f"import os\nprint(repr(open({str(secret)!r}).read()))\n"
        f"open({str(secret)!r}, 'w').write('overwritten')\n"
        f"print(repr(open({str(secret)!r}).read()))\n"
        f"try:\n    os.unlink({str(secret)!r})\nexcept OSError as e: print('unlink-failed')\n",
        hide=[str(secret)],
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["''", "''", "unlink-failed"]
    assert secret.read_text() == '{"fix": 42}'


def test_hide_a_file_below_a_hidden_directory_is_skipped(tmp_path):
    hidden = tmp_path / "gold"
    hidden.mkdir()
    (hidden / "a.txt").write_text("1")
    done = _run("print('ok')", hide=[str(hidden / "a.txt"), str(hidden)])
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


# What a role that wants its read-only directory back would try, all from inside the seal: remount,
# unmount, re-bind the parent to look beneath the seal's mounts, bind a writable directory over the target;
# then the same from a user and mount namespace of its own (where it is "root"). Outside such a namespace
# every call is refused (no capability after exec). Inside one the role may mount in its own private view,
# but the parent namespace's mounts are locked: `remount`/`umount` fail, and `uncover` fails because the
# kernel refuses a non-recursive bind whose source holds a locked mount (it would expose the directory
# beneath a hidden or read-only mount). `bind-over` of SOURCE succeeds there, and that is the point of
# the write that follows: it lands in SOURCE, the real TARGET is unchanged and the outer view stays EROFS.
# Every probe names a directory the test made, so no result depends on where the host puts tmp_path.
_ESCAPE = """
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
target, source = TARGET.encode(), SOURCE.encode()
parent = os.path.dirname(TARGET).encode()  # holds a mount the seal made: the kernel will not re-bind it
MS_REMOUNT, MS_BIND = 0x20, 0x1000

def attempt():
    results = {}
    results['remount'] = libc.mount(None, target, None, MS_REMOUNT | MS_BIND, None)
    results['umount'] = libc.umount2(target, 0)
    results['umount-lazy'] = libc.umount2(target, 2)
    results['uncover'] = libc.mount(parent, target, None, MS_BIND, None)
    results['bind-over'] = libc.mount(source, target, None, MS_BIND, None)
    return results

print('outer', attempt())
# The child leaves by os._exit, which drops buffered output: flush around the fork, or a host where stdout
# is block-buffered (no PYTHONUNBUFFERED, as on CI) loses the nested line. A failing child must show too.
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    code = 1
    try:
        os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
        open('/proc/self/setgroups', 'w').write('deny')
        open('/proc/self/uid_map', 'w').write('0 %d 1' % os.getuid())
        print('nested', attempt())
        open(os.path.join(TARGET, 'new.txt'), 'w').write('through the private view')
        print('nested-wrote', sorted(os.listdir(SOURCE)))  # into SOURCE, which the bind put in front
        code = 0
    except BaseException as exc:
        print('nested-failed', repr(exc))
    finally:
        sys.stdout.flush()
        os._exit(code)
status = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
if status:
    print('nested-exit', status)
try:
    open(os.path.join(TARGET, 'new.txt'), 'w')
    print('WRITABLE')
except OSError as e:
    print('still-readonly', e.errno)
"""

# Outside a namespace of its own every call is refused; inside one only its own bind goes through.
_REFUSED = dict.fromkeys(["remount", "umount", "umount-lazy", "uncover", "bind-over"], -1)
_NESTED = {**_REFUSED, "bind-over": 0}


def test_a_readonly_path_cannot_be_made_writable_from_inside(tmp_path):
    guarded = tmp_path / "guard"
    guarded.mkdir()
    (guarded / "guard.json").write_text("{}")
    source = tmp_path / "src"  # a writable directory the nested namespace may bind over the target
    source.mkdir()
    done = _run(
        f"import os\n"
        f"try:\n    open({str(guarded / 'guard.json')!r}, 'w')\n"
        "except OSError as e: print('write-failed', e.errno)\n"
        f"TARGET, SOURCE = {str(guarded)!r}, {str(source)!r}\nexec({_ESCAPE!r})",
        readonly=[str(guarded)],
        env={},
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "write-failed 30", done.stdout
    assert "WRITABLE" not in done.stdout
    assert lines[-1] == "still-readonly 30"
    assert lines[1:-1] == [f"outer {_REFUSED}", f"nested {_NESTED}", "nested-wrote ['new.txt']"]
    assert (guarded / "guard.json").read_text() == "{}"
    assert sorted(p.name for p in guarded.iterdir()) == ["guard.json"]  # the nested write never reached it
    (guarded / "later.txt").write_text("the real directory is writable outside")


def test_a_readonly_path_that_cannot_be_mounted_fails_the_launch_closed(tmp_path):
    done = _run("print('RAN')", readonly=[str(tmp_path / "missing")])
    assert done.returncode == 125
    assert "RAN" not in done.stdout
    assert "sealed launch failed" in done.stderr


def test_the_proxy_socket_on_a_readonly_mount_is_still_reachable(tmp_path):
    private = tmp_path / "priv"
    private.mkdir()
    sock = str(private / "p.sock")
    server = socketserver.ThreadingUnixStreamServer(sock, _AllowOne)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code = (
            "import urllib.request\n"
            "print(urllib.request.urlopen('http://allowed.test/hello', timeout=10).read().decode())\n"
        )
        done = _run(code, socket_path=sock, port=18994, readonly=[str(private)])
    finally:
        server.shutdown()
        server.server_close()
    assert done.stdout.splitlines() == ["hello from proxy"], done.stderr


def test_environment_is_set_for_the_command():
    done = _run(
        "import os\n"
        "names = ('HTTPS_PROXY', 'http_proxy', 'NO_PROXY', 'DISABLE_AUTOUPDATER',\n"
        "         'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'PIP_INDEX_URL', 'UV_INDEX_URL',\n"
        "         'UV_DEFAULT_INDEX', 'UV_EXCLUDE_NEWER')\n"
        "print(*[os.environ[k] for k in names])",
        port=18997,
        index_url="http://127.0.0.1:18997/index/simple/",
        exclude_newer="2025-01-01T00:00:00Z",
        env={"HTTPS_PROXY": "http://elsewhere:1"},
    )
    assert done.stdout.split() == [
        "http://127.0.0.1:18997", "http://127.0.0.1:18997", "localhost,127.0.0.1", "1", "1",
        "http://127.0.0.1:18997/index/simple/", "http://127.0.0.1:18997/index/simple/",
        "http://127.0.0.1:18997/index/simple/", "2025-01-01T00:00:00Z",
    ]  # fmt: skip


class _AllowOne(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "http://allowed.test/hello"
        body = b"hello from proxy" if ok else b"denied"
        self.send_response(200 if ok else 403)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_only_the_proxy_is_reachable_through_the_forwarder(tmp_path):
    sock = str(tmp_path / "p.sock")
    server = socketserver.ThreadingUnixStreamServer(sock, _AllowOne)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code = (
            "import urllib.request, urllib.error\n"
            "print(urllib.request.urlopen('http://allowed.test/hello', timeout=10).read().decode())\n"
            "try:\n    urllib.request.urlopen('http://other.test/x', timeout=10)\n"
            "except urllib.error.HTTPError as e: print('status', e.code)\n"
        )
        done = _run(code, socket_path=sock, port=18996)
    finally:
        server.shutdown()
        server.server_close()
    assert done.stdout.splitlines() == ["hello from proxy", "status 403"], done.stderr


def _is_gone(pid):
    # A killed process nobody reaps (a container without an init) stays as a zombie: dead for our purpose.
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        # Gone before or while we read its stat: the kernel answers ENOENT or ESRCH.
        return True


def test_forwarder_dies_with_the_command():
    code = (
        "import os\n"
        "def parent(pid):\n"
        "    try:\n        return open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()[1]\n"
        "    except OSError: return None  # exited while we looked\n"
        "me = str(os.getpid())\n"
        "print(*[d for d in os.listdir('/proc') if d.isdigit() and parent(d) == me])\n"
    )
    done = _run(code, port=18995)
    assert done.returncode == 0, done.stderr
    forwarders = [int(pid) for pid in done.stdout.split()]
    assert len(forwarders) == 1, done.stdout
    for _ in range(50):
        if _is_gone(forwarders[0]):
            return
        time.sleep(0.1)
    pytest.fail("forwarder outlived the command")


def test_filesystem_allowlist_and_private_tmp(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    # Containment only: a write may fail read-only, denied or hidden (ENOENT when $HOME is below the replaced
    # /tmp); the EROFS lock itself is asserted on a visible path by the lock test below.
    outside = Path.home() / f"seal-outside-{os.getpid()}"
    outside.mkdir()
    # A harness file in the real /tmp (not `tmp_path`, which need not lie there): the private tmp hides it.
    harness_tmp = tempfile.NamedTemporaryFile(prefix="seal-harness-secret-", dir="/tmp")
    marker = Path(harness_tmp.name)
    try:
        (outside / "existing").write_text("intact")
        (work / "outside-link").symlink_to(outside, target_is_directory=True)
        code = f"""
import errno, os, pathlib, subprocess, tempfile
work, outside = pathlib.Path({str(work)!r}), pathlib.Path({str(outside)!r})
assert not pathlib.Path({str(marker)!r}).exists()
for path in [outside / 'existing', outside / 'new', work / 'outside-link' / 'new',
             pathlib.Path('/dev/seal-escape'), pathlib.Path('/var/tmp/seal-escape')]:
    try:
        path.write_text('escaped')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.ENOENT), (path, exc)
    else:
        raise AssertionError(f'write escaped: {{path}}')
(work / 'allowed').write_text('ok')
assert pathlib.Path(tempfile.gettempdir()) == pathlib.Path('/tmp')
pathlib.Path('/tmp/seal-private').write_text('private')
pathlib.Path('/dev/shm/seal-private').write_text('private')  # shared memory is the seal's own
import multiprocessing; multiprocessing.Lock()
subprocess.run(['git', 'init', '-q', str(work / 'repo')], check=True)
subprocess.run(['git', '-C', str(work / 'repo'), '-c', 'user.name=Test', '-c',
                'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'test'], check=True)
subprocess.run([os.sys.executable, '-m', 'venv', str(work / 'venv')], check=True)
print('contained')
"""
        done = _run(code, writable=[str(work)], private_tmp="/tmp", cwd=work)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "contained"
        assert (work / "allowed").read_text() == "ok"
        assert (outside / "existing").read_text() == "intact"
        assert not (outside / "new").exists()
        assert not Path("/tmp/seal-private").exists()
        assert not Path("/dev/shm/seal-private").exists()
    finally:
        import shutil

        shutil.rmtree(outside)
        harness_tmp.close()


def test_private_tmp_does_not_carry_the_harness_tmp_when_allowlisted(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    # The file is in the real /tmp itself (`tmp_path` need not lie there), the directory being replaced.
    with tempfile.NamedTemporaryFile(prefix="seal-private-to-harness-", dir="/tmp") as harness_tmp:
        done = _run(
            f"from pathlib import Path; assert not Path({harness_tmp.name!r}).exists(); print('private')",
            writable=[str(work), "/tmp"],
            private_tmp="/tmp",
            cwd=work,
        )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "private"


def test_filesystem_lock_cannot_be_undone_or_reached_through_proc(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    # A directory the seal leaves visible but not on the allowlist, so it must be locked read-only (EROFS).
    # It must not lie below the private tmp, where it would be hidden instead (ENOENT): $HOME can be below
    # /tmp on some hosts, so neither it nor /tmp is used; the private tmp is its own directory here.
    outside = tmp_path / "outside"
    outside.mkdir()
    private = tmp_path / "private-tmp"
    private.mkdir()
    source = work / "src"  # the only writable place: what a nested namespace would bind over `outside`
    source.mkdir()
    done = _run(
        f"TARGET, SOURCE = {str(outside)!r}, {str(source)!r}\nexec({_ESCAPE!r})\n"
        f"from pathlib import Path\n"
        f"p = Path('/proc/{os.getpid()}/root') / {str(outside).lstrip('/')!r} / 'escaped'\n"
        "try: p.write_text('escape')\n"
        "except OSError: pass\n"
        "else: raise AssertionError('proc escape')\n",
        writable=[str(work)],
        private_tmp=str(private),
        cwd=work,
    )
    assert done.returncode == 0, done.stderr
    assert "WRITABLE" not in done.stdout
    assert done.stdout.splitlines() == [
        f"outer {_REFUSED}", f"nested {_NESTED}", "nested-wrote ['new.txt']", "still-readonly 30"
    ]  # fmt: skip
    assert not (outside / "escaped").exists()
    assert sorted(p.name for p in outside.iterdir()) == []  # neither the nested write nor the proc one
    assert (source / "new.txt").exists()


def test_missing_allowlist_path_fails_closed(tmp_path):
    done = _run("print('RAN')", writable=[str(tmp_path / "missing")])
    assert done.returncode == 125
    assert "RAN" not in done.stdout
    assert "sealed launch failed" in done.stderr


def test_a_symlinked_allowlist_path_fails_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "sub").mkdir()
    (tmp_path / "link").symlink_to(target, target_is_directory=True)
    for path in ["link", "link/", "link/sub"]:  # the link itself, with a trailing slash, as an ancestor
        done = _run("print('RAN')", writable=[f"{tmp_path}/{path}"])
        assert done.returncode == 125, path
        assert "RAN" not in done.stdout
        assert "symbolic link" in done.stderr


_SHM_GET = """
import ctypes, sys
libc = ctypes.CDLL(None, use_errno=True)
shm = libc.shmget(ctypes.c_int(KEY), ctypes.c_size_t(4096), ctypes.c_int(CREATE | 0o600))
"""


def _sysv_shm_ids(key):
    rows = Path("/proc/sysvipc/shm").read_text().splitlines()[1:]
    return [int(row.split()[1]) for row in rows if int(row.split()[0]) == key]


def _remove_shm(ids):
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    for shm_id in ids:
        libc.shmctl(shm_id, 0, None)  # IPC_RMID


def test_sysv_shared_memory_is_the_seals_own(tmp_path):
    key_in, key_out = 0x5EA10000 + os.getpid() % 0xFFFF, 0x5EA20000 + os.getpid() % 0xFFFF
    import ctypes

    outside = ctypes.CDLL(None, use_errno=True).shmget(key_out, 4096, 0o1000 | 0o600)  # IPC_CREAT
    assert outside >= 0
    try:
        code = (
            f"KEY, CREATE = {key_out}, 0\n{_SHM_GET}\n"
            "assert shm == -1, 'the harness segment is visible inside'\n"
            f"KEY, CREATE = {key_in}, 0o1000\n{_SHM_GET}\n"
            "assert shm >= 0, ctypes.get_errno()\nprint('own')\n"
        )
        done = _run(code, writable=[str(tmp_path)], private_tmp="/tmp", cwd=tmp_path)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "own"
        assert _sysv_shm_ids(key_in) == []
    finally:
        _remove_shm([outside, *_sysv_shm_ids(key_in)])


def test_every_mount_but_the_allowlist_is_readonly_inside(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    code = f"""
import os
# The private /tmp itself, not what is mounted below it (carried paths, which must be read-only).
exact = ['/tmp', '/dev/shm']
allowed = [os.path.realpath({str(work)!r})]
unlocked = ['/proc', '/sys', '/dev/pts']
def below(point, top):
    return point == top or point.startswith(top.rstrip('/') + '/')
writable = []
for line in open('/proc/self/mountinfo'):
    point = line.split()[4].encode().decode('unicode_escape')
    if point in exact or any(below(point, p) for p in allowed + unlocked) or not os.path.exists(point):
        continue
    if not os.statvfs(point).f_flag & os.ST_RDONLY:
        writable.append(point)
print('writable:', writable)
"""
    done = _run(code, writable=[str(work)], private_tmp="/tmp", cwd=work)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "writable: []"
