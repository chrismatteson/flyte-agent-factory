"""Run a command with no network except one local port that leads to the seal proxy.

`python -m flyte_agent_factory._seal_net --socket S --port P -- cmd...` moves itself into new user,
network and mount namespaces (no root: the user namespace maps the caller's own uid and gid to
themselves), brings `lo` up, starts a forwarder from `127.0.0.1:P` to the unix socket `S`, and execs
`cmd`. A unix socket is addressed by path, so it crosses the namespace boundary; nothing else does:
no route, no DNS. The proxy on `S` runs outside (`_seal_proxy`) and decides what gets through.

The command is the launcher's own process after exec, so its pid, signals, controlling terminal,
inherited file descriptors and exit status are the launcher's. The forwarder is a forked child told by
the kernel (`PR_SET_PDEATHSIG`) to die with the command. A waiting parent would instead have to relay
signals and mimic the exit status, and would keep a second process between a supervisor and the role.
"""

from __future__ import annotations

import ctypes
import fcntl
import functools
import os
import re
import signal
import socket
import struct
import sys
import threading
from collections.abc import Sequence
from typing import cast

_MS_RDONLY = 0x1
_MS_REMOUNT = 0x20
_MS_BIND = 0x1000
_MS_NOSUID = 0x2
_MS_NODEV = 0x4
_MS_REC = 0x4000
_MS_PRIVATE = 0x40000
# Linux statvfs flags, spelled out: `os` defines ST_NODEV and the rest only on Linux, and this module is
# imported on the machine running a hand deploy (macOS has no ST_NODEV), where none of the sealing code runs.
_ST_NOSUID = 0x2
_ST_NODEV = 0x4
_ST_NOEXEC = 0x8
_ST_NOATIME = 0x400
_ST_NODIRATIME = 0x800
_ST_RELATIME = 0x1000
#: (statvfs flag, mount flag) pairs an unprivileged remount must keep: a user namespace locks them.
_LOCKED_MOUNT_FLAGS = (
    (_ST_NOSUID, 0x2),
    (_ST_NODEV, 0x4),
    (_ST_NOEXEC, 0x8),
    (_ST_NOATIME, 0x400),
    (_ST_NODIRATIME, 0x800),
    (_ST_RELATIME, 0x200000),
)
#: Mounts the filesystem lock leaves alone: the kernel's own trees, and the terminals the command needs.
#: `/dev` itself is locked (a device node still opens for writing on a read-only mount).
_UNLOCKED_PREFIXES = ("/proc", "/sys", "/dev/pts")
#: Shared memory: writable on the host and seen by the harness, so the command gets a fresh one instead.
_SHM = "/dev/shm"
_PR_SET_PDEATHSIG = 1
_PROBE_FLAG = "--probe"  # enter the namespaces, bring lo up, exit 0; what `available` runs
_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1
#: The launcher's own failures exit with this, not 1, so they are not taken for the command's status.
_LAUNCH_FAILED = 125
_RELAY_CHUNK = 65536


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _write_proc(path: str, text: str) -> None:
    with open(path, "w") as handle:
        handle.write(text)


def _enter_namespaces() -> None:
    uid, gid = os.getuid(), os.getgid()
    # A fresh IPC namespace too: SysV shared memory and message queues are not files, so the filesystem
    # lock does not reach them, and the harness (same uid) could otherwise read what a role left there.
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET | os.CLONE_NEWNS | os.CLONE_NEWIPC)
    # The kernel requires setgroups=deny before an unprivileged process may write gid_map.
    _write_proc("/proc/self/setgroups", "deny")
    _write_proc("/proc/self/uid_map", f"{uid} {uid} 1")
    _write_proc("/proc/self/gid_map", f"{gid} {gid} 1")


def _bring_loopback_up() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("16sH14s", b"lo", 0, b"")
        flags = struct.unpack("16sH14s", fcntl.ioctl(probe, _SIOCGIFFLAGS, request))[1]
        fcntl.ioctl(probe, _SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", flags | _IFF_UP, b""))


def _mount(libc: ctypes.CDLL, source: bytes | None, target: bytes, fstype: bytes | None, flags: int) -> None:
    if libc.mount(source, target, fstype, flags, None) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"mount {target.decode()}: {os.strerror(errno)}")


def _bind_readonly(libc: ctypes.CDLL, source: bytes, target: bytes) -> None:
    """Bind `source` onto `target` and remount that bind read-only.

    The remount must repeat the flags the kernel locks on a bind mount inside a user namespace (nosuid,
    nodev, noexec and the atime ones), or it fails with EPERM; they are read back from the mount itself.
    The mount is locked by the parent namespace: the command, in this user namespace or one nested in it,
    can neither remount it read-write nor unmount it.
    """
    _mount(libc, source, target, None, _MS_BIND)
    current = os.statvfs(target).f_flag
    kept = sum(mount_flag for stat_flag, mount_flag in _LOCKED_MOUNT_FLAGS if current & stat_flag)
    _mount(libc, None, target, None, _MS_BIND | _MS_REMOUNT | _MS_RDONLY | kept)


def _hide_paths(paths: Sequence[str]) -> None:
    """Hide each existing path in this mount namespace only.

    A directory is covered with an empty tmpfs; a file with a read-only bind of /dev/null, so it reads as
    empty and cannot be written. Directories go first: a file below one is then gone and is skipped.
    """
    libc = _libc()
    # Without this the mounts below would propagate to the namespace we came from.
    _mount(libc, None, b"/", None, _MS_REC | _MS_PRIVATE)
    for directory in (p for p in paths if os.path.isdir(p)):
        _mount(libc, b"seal-hidden", os.fsencode(directory), b"tmpfs", 0)
    for file in (p for p in paths if os.path.lexists(p) and not os.path.isdir(p)):
        _bind_readonly(libc, b"/dev/null", os.fsencode(file))


def _make_readonly(paths: Sequence[str]) -> None:
    """Make each path read-only in this mount namespace; a path that cannot be is an error (fail closed)."""
    libc = _libc()
    for path in paths:
        _bind_readonly(libc, os.fsencode(path), os.fsencode(path))


def _below(path: str, root: str) -> bool:
    return path.startswith(root.rstrip("/") + "/")


def _private_tmp(libc: ctypes.CDLL, tmp: str, carry: Sequence[str]) -> None:
    """Replace `tmp` with a fresh tmpfs, keeping each existing path in `carry` that lies below it.

    The tmpfs is new in this mount namespace, so nothing the command writes there is visible outside, and
    it cannot reach the files the harness keeps in the real `tmp`. The paths the command needs that live
    there (its workdir, the guard's log and config, the proxy socket) are bound back at the same path,
    recursively so a hidden or read-only mount inside one stays. They are opened first: after the tmpfs
    covers `tmp` they could no longer be looked up, but a descriptor still names them.
    """
    inside = sorted({os.path.realpath(p) for p in carry if _below(os.path.realpath(p), tmp)})
    # A path below another one that is carried comes with it.
    kept = [p for i, p in enumerate(inside) if not any(_below(p, q) for q in inside[:i])]
    held = [(os.open(p, os.O_PATH), p, os.path.isdir(p)) for p in kept]
    try:
        _mount(libc, b"seal-tmp", os.fsencode(tmp), b"tmpfs", _MS_NOSUID | _MS_NODEV)
        for fd, path, is_dir in held:
            if is_dir:
                os.makedirs(path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "a").close()
            _mount(libc, f"/proc/self/fd/{fd}".encode(), os.fsencode(path), None, _MS_BIND | _MS_REC)
    finally:
        for fd, _, _ in held:
            os.close(fd)


def _mount_points() -> list[str]:
    """Return every mount point of this mount namespace, from `/proc/self/mountinfo`."""
    points = []
    with open("/proc/self/mountinfo") as handle:
        for line in handle:
            # The mount point is the fifth field; a space, tab, newline or backslash in it is escaped (\040).
            escaped = line.split(" ")[4]
            points.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), escaped))
    return points


def _lock_filesystem(writable: Sequence[str], tmp: str | None) -> None:
    """Make every mount read-only except the `writable` paths (each its own mount) and `tmp`.

    The harness runs as the same uid outside the seal and reads files a role could plant (a git config,
    a `.pth`, a shell rc file, its temp files), so a role may write only where it is told to. A writable
    path is bound onto itself, recursively (a read-only or hidden mount inside it stays), so that it is
    a mount of its own and the remount below does not reach it. Any other mount is remounted read-only
    with the flags a user namespace locks. A failing remount fails the launch (fail closed). The mounts
    are locked by the parent namespace, so the command cannot remount or unmount them; it also cannot
    make a new one: it holds no capability in the mount namespace's owning user namespace.
    """
    libc = _libc()
    for path in writable:
        # A link anywhere in a writable path (one the role swapped in, if it can write a parent) would bind
        # wherever it points, $HOME say, writable: refuse rather than follow. Callers pass resolved paths.
        if os.path.realpath(path) != os.path.normpath(os.path.abspath(path)):
            raise OSError(f"writable path is or passes through a symbolic link: {path}")
    real = sorted({os.path.realpath(p) for p in writable}, key=lambda p: p.count("/"))
    for path in real:
        _mount(libc, os.fsencode(path), os.fsencode(path), None, _MS_BIND | _MS_REC)
    skip = {*real, *([os.path.realpath(tmp)] if tmp else [])}
    if os.path.isdir(_SHM):
        _mount(libc, b"seal-shm", os.fsencode(_SHM), b"tmpfs", _MS_NOSUID | _MS_NODEV)
        skip.add(_SHM)
    for point in _mount_points():
        if point in skip or any(point == p or _below(point, p) for p in _UNLOCKED_PREFIXES):
            continue
        current = os.statvfs(point).f_flag
        if current & os.ST_RDONLY:
            continue
        kept = sum(mount_flag for stat_flag, mount_flag in _LOCKED_MOUNT_FLAGS if current & stat_flag)
        _mount(libc, None, os.fsencode(point), None, _MS_BIND | _MS_REMOUNT | _MS_RDONLY | kept)


def _relay(source: socket.socket, sink: socket.socket) -> None:
    try:
        while chunk := source.recv(_RELAY_CHUNK):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _serve_connection(client: socket.socket, socket_path: str) -> None:
    with client:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(socket_path)
        except OSError:
            upstream.close()
            return
        with upstream:
            sender = threading.Thread(target=_relay, args=(client, upstream), daemon=True)
            sender.start()
            _relay(upstream, client)
            sender.join()


def _run_forwarder(listener: socket.socket, socket_path: str) -> None:
    while True:
        client, _ = listener.accept()
        threading.Thread(target=_serve_connection, args=(client, socket_path), daemon=True).start()


def _start_forwarder(port: int, socket_path: str) -> None:
    """Fork the TCP-to-unix forwarder; it holds no descriptor but its listener and dies with the parent."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)  # listening before the fork: the command may connect the moment it starts
    parent = os.getpid()
    if os.fork() != 0:
        listener.close()
        return
    try:
        _libc().prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent:  # the parent died before prctl took effect
            os._exit(0)
        # The command's inherited descriptors (a token fd, say) must not stay open in a process it can signal.
        keep = listener.fileno()
        os.closerange(3, keep)
        os.closerange(keep + 1, os.sysconf("SC_OPEN_MAX"))
        _run_forwarder(listener, socket_path)
    finally:
        os._exit(1)


def _probe() -> str:
    """Try the namespaces as a role's launch does; return "" when they work, else the launcher's error.

    A fresh process, not a fork: the caller is usually non-dumpable (`_auth.set_non_dumpable`), which makes
    a forked child's `/proc/self` files root-owned so that it cannot write its own uid map, while the exec
    that starts a role's launcher resets that. A forked probe would refuse a seal the launch can make.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "flyte_agent_factory._seal_net", _PROBE_FLAG],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"probe did not run: {error}"
    if proc.returncode == 0:
        return ""
    return (proc.stderr.strip() or f"probe exited with status {proc.returncode}")[-500:]


@functools.cache
def unavailable_reason() -> str:
    """Why this process may not create the namespaces the seal needs, or "" when it may (probed once)."""
    return _probe()


def available() -> bool:
    """Whether this process may create the namespaces the seal needs (probed once, in a forked child)."""
    return not unavailable_reason()


def launcher_argv(
    socket_path: str,
    port: int,
    index_url: str | None = None,
    exclude_newer: str | None = None,
    hide: Sequence[str] = (),
    readonly: Sequence[str] = (),
    writable: Sequence[str] = (),
    private_tmp: str | None = None,
) -> list[str]:
    """Return the argv that runs a command sealed: append the command's own argv to it.

    With `writable` or `private_tmp` the filesystem is locked: read-only everywhere but those paths (see
    `_lock_filesystem`); `private_tmp` is a fresh tmpfs over that directory and is writable too.
    """
    argv = [
        sys.executable,
        "-m",
        "flyte_agent_factory._seal_net",
        "--socket",
        str(socket_path),
        "--port",
        str(port),
    ]
    if index_url:
        argv += ["--index-url", index_url]
    if exclude_newer:
        argv += ["--exclude-newer", exclude_newer]
    for directory in hide:
        argv += ["--hide", str(directory)]
    for path in readonly:
        argv += ["--readonly", str(path)]
    for path in writable:
        argv += ["--writable", str(path)]
    if private_tmp:
        argv += ["--private-tmp", str(private_tmp)]
    return [*argv, "--"]


def _sealed_environment(
    port: int, index_url: str | None, exclude_newer: str | None, private_tmp: str | None = None
) -> dict[str, str]:
    proxy = f"http://127.0.0.1:{port}"
    env = {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    if private_tmp:
        env["TMPDIR"] = private_tmp
    if index_url:
        env.update(PIP_INDEX_URL=index_url, UV_INDEX_URL=index_url, UV_DEFAULT_INDEX=index_url)
    if exclude_newer:
        env["UV_EXCLUDE_NEWER"] = exclude_newer
    return env


def _parse(argv: Sequence[str]) -> tuple[dict[str, object], list[str]]:
    if "--" not in argv:
        raise ValueError(
            "usage: _seal_net --socket S --port P [--index-url U] [--exclude-newer D] [--hide PATH]... "
            "[--readonly PATH]... [--writable PATH]... [--private-tmp DIR] "
            "-- cmd..."
        )
    split = list(argv).index("--")
    command = list(argv[split + 1 :])
    if not command:
        raise ValueError("no command after --")
    options: dict[str, object] = {
        "index_url": None,
        "exclude_newer": None,
        "hide": [],
        "readonly": [],
        "writable": [],
        "private_tmp": None,
    }
    names = {
        "--socket": "socket",
        "--port": "port",
        "--index-url": "index_url",
        "--exclude-newer": "exclude_newer",
        "--private-tmp": "private_tmp",
    }
    words = list(argv[:split])
    while words:
        flag = words.pop(0)
        if not words:
            raise ValueError(f"{flag} needs a value")
        value = words.pop(0)
        if flag in {"--hide", "--readonly", "--writable"}:
            cast(list[str], options[flag[2:]]).append(value)
        elif flag in names:
            options[names[flag]] = value
        else:
            raise ValueError(f"unknown option {flag}")
    if "socket" not in options or "port" not in options:
        raise ValueError("--socket and --port are required")
    options["port"] = int(options["port"])  # type: ignore[call-overload]
    return options, command


def _lock_if_asked(options: dict[str, object]) -> None:
    """Apply the filesystem lock when the caller named a writable path or a private tmp."""
    writable = cast(list[str], options["writable"])
    tmp = cast("str | None", options["private_tmp"])
    if not writable and not tmp:
        return
    # A working directory below a mount that is replaced or remounted still names the old, now hidden or
    # read-only, directory: go back to the same path in the new view.
    cwd = os.getcwd()
    if tmp:
        socket_dir = os.path.dirname(str(options["socket"]))
        # The interpreter this launcher runs on (a venv below `tmp` is possible) and what the command is
        # found and imported from must stay reachable too; the lock makes them read-only.
        search = [
            p
            for var in ("PYTHONPATH", "PATH")
            for p in os.environ.get(var, "").split(os.pathsep)
            if p and os.path.exists(p)
        ]
        hidden = [p for p in cast(list[str], options["hide"]) if os.path.lexists(p)]
        carry = [*hidden, *cast(list[str], options["readonly"]), *writable, socket_dir, sys.prefix, *search]
        _private_tmp(_libc(), tmp, [p for p in carry if p])
    _lock_filesystem(writable, tmp)
    os.chdir(cwd)


def main(argv: Sequence[str] | None = None) -> int:
    """Enter the namespaces and exec the command; return only if that fails."""
    if list(sys.argv[1:] if argv is None else argv) == [_PROBE_FLAG]:
        try:
            _enter_namespaces()
            _bring_loopback_up()
        except OSError as error:
            print(f"sealed launch failed: {error}", file=sys.stderr)
            return _LAUNCH_FAILED
        return 0
    try:
        options, command = _parse(sys.argv[1:] if argv is None else argv)
    except ValueError as error:
        print(f"sealed launch failed: {error}", file=sys.stderr)
        return _LAUNCH_FAILED
    try:
        _enter_namespaces()
        _bring_loopback_up()
        _hide_paths(options["hide"])  # type: ignore[arg-type]
        _make_readonly(options["readonly"])  # type: ignore[arg-type]
        _lock_if_asked(options)
        _start_forwarder(options["port"], str(options["socket"]))  # type: ignore[arg-type]
    except OSError as error:
        # Never fall back to running the command unsealed.
        print(f"sealed launch failed: {error}", file=sys.stderr)
        return _LAUNCH_FAILED
    env = _sealed_environment(
        options["port"],  # type: ignore[arg-type]
        options["index_url"],  # type: ignore[arg-type]
        options["exclude_newer"],  # type: ignore[arg-type]
        options["private_tmp"],  # type: ignore[arg-type]
    )
    os.environ.update(env)
    try:
        os.execvp(command[0], command)
    except OSError as error:
        print(f"sealed launch failed: cannot run {command[0]}: {error}", file=sys.stderr)
        return _LAUNCH_FAILED


if __name__ == "__main__":
    sys.exit(main())
