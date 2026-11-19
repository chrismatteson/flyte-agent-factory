import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _seal_net, _seal_proxy
from flyte_agent_factory._seal_spec import SealSpec

SRC = str(Path(_seal_net.__file__).resolve().parent.parent)
PORT = 18990
JSON_TYPE = "application/vnd.pypi.simple.v1+json"
BIG = os.urandom(3 * 1024 * 1024)
GIT_BACKEND = (
    Path(subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip())
    / "git-http-backend"
)

SPEC = SealSpec.from_mapping(
    {
        "task_repo": "https://example.com/task.git",
        "base_sha": "a" * 40,
        "task_project": "Task_Project",
        "index_cutoff": "2025-03-01",
        "push_remote": "https://example.com/workspace.git",
    }
)


def _entry(host, filename, uploaded, **extra):
    return {
        "filename": filename,
        "url": f"http://{host}/packages/ab/cd/{filename}",
        "hashes": {"sha256": "ff" * 32},
        "requires-python": ">=3.8",
        "upload-time": uploaded,
        **extra,
    }


class _Fake(ThreadingHTTPServer):
    """A local server standing in for an outer service; records what it was sent."""

    daemon_threads = True

    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.seen: list[tuple[Any, ...]] = []
        self.responses: dict[str, Any] = {}
        self.pages: dict[str, Any] = {}
        self.root = ""

    @property
    def host(self):
        return f"127.0.0.1:{self.server_address[1]}"

    @property
    def url(self):
        return f"http://{self.host}"


def _serve(handler):
    server = _Fake(handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _PyPI(BaseHTTPRequestHandler):
    server: _Fake

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Accept")))
        project = self.path.strip("/").split("/")[-1]
        page = self.server.pages.get(project)
        if page is None:
            self.send_error(404)
            return
        body = json.dumps(page).encode()
        self.send_response(200)
        self.send_header("Content-Type", JSON_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Files(BaseHTTPRequestHandler):
    server: _Fake

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Length", str(len(BIG)))
        self.end_headers()
        self.wfile.write(BIG)

    def log_message(self, *args):
        pass


class _Git(BaseHTTPRequestHandler):
    server: _Fake

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if "chunked" in (self.headers.get("Transfer-Encoding") or ""):
            body = b""
            while size := int(self.rfile.readline().split(b";")[0], 16):
                body += self.rfile.read(size)
                self.rfile.readline()
            self.rfile.readline()
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        reply = b"receive-pack-reply"
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = do_POST = _record

    def log_message(self, *args):
        pass


@pytest.fixture
def upstreams():
    pypi, files, git = _serve(_PyPI), _serve(_Files), _serve(_Git)
    pypi.pages = {
        "otherpkg": {
            "meta": {"api-version": "1.1"},
            "name": "otherpkg",
            "files": [
                _entry(files.host, "otherpkg-1.0.tar.gz", "2024-01-01T00:00:00.000000Z"),
                _entry(files.host, "otherpkg-2.0.tar.gz", "2025-06-01T00:00:00.000000Z"),
                _entry(files.host, "otherpkg-0.5-py3-none-any.whl", "2025-03-01T00:00:00.000000Z"),
            ],
        },
        "task-project": {"name": "task-project", "files": []},
    }
    yield pypi, files, git
    for server in (pypi, files, git):
        server.shutdown()
        server.server_close()


@pytest.fixture
def sock_dir():
    path = tempfile.mkdtemp(prefix="seal")  # short: a unix socket path is limited to ~108 bytes
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def proxy(upstreams, sock_dir):
    pypi, files, git = upstreams
    log = sock_dir / "proxy.jsonl"
    with _seal_proxy.run_in_thread(
        SPEC, sock_dir / "p.sock", log,
        pypi_simple=f"{pypi.url}/simple", pypi_files=files.url, push_remote=f"{git.url}/ws.git",
        port=PORT, allow_http_upstreams_for_tests=True,
    ):  # fmt: skip
        yield sock_dir / "p.sock", log


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("127.0.0.1", PORT, timeout=20)
        self._path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(20)
        self.sock.connect(self._path)


def _request(sock, method, target, headers=None, body=None, **kw):
    conn = UnixConnection(sock)
    conn.request(method, target, body=body, headers=headers or {}, **kw)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def _log_lines(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_index_hides_files_uploaded_after_the_cutoff_and_rewrites_urls(proxy, upstreams):
    sock, log = proxy
    response, data = _request(sock, "GET", "/index/simple/OtherPkg/", {"Accept": JSON_TYPE})
    assert response.status == 200
    assert response.getheader("Content-Type") == JSON_TYPE
    page = json.loads(data)
    names = [f["filename"] for f in page["files"]]
    assert names == ["otherpkg-1.0.tar.gz", "otherpkg-0.5-py3-none-any.whl"]
    assert page["files"][0]["url"] == f"http://127.0.0.1:{PORT}/files/packages/ab/cd/otherpkg-1.0.tar.gz"
    assert page["files"][0]["hashes"]["sha256"] == "ff" * 32
    assert upstreams[0].seen == [("/simple/otherpkg/", JSON_TYPE)]
    line = _log_lines(log)[-1]
    assert (line["route"], line["decision"], line["status"]) == ("index", "allow", 200)


def test_index_serves_html_to_a_client_that_does_not_accept_json(proxy):
    sock, _ = proxy
    response, data = _request(sock, "GET", "/index/simple/otherpkg/", {"Accept": "text/html"})
    text = data.decode()
    assert response.getheader("Content-Type").startswith("text/html")
    assert (
        f'href="http://127.0.0.1:{PORT}/files/packages/ab/cd/otherpkg-1.0.tar.gz#sha256={"ff" * 32}"' in text
    )
    assert 'data-requires-python="&gt;=3.8"' in text
    assert "otherpkg-2.0" not in text
    assert ">otherpkg-0.5-py3-none-any.whl</a>" in text


def test_a_connection_that_crashes_does_not_stop_the_proxy(proxy, monkeypatch):
    sock, _ = proxy
    handle, calls = _seal_proxy.SealProxy._handle, []

    async def crash_once(self, reader, writer):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("one bad client")
        await handle(self, reader, writer)

    monkeypatch.setattr(_seal_proxy.SealProxy, "_handle", crash_once)
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
        _request(sock, "GET", "/index/simple/nothere/", {"Accept": JSON_TYPE})
    response, _ = _request(sock, "GET", "/index/simple/nothere/", {"Accept": JSON_TYPE})
    assert response.status == 404 and len(calls) == 2


def test_index_unknown_project_is_not_found(proxy):
    sock, _ = proxy
    response, _ = _request(sock, "GET", "/index/simple/nothere/", {"Accept": JSON_TYPE})
    assert response.status == 404


@pytest.mark.parametrize("spelling", ["task-project", "Task_Project", "TASK.project"])
def test_index_page_of_the_task_project_is_forbidden(proxy, upstreams, spelling):
    sock, log = proxy
    response, data = _request(sock, "GET", f"/index/simple/{spelling}/", {"Accept": JSON_TYPE})
    assert response.status == 403
    assert b"task-project" in data
    assert upstreams[0].seen == []  # refused before asking PyPI
    line = _log_lines(log)[-1]
    assert (line["route"], line["decision"], line["rule"]) == ("index", "deny", "task-project")


@pytest.mark.parametrize("suffix", ["%2F", "%2f", "%23", "%3F", "%2F%23", "%2F/"])
def test_index_page_of_the_task_project_is_forbidden_however_its_name_is_escaped(proxy, upstreams, suffix):
    sock, log = proxy
    response, _ = _request(sock, "GET", f"/index/simple/task-project{suffix}", {"Accept": JSON_TYPE})
    assert response.status == 403
    assert upstreams[0].seen == []
    line = _log_lines(log)[-1]
    assert (line["route"], line["decision"], line["rule"]) == ("index", "deny", "task-project")


def test_index_name_with_an_escaped_slash_inside_is_a_listing(proxy, upstreams):
    sock, log = proxy
    response, _ = _request(sock, "GET", "/index/simple/otherpkg%2Fx/", {"Accept": JSON_TYPE})
    assert response.status == 403
    assert upstreams[0].seen == []
    assert _log_lines(log)[-1]["rule"] == "index-listing"


def test_task_project_files_are_forbidden_and_others_stream(proxy, upstreams):
    sock, log = proxy
    for name in (
        "Task_Project-1.0-py3-none-any.whl",
        "task-project-1.0.tar.gz",
        "task_project-1.0.tar.gz.metadata",
    ):
        response, _ = _request(sock, "GET", f"/files/packages/ab/cd/{name}")
        assert response.status == 403, name
    assert upstreams[1].seen == []
    response, data = _request(
        sock, "GET", "/files/packages/ab/cd/otherpkg-1.0.tar.gz", {"Authorization": "Bearer x"}
    )
    assert response.status == 200
    assert data == BIG
    assert upstreams[1].seen == [
        ("/packages/ab/cd/otherpkg-1.0.tar.gz", None)
    ]  # credentials are not forwarded
    line = _log_lines(log)[-1]
    assert (line["route"], line["decision"], line["status"]) == ("files", "allow", 200)


def test_push_receive_pack_is_forwarded_with_authorization(proxy, upstreams):
    sock, log = proxy
    auth = {"Authorization": "Basic c2VjcmV0"}
    response, data = _request(sock, "GET", "/push/info/refs?service=git-receive-pack", auth)
    assert (response.status, data) == (200, b"receive-pack-reply")
    response, data = _request(
        sock, "POST", "/push/git-receive-pack", {**auth, "Content-Type": "x-git"}, b"PACKDATA"
    )
    assert (response.status, data) == (200, b"receive-pack-reply")
    assert upstreams[2].seen == [
        ("GET", "/ws.git/info/refs?service=git-receive-pack", "Basic c2VjcmV0", b""),
        ("POST", "/ws.git/git-receive-pack", "Basic c2VjcmV0", b"PACKDATA"),
    ]
    assert [(e["route"], e["decision"]) for e in _log_lines(log)] == [("push", "allow")] * 2
    assert "c2VjcmV0" not in log.read_text()


def test_push_with_a_chunked_body_is_reframed_and_forwarded(proxy, upstreams):
    sock, _ = proxy
    chunks = (b"A" * 1000, b"B" * 20)
    response, data = _request(
        sock, "POST", "/push/git-receive-pack", {"Content-Type": "x-git"}, iter(chunks), encode_chunked=True
    )
    assert response.status == 200
    assert upstreams[2].seen[-1][3] == b"".join(chunks)


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "/push/info/refs?service=git-upload-pack"),
        ("POST", "/push/git-upload-pack"),
        ("GET", "/push/info/refs"),
        ("GET", "/push/HEAD"),
        ("POST", "/push/git-receive-pack?x=1"),
        ("GET", "/push/../index/simple/otherpkg/"),
    ],
)
def test_everything_but_push_is_denied_on_the_push_route(proxy, upstreams, method, target):
    sock, log = proxy
    response, _ = _request(sock, method, target)
    assert response.status == 403
    assert upstreams[2].seen == []
    assert _log_lines(log)[-1]["decision"] == "deny"


def test_upload_pack_rule_is_named(proxy):
    sock, log = proxy
    _request(sock, "GET", "/push/info/refs?service=git-upload-pack")
    assert _log_lines(log)[-1]["rule"] == "upload-pack"


@pytest.mark.parametrize(
    "target",
    ["/", "/other", "http://example.com/x", "http://pypi.org/simple/requests/", "/index/", "/index/simple/"],
)
def test_other_requests_are_denied_with_a_body_naming_the_rule(proxy, target):
    sock, log = proxy
    response, data = _request(sock, "GET", target)
    assert response.status == 403
    assert b"denied by seal rule" in data
    line = _log_lines(log)[-1]
    assert line["decision"] == "deny"
    assert line["target"] == target


def test_absolute_form_request_to_the_proxys_own_route_is_served(proxy):
    sock, _ = proxy
    response, data = _request(
        sock, "GET", f"http://127.0.0.1:{PORT}/index/simple/otherpkg/", {"Accept": JSON_TYPE}
    )
    assert response.status == 200
    assert json.loads(data)["name"] == "otherpkg"


def test_allow_index_false_closes_index_and_files(upstreams, sock_dir):
    spec = SealSpec.from_mapping({**json.loads(SPEC.to_json()), "allow_index": False})
    pypi, files, git = upstreams
    with _seal_proxy.run_in_thread(
        spec, sock_dir / "p.sock", sock_dir / "l.jsonl",
        pypi_simple=f"{pypi.url}/simple", pypi_files=files.url,
        port=PORT, allow_http_upstreams_for_tests=True,
    ):  # fmt: skip
        assert _request(sock_dir / "p.sock", "GET", "/index/simple/otherpkg/")[0].status == 403
        assert _request(sock_dir / "p.sock", "GET", "/files/packages/x/otherpkg-1.0.tar.gz")[0].status == 403


def test_http_upstreams_are_refused_unless_the_test_flag_is_set(sock_dir):
    with pytest.raises(ValueError, match="https"):
        _seal_proxy.SealProxy(SPEC, sock_dir / "l", pypi_files="http://127.0.0.1:1", port=PORT)
    with pytest.raises(ValueError, match="https"):
        _seal_proxy.SealProxy(SPEC, sock_dir / "l", push_remote="http://127.0.0.1:1/w.git", port=PORT)


def _connect(sock_path, target):
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(10)
    sock.connect(str(sock_path))
    sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    return sock


def test_connect_to_a_model_host_is_tunnelled_and_others_are_denied(upstreams, sock_dir):
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)

    def echo_once():
        conn, _ = echo.accept()
        with conn:
            conn.sendall(b"echo:" + conn.recv(100))

    threading.Thread(target=echo_once, daemon=True).start()
    spec = SealSpec.from_mapping({**json.loads(SPEC.to_json()), "model_hosts": ["127.0.0.1"]})
    log = sock_dir / "l.jsonl"
    with _seal_proxy.run_in_thread(
        spec, sock_dir / "p.sock", log, port=PORT, connect_port=echo.getsockname()[1],
        pypi_files=upstreams[1].url, allow_http_upstreams_for_tests=True,
    ):  # fmt: skip
        tunnel = _connect(sock_dir / "p.sock", f"127.0.0.1:{echo.getsockname()[1]}")
        assert tunnel.recv(100).startswith(b"HTTP/1.1 200")
        tunnel.sendall(b"ping")
        assert tunnel.recv(100) == b"echo:ping"
        tunnel.close()
        for target in ("github.com:443", "127.0.0.1:22", "api.anthropic.com:443"):
            denied = _connect(sock_dir / "p.sock", target)
            assert denied.recv(200).startswith(b"HTTP/1.1 403")
            denied.close()
    echo.close()
    lines = _log_lines(log)
    assert [(e["method"], e["decision"]) for e in lines] == [("CONNECT", "allow")] + [("CONNECT", "deny")] * 3
    assert lines[0]["route"] == "model"
    assert [e["target"] for e in lines[1:]] == ["github.com:443", "127.0.0.1:22", "api.anthropic.com:443"]


def test_log_lines_have_the_documented_shape_and_no_headers(proxy):
    sock, log = proxy
    _request(sock, "GET", "/nope", {"X-Secret": "hunter2"})
    line = _log_lines(log)[-1]
    assert set(line) == {"ts", "method", "target", "route", "decision", "rule", "status"}
    assert line["ts"].endswith("Z")
    assert "hunter2" not in log.read_text()


def test_malformed_request_gets_400_and_the_server_keeps_serving(proxy):
    sock, log = proxy
    raw = socket.socket(socket.AF_UNIX)
    raw.connect(str(sock))
    raw.sendall(b"NONSENSE\r\n\r\n")
    assert raw.recv(100).startswith(b"HTTP/1.1 400")
    raw.close()
    assert _request(sock, "GET", "/nope")[0].status == 403


# --- the whole stack: launcher + proxy -----------------------------------------------------------

needs_netns = pytest.mark.usefixtures("seal_namespaces")


def _sealed(sock, code, *, cwd=None, port=PORT, index_url=None):
    argv = _seal_net.launcher_argv(str(sock), port, index_url, None, ())
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.run(
        [*argv, *code], capture_output=True, text=True, env=env, cwd=cwd, timeout=120, check=False
    )


@needs_netns
def test_a_sealed_fetch_of_the_task_project_fails_and_another_works(proxy, upstreams):
    sock, log = proxy
    code = (
        "import os, urllib.request, urllib.error\n"
        "index = os.environ['PIP_INDEX_URL']\n"
        "root = index.split('/index/')[0]\n"
        "def get(url):\n"
        "    try:\n        return urllib.request.urlopen(url, timeout=20).status\n"
        "    except urllib.error.HTTPError as e: return e.code\n"
        "    except OSError: return 'blocked'\n"
        "print('task page', get(index + 'task-project/'))\n"
        "print('task file', get(root + '/files/packages/ab/cd/task_project-1.0.tar.gz'))\n"
        "print('other page', get(index + 'otherpkg/'))\n"
        "print('other file', get(root + '/files/packages/ab/cd/otherpkg-1.0.tar.gz'))\n"
        "print('pypi direct', get('https://pypi.org/simple/task-project/'))\n"
    )
    done = _sealed(sock, [sys.executable, "-c", code], index_url=f"http://127.0.0.1:{PORT}/index/simple/")
    lines = done.stdout.splitlines()
    assert lines[:4] == ["task page 403", "task file 403", "other page 200", "other file 200"], done.stderr
    assert lines[4].startswith("pypi direct") and lines[4] != "pypi direct 200"
    decisions = [(e["route"], e["decision"]) for e in _log_lines(log)]
    assert ("none", "deny") in decisions  # pypi.org via the proxy
    assert not any(e["decision"] == "allow" and e["route"] not in ("index", "files") for e in _log_lines(log))


class _GitCGI(BaseHTTPRequestHandler):
    """Smart-HTTP for a bare repository through `git http-backend`, as an operator's git host would."""

    server: _Fake

    def _cgi(self):
        path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        env = {
            "GIT_PROJECT_ROOT": self.server.root,
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "REMOTE_USER": "tester",
            "PATH": os.environ["PATH"],
            "HOME": self.server.root,
        }
        out = subprocess.run(
            [str(GIT_BACKEND)], input=self.rfile.read(length), env=env, capture_output=True
        ).stdout
        head, _, body = out.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(": ")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers.append((name, value))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _cgi

    def log_message(self, *args):
        pass


@needs_netns
@pytest.mark.usefixtures("git_http_backend")
def test_git_push_through_the_stack_works_and_fetch_does_not(upstreams, sock_dir):
    root = sock_dir / "git"
    bare = root / "ws.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True)
    work = sock_dir / "work"
    work.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "work"]):
        subprocess.run(["git", "-C", str(work), *args], check=True, env=env)
    git_host = _serve(_GitCGI)
    git_host.root = str(root)
    try:
        log = sock_dir / "l.jsonl"
        with _seal_proxy.run_in_thread(
            SPEC, sock_dir / "p.sock", log, push_remote=f"{git_host.url}/ws.git",
            pypi_simple=f"{upstreams[0].url}/simple", pypi_files=upstreams[1].url,
            port=PORT, allow_http_upstreams_for_tests=True,
        ):  # fmt: skip
            remote = f"http://127.0.0.1:{PORT}/push/"
            script = (
                f"cd {work} && git push {remote} HEAD:refs/heads/main; echo push=$?; "
                f"git ls-remote {remote}; echo ls-remote=$?; "
                f"git fetch {remote} main; echo fetch=$?\n"
            )
            done = _sealed(sock_dir / "p.sock", ["sh", "-c", script])
            decisions = [(e["method"], e["rule"], e["decision"]) for e in _log_lines(log)]
    finally:
        git_host.shutdown()
        git_host.server_close()
    assert "push=0" in done.stdout, done.stderr
    assert "ls-remote=0" not in done.stdout
    assert "fetch=0" not in done.stdout
    pushed = subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], capture_output=True, text=True)
    assert (
        pushed.stdout.strip()
        == subprocess.run(
            ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    )
    assert ("GET", "receive-pack", "allow") in decisions
    assert ("POST", "receive-pack", "allow") in decisions
    assert ("GET", "upload-pack", "deny") in decisions


def test_push_auth_adds_the_header_outside_the_role_and_never_logs_it(upstreams, sock_dir):
    pypi, files, git = upstreams
    log, sock = sock_dir / "proxy.jsonl", sock_dir / "a.sock"
    with _seal_proxy.run_in_thread(
        SPEC, sock, log,
        pypi_simple=f"{pypi.url}/simple", pypi_files=files.url, push_remote=f"{git.url}/ws.git",
        push_auth=lambda: "Basic QVBQVE9LRU4=", port=PORT, allow_http_upstreams_for_tests=True,
    ):  # fmt: skip
        response, _ = _request(sock, "GET", "/push/info/refs?service=git-receive-pack")
        assert response.status == 200
        # What the role sends is replaced, not forwarded next to it.
        _request(sock, "GET", "/push/info/refs?service=git-receive-pack", {"Authorization": "Basic cm9sZQ=="})
    assert [seen[2] for seen in upstreams[2].seen[-2:]] == ["Basic QVBQVE9LRU4="] * 2
    assert "QVBQVE9LRU4" not in log.read_text() and "cm9sZQ" not in log.read_text()
