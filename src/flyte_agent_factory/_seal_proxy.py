"""The one way out of a sealed run: a filtering proxy on a unix socket, outside the network namespace.

`_seal_net` forwards `127.0.0.1:P` inside the namespace to this server's socket. Allowed here, and only
here: `CONNECT` to the model hosts, a date-frozen read-only view of the PyPI index with the task project
removed, the package files it links to, and `git push` (never fetch) to the run's own workspace repo.
Everything else gets a 403 naming the rule, and every request leaves one line in the role's log.

Index pages are fetched with `urllib` (small, buffered, parsed). Package files and git traffic are
relayed over asyncio streams instead of `http.client` in a thread: a pack can be large, a git push body
is often chunked, and a stream relays both without buffering or a thread per request. Certificates are
verified (`ssl.create_default_context()`); the client's request is re-framed, never passed on raw, so a
second request cannot ride in a body past the checks. Every response closes the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from ._seal_shell import normalize_project
from ._seal_spec import SealSpec

logger = logging.getLogger(__name__)

PYPI_SIMPLE = "https://pypi.org/simple"
PYPI_FILES = "https://files.pythonhosted.org"
_JSON_TYPE = "application/vnd.pypi.simple.v1+json"
_HEAD_LIMIT = 1 << 16
_CHUNK = 1 << 16
_IDLE_SECONDS = 120
_UPSTREAM_SECONDS = 30
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate",
    "te", "trailer", "upgrade", "expect", "host",
}  # fmt: skip
_PUSH_SERVICE = "service=git-receive-pack"
_NO_BODY_STATUS = {204, 304}


class _BadRequestError(Exception):
    """The client's request could not be parsed or framed."""


@dataclass(frozen=True)
class _Request:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]

    def header(self, name: str) -> str:
        for key, value in self.headers:
            if key == name:
                return value
        return ""


@dataclass(frozen=True)
class _Upstream:
    ssl_context: ssl.SSLContext | None
    host: str
    port: int
    base_path: str

    @classmethod
    def parse(cls, url: str, *, allow_http: bool) -> _Upstream:
        parts = urlsplit(url)
        if parts.scheme == "https":
            return cls(
                ssl.create_default_context(), parts.hostname or "", parts.port or 443, parts.path.rstrip("/")
            )
        if parts.scheme == "http" and allow_http:
            return cls(None, parts.hostname or "", parts.port or 80, parts.path.rstrip("/"))
        raise ValueError(f"seal proxy upstream must be an https URL, got {url!r}")


@dataclass(frozen=True)
class _Verdict:
    route: str
    decision: str
    rule: str


def _project_of_filename(filename: str) -> str:
    """Return the normalised project a wheel, sdist or `.metadata` file name belongs to."""
    filename = filename.removesuffix(".metadata")
    if filename.endswith((".whl", ".egg")):
        return normalize_project(filename.split("-")[0])
    stem = re.sub(r"\.(tar\.gz|tar\.bz2|tar\.xz|tgz|zip)$", "", filename)
    return normalize_project(stem.rpartition("-")[0] or stem)


def _parse_head(raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    lines = raw.decode("latin-1").split("\r\n")
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise _BadRequestError(f"malformed header line {line!r}")
        headers.append((name.strip().lower(), value.strip()))
    return lines[0], headers


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _IDLE_SECONDS)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
        raise _BadRequestError("incomplete or oversized message head") from None


async def _copy_exact(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: int) -> None:
    while count > 0:
        chunk = await reader.read(min(count, _CHUNK))
        if not chunk:
            raise asyncio.IncompleteReadError(b"", count)
        writer.write(chunk)
        await writer.drain()
        count -= len(chunk)


async def _copy_chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        line = await reader.readuntil(b"\r\n")
        writer.write(line)
        size = int(line.split(b";")[0], 16)
        if size == 0:
            while (trailer := await reader.readuntil(b"\r\n")) != b"\r\n":
                writer.write(trailer)
            writer.write(b"\r\n")
            await writer.drain()
            return
        await _copy_exact(reader, writer, size + 2)


async def _copy_body(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    headers: dict[str, str],
    *,
    until_eof: bool,
) -> None:
    """Relay one message body by its own framing; `until_eof` is for a response that declares none."""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        await _copy_chunked(reader, writer)
    elif "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError:
            raise _BadRequestError("bad content-length") from None
        await _copy_exact(reader, writer, length)
    elif until_eof:
        while chunk := await reader.read(_CHUNK):
            writer.write(chunk)
            await writer.drain()


def _forward_headers(headers: tuple[tuple[str, str], ...], drop: set[str]) -> bytes:
    return b"".join(f"{name}: {value}\r\n".encode("latin-1") for name, value in headers if name not in drop)


class SealProxy:
    """The filtering proxy; build one per role, `start` it on a unix socket, `stop` it when the role ends."""

    def __init__(
        self,
        spec: SealSpec,
        log_path: Path,
        *,
        pypi_simple: str = PYPI_SIMPLE,
        pypi_files: str = PYPI_FILES,
        push_remote: str | None = None,
        push_auth: Callable[[], str] | None = None,
        port: int,
        connect_port: int = 443,
        allow_http_upstreams_for_tests: bool = False,
    ) -> None:
        """`port` is where the role reaches the proxy (the forwarder's port); it is written into file URLs.

        `push_auth` returns the `Authorization` header value for the push route. The proxy runs outside the
        role, so the push credential never enters it: the role's own `Authorization` header is dropped there
        and this one is added, and it is not logged. Without it the client's header passes through.

        `connect_port` is the one port CONNECT may target. `allow_http_upstreams_for_tests` lets the three
        upstreams be plain http, for fake servers; the spec can never switch it on.
        """
        self._spec = spec
        self._log_path = Path(log_path)
        self._port = port
        self._connect_port = connect_port
        self._cutoff = datetime.fromisoformat(spec.index_cutoff)
        self._simple = pypi_simple.rstrip("/")
        self._files = _Upstream.parse(pypi_files, allow_http=allow_http_upstreams_for_tests)
        self._files_base = pypi_files.rstrip("/")
        remote = spec.push_remote if push_remote is None else push_remote
        self._push = _Upstream.parse(remote, allow_http=allow_http_upstreams_for_tests) if remote else None
        self._push_auth = push_auth
        if not allow_http_upstreams_for_tests:
            _Upstream.parse(pypi_simple, allow_http=False)
        self._own_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._log: Any = None

    async def start(self, socket_path: str | Path) -> None:
        """Listen on `socket_path` (replacing a stale socket file)."""
        Path(socket_path).unlink(missing_ok=True)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self._log_path.open("a", encoding="utf-8")
        self._server = await asyncio.start_unix_server(self._serve, path=str(socket_path), limit=_HEAD_LIMIT)

    async def stop(self) -> None:
        """Stop listening, drop open connections and close the log."""
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def _record(self, method: str, target: str, verdict: _Verdict, status: int) -> None:
        line = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "method": method,
            "target": target,
            "route": verdict.route,
            "decision": verdict.decision,
            "rule": verdict.rule,
            "status": status,
        }
        self._log.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._log.flush()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        # An error escaping one connection ends only that connection's task: asyncio reports it through the
        # loop's exception handler ("Unhandled exception in client_connected_cb") and keeps serving.
        try:
            await self._handle(reader, writer)
        finally:
            self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line, headers = _parse_head(await _read_head(reader))
            method, target, _ = request_line.split(" ", 2)
        except (_BadRequestError, ValueError) as error:
            verdict = _Verdict("none", "deny", "bad-request")
            await self._deny(writer, "?", "?", verdict, 400, str(error))
            return
        request = _Request(method.upper(), target, tuple(headers))
        if request.method == "CONNECT":
            await self._connect(reader, writer, request)
            return
        path, _, query = self._origin_form(request.target).partition("?")
        if path is None or not path.startswith("/") or ".." in unquote(path).split("/"):
            denied = _Verdict("none", "deny", "plain-http" if "://" in request.target else "bad-path")
            await self._deny(writer, request.method, request.target, denied, 403)
        elif path.startswith("/index/"):
            await self._index(writer, request, path)
        elif path.startswith("/files/"):
            await self._files_route(reader, writer, request, path, query)
        elif path.startswith("/push/"):
            await self._push_route(reader, writer, request, path, query)
        else:
            await self._deny(
                writer, request.method, request.target, _Verdict("none", "deny", "unknown-route"), 403
            )

    def _origin_form(self, target: str) -> str:
        """Return the path and query of `target`; an absolute URL only if it names this proxy's own port."""
        if "://" not in target:
            return target
        parts = urlsplit(target)
        if parts.scheme == "http" and parts.netloc in self._own_hosts:
            return parts.path + (f"?{parts.query}" if parts.query else "")
        return "http://"  # not a path: caller denies it

    async def _deny(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        verdict: _Verdict,
        status: int,
        detail: str = "",
    ) -> None:
        await self._respond(
            writer, method, target, verdict, status, f"denied by seal rule: {verdict.rule}\n{detail}".encode()
        )

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        verdict: _Verdict,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
        }.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        self._record(method, target, verdict, status)
        writer.write(head.encode() + (b"" if method == "HEAD" else body))
        with contextlib.suppress(OSError):
            await writer.drain()

    # --- CONNECT ---------------------------------------------------------------------------------

    async def _connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: _Request
    ) -> None:
        host, _, port_text = request.target.rpartition(":")
        host = host.lower().strip("[]")
        if host not in self._spec.model_hosts:
            verdict = _Verdict("none", "deny", "connect-host-not-allowed")
        elif port_text != str(self._connect_port):
            verdict = _Verdict("none", "deny", "connect-port-not-allowed")
        else:
            verdict = _Verdict("model", "allow", "model-host")
        if verdict.decision == "deny":
            await self._deny(writer, "CONNECT", request.target, verdict, 403)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, self._connect_port), _UPSTREAM_SECONDS
            )
        except (OSError, TimeoutError):
            await self._respond(writer, "CONNECT", request.target, verdict, 502, b"upstream unreachable\n")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        self._record("CONNECT", request.target, verdict, 200)
        try:
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            upstream_writer.close()

    # --- PyPI index ------------------------------------------------------------------------------

    async def _index(self, writer: asyncio.StreamWriter, request: _Request, path: str) -> None:
        rest = path.removeprefix("/index/").removeprefix("simple/").strip("/")
        if request.method != "GET":
            await self._deny(writer, request.method, request.target, _Verdict("index", "deny", "method"), 405)
            return
        if not self._spec.allow_index:
            await self._deny(
                writer, request.method, request.target, _Verdict("index", "deny", "index-disabled"), 403
            )
            return
        # Decide on the name that is looked up, so `<task>%2F` and `<task>%23` are the task project too:
        # the literal-`/` check used to run before `unquote`, and the task-project check after it.
        name = re.split(r"[?#]", unquote(rest))[0].strip("/")
        if not name or "/" in name:
            await self._deny(
                writer, request.method, request.target, _Verdict("index", "deny", "index-listing"), 403
            )
            return
        project = normalize_project(name)
        if project == self._spec.normalized_project():
            await self._deny(
                writer, request.method, request.target, _Verdict("index", "deny", "task-project"), 403
            )
            return
        verdict = _Verdict("index", "allow", "index-frozen")
        try:
            page = await asyncio.get_running_loop().run_in_executor(None, self._fetch_index, project)
        except urllib.error.HTTPError as error:
            await self._respond(
                writer, request.method, request.target, verdict, error.code, b"upstream index error\n"
            )
            return
        except (OSError, ValueError):
            await self._respond(
                writer, request.method, request.target, verdict, 502, b"upstream index unreachable\n"
            )
            return
        files = self._visible_files(page["files"], project)
        if _JSON_TYPE in request.header("accept") or "vnd.pypi.simple.latest+json" in request.header(
            "accept"
        ):
            document = {"meta": {"api-version": "1.1"}, "name": page.get("name", project), "files": files}
            body, content_type = json.dumps(document).encode(), _JSON_TYPE
        else:
            body, content_type = (
                _html_page(page.get("name", project), files).encode(),
                "text/html; charset=utf-8",
            )
        await self._respond(writer, request.method, request.target, verdict, 200, body, content_type)

    def _fetch_index(self, project: str) -> dict[str, Any]:
        url = f"{self._simple}/{project}/"
        # An empty ProxyHandler: this process may inherit proxy variables meant for the role, not for us.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(url, headers={"Accept": _JSON_TYPE})
        with opener.open(request, timeout=_UPSTREAM_SECONDS) as response:
            page = json.load(response)
        if not isinstance(page, dict) or not isinstance(page.get("files"), list):
            raise ValueError("upstream index page is not PEP 691 JSON")
        page["_url"] = url
        return page

    def _visible_files(self, files: list[dict[str, Any]], project: str) -> list[dict[str, Any]]:
        """Files uploaded by the cutoff, with URLs pointing at this proxy; anything unclear is dropped."""
        visible = []
        base = f"http://127.0.0.1:{self._port}/files/"
        files_host = urlsplit(self._files_base).netloc
        for entry in files:
            try:
                uploaded = datetime.fromisoformat(entry["upload-time"])
                if uploaded.tzinfo is None:
                    uploaded = uploaded.replace(tzinfo=UTC)
                url = urlsplit(urljoin(f"{self._simple}/{project}/", entry["url"]))
                filename = entry["filename"]
            except (KeyError, ValueError, TypeError):
                continue  # no usable upload-time (or url): fail closed
            if uploaded > self._cutoff or url.netloc != files_host:
                continue
            if _project_of_filename(filename) == self._spec.normalized_project():
                continue
            prefix = urlsplit(self._files_base).path.rstrip("/")
            visible.append({**entry, "url": base + url.path.removeprefix(prefix).lstrip("/")})
        return visible

    # --- package files and git push: relayed ----------------------------------------------------

    async def _files_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _Request,
        path: str,
        query: str,
    ) -> None:
        if request.method not in ("GET", "HEAD"):
            await self._deny(writer, request.method, request.target, _Verdict("files", "deny", "method"), 405)
        elif not self._spec.allow_index:
            await self._deny(
                writer, request.method, request.target, _Verdict("files", "deny", "index-disabled"), 403
            )
        elif _project_of_filename(unquote(path.rsplit("/", 1)[-1])) == self._spec.normalized_project():
            await self._deny(
                writer, request.method, request.target, _Verdict("files", "deny", "task-project"), 403
            )
        else:
            rest = path.removeprefix("/files/")
            await self._relay(
                reader, writer, request, self._files, rest, query, _Verdict("files", "allow", "package-file"),
                drop=_HOP_BY_HOP | {"authorization", "cookie"},
            )  # fmt: skip

    async def _push_route(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _Request,
        path: str,
        query: str,
    ) -> None:
        rest = path.removeprefix("/push/")
        if "git-upload-pack" in path or "git-upload-pack" in query:
            denied = _Verdict("push", "deny", "upload-pack")
        elif self._push is None:
            denied = _Verdict("push", "deny", "push-disabled")
        elif not (
            (request.method == "GET" and rest == "info/refs" and query == _PUSH_SERVICE)
            or (request.method == "POST" and rest == "git-receive-pack" and not query)
        ):
            denied = _Verdict("push", "deny", "push-only")
        else:
            drop, extra = _HOP_BY_HOP, b""
            if self._push_auth is not None:
                # In a thread: the callable may read a token file that another task replaces.
                extra = f"Authorization: {await asyncio.to_thread(self._push_auth)}\r\n".encode("latin-1")
                drop = _HOP_BY_HOP | {"authorization"}
            await self._relay(
                reader, writer, request, self._push, rest, query, _Verdict("push", "allow", "receive-pack"),
                drop=drop, extra_headers=extra,
            )  # fmt: skip
            return
        await self._deny(writer, request.method, request.target, denied, 403)

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _Request,
        upstream: _Upstream,
        rest: str,
        query: str,
        verdict: _Verdict,
        *,
        drop: set[str],
        extra_headers: bytes = b"",
    ) -> None:
        """Send `request` to `upstream` re-framed, and stream the response back; close both afterwards."""
        recorded = False
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    upstream.host, upstream.port, ssl=upstream.ssl_context, limit=_HEAD_LIMIT
                ),
                _UPSTREAM_SECONDS,
            )
        except (OSError, TimeoutError):
            await self._respond(
                writer, request.method, request.target, verdict, 502, b"upstream unreachable\n"
            )
            return
        try:
            target = f"{upstream.base_path}/{rest}" + (f"?{query}" if query else "")
            host = upstream.host if upstream.port in (80, 443) else f"{upstream.host}:{upstream.port}"
            head = f"{request.method} {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
            up_writer.write(head.encode() + _forward_headers(request.headers, drop) + extra_headers + b"\r\n")
            await up_writer.drain()
            headers = dict(request.headers)
            if "expect" in headers:
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            await _copy_body(reader, up_writer, headers, until_eof=False)
            await up_writer.drain()
            status_line, response_headers = _parse_head(await _read_head(up_reader))
            status = int(status_line.split(" ", 2)[1])
            self._record(request.method, request.target, verdict, status)  # before the body: it can be large
            recorded = True
            writer.write(status_line.encode("latin-1") + b"\r\n")
            writer.write(
                _forward_headers(tuple(response_headers), _HOP_BY_HOP) + b"Connection: close\r\n\r\n"
            )
            if request.method != "HEAD" and status >= 200 and status not in _NO_BODY_STATUS:
                await _copy_body(up_reader, writer, dict(response_headers), until_eof=True)
            await writer.drain()
        except (
            _BadRequestError,
            ValueError,
            IndexError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            logger.warning("seal proxy: malformed message relaying %s %s", request.method, request.target)
        except (OSError, TimeoutError):
            logger.warning("seal proxy: connection lost relaying %s %s", request.method, request.target)
        finally:
            up_writer.close()
            if not recorded:
                self._record(request.method, request.target, verdict, 502)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_CHUNK):
            writer.write(chunk)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


def _html_page(name: str, files: list[dict[str, Any]]) -> str:
    """Return a minimal PEP 503 page with the same links, hashes and `data-requires-python` as the JSON."""
    rows = []
    for entry in files:
        url = entry["url"]
        sha256 = entry.get("hashes", {}).get("sha256")
        attributes = [f'href="{html.escape(url + (f"#sha256={sha256}" if sha256 else ""))}"']
        if entry.get("requires-python"):
            attributes.append(f'data-requires-python="{html.escape(entry["requires-python"])}"')
        yanked = entry.get("yanked")
        if yanked:
            attributes.append(f'data-yanked="{html.escape(yanked if isinstance(yanked, str) else "")}"')
        metadata = entry.get("core-metadata") or entry.get("dist-info-metadata")
        if isinstance(metadata, dict) and metadata.get("sha256"):
            attributes.append(f'data-core-metadata="sha256={html.escape(metadata["sha256"])}"')
        elif metadata:
            attributes.append('data-core-metadata="true"')
        rows.append(f"<a {' '.join(attributes)}>{html.escape(entry['filename'])}</a><br/>")
    title = html.escape(f"Links for {name}")
    return (
        f'<!DOCTYPE html>\n<html><head><meta name="pypi:repository-version" content="1.1">'
        f"<title>{title}</title></head><body><h1>{title}</h1>\n" + "\n".join(rows) + "\n</body></html>\n"
    )


@contextlib.contextmanager
def run_in_thread(
    spec: SealSpec, socket_path: str | Path, log_path: Path, **kwargs: Any
) -> Iterator[SealProxy]:
    """Run a `SealProxy` on its own event-loop thread for synchronous callers; stops it on exit."""
    proxy = SealProxy(spec, log_path, **kwargs)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    failure: list[BaseException] = []

    def serve() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(proxy.start(socket_path))
        except OSError as error:
            failure.append(error)
            ready.set()
            return
        ready.set()
        loop.run_forever()
        loop.run_until_complete(proxy.stop())
        loop.close()

    thread = threading.Thread(target=serve, name="seal-proxy", daemon=True)
    thread.start()
    ready.wait()
    if failure:
        thread.join()
        raise failure[0]
    try:
        yield proxy
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        Path(socket_path).unlink(missing_ok=True)
