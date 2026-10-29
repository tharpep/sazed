"""Sandbox probe (plans/10 P1.11): what can a sandboxed command reach?

Never prints or stores a secret value: only names, paths, counts and booleans.
"""

import json
import os
import socket
import stat
import struct
import uuid
from pathlib import Path
from urllib.parse import urlparse

PROXY_VARS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]
HOME_PATHS = ["~/.claude", "~/.claude.json", "~/.gemini", "~/.local", "~/.config", "~/jobs"]
HOME_PATHS.append("/etc/sazed-eco")
TARGETS = [
    ("example.com", 443),
    ("192.168.50.155", 8000),
    ("192.168.50.63", 8000),
    ("192.168.50.205", 5432),
    ("192.168.50.111", 8006),
    ("sazed.tail77c590.ts.net", 443),
    ("127.0.0.1", 8000),
    ("localhost", 22),
]
TIMEOUT = 3


def _can_open(path: Path) -> bool:
    try:
        if path.is_file():
            with open(path, "rb"):
                return True
            return False
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    with open(os.path.join(root, name), "rb"):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def _proc_environ() -> tuple[int, int]:
    readable = token = 0
    needle = b"CLAUDE_CODE_OAUTH_TOKEN="
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as f:
                data = f.read()
        except OSError:
            continue
        readable += 1
        for item in data.split(b"\0"):
            if item.startswith(needle) and len(item) > len(needle):
                token += 1
                break
    return readable, token


def _find_sockets() -> list[str]:
    found = []
    for root, dirs, files in os.walk("/"):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in ("/proc", "/sys")]
        for name in dirs + files:
            path = os.path.join(root, name)
            try:
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    found.append(path)
            except OSError:
                continue
    return found


def _unix_connect(path: str) -> socket.socket | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(path)
        return s
    except OSError:
        s.close()
        return None


def _recv(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


def _socks5(s: socket.socket, host: str, port: int) -> bool:
    s.sendall(b"\x05\x01\x00")
    if _recv(s, 2) != b"\x05\x00":
        return False
    h = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
    head = _recv(s, 4)
    return head[0] == 5 and head[1] == 0


def _http_connect(s: socket.socket, host: str, port: int) -> bool:
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    line = s.recv(256).split(b"\r\n", 1)[0].split()
    return len(line) >= 2 and line[1].startswith(b"2")


def _direct(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def _via_proxy(proxy_url: str, host: str, port: int) -> bool:
    u = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    try:
        with socket.create_connection((u.hostname, u.port), timeout=TIMEOUT) as s:
            s.settimeout(TIMEOUT)
            if u.scheme.startswith("socks"):
                return _socks5(s, host, port)
            return _http_connect(s, host, port)
    except OSError:
        return False


def _via_unix_socks(path: str, host: str, port: int) -> bool:
    s = _unix_connect(path)
    if s is None:
        return False
    try:
        return _socks5(s, host, port)
    except OSError:
        return False
    finally:
        s.close()


def _redact(value: str) -> str:
    """Drop any user:password@ part so proxy credentials are never printed."""
    if "@" not in value:
        return value
    scheme, sep, rest = value.partition("://")
    return f"{scheme}{sep}<redacted>@{rest.rsplit('@', 1)[1]}"


def test_sandbox_probe():
    env = os.environ
    raw_proxy = {k: env[k] for k in PROXY_VARS if k in env}
    raw_proxy.update({k: env[k] for k in (v.lower() for v in PROXY_VARS) if k in env})
    proxy_env = {k: _redact(v) for k, v in raw_proxy.items()}
    result: dict = {
        "env_token": "CLAUDE_CODE_OAUTH_TOKEN" in env,
        "env_secret_names": sorted(
            k for k in env if any(w in k.upper() for w in ("TOKEN", "KEY", "SECRET"))
        ),
        "proxy_env": proxy_env,
    }

    readable, token = _proc_environ()
    result["proc_readable"] = readable
    result["proc_token"] = token

    result["home_reads"] = {p: _can_open(Path(p).expanduser()) for p in HOME_PATHS}

    sockets = _find_sockets()
    connected = []
    for path in sockets:
        s = _unix_connect(path)
        if s is not None:
            connected.append(path)
            s.close()
    try:
        with open("/proc/net/unix") as f:
            proc_net_unix = max(len(f.readlines()) - 1, 0)
    except OSError:
        proc_net_unix = None
    result["sockets"] = {"found": sockets, "connected": connected}
    result["proc_net_unix"] = proc_net_unix

    proxy_url = next((raw_proxy[k] for k in raw_proxy if k.upper() != "NO_PROXY"), None)
    socks_paths = [p for p in connected if "cc-socks" in p or "srt" in p]
    egress = {}
    for host, port in TARGETS:
        key = f"{host}:{port}"
        row = {"direct": _direct(host, port)}
        if proxy_url:
            row["proxy"] = _via_proxy(proxy_url, host, port)
        for p in socks_paths:
            row[f"unix:{p}"] = _via_unix_socks(p, host, port)
        egress[key] = row
    result["egress"] = egress

    probe = Path("~").expanduser() / f".probe-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        write_home = True
    except OSError:
        write_home = False
    result["write_home"] = write_home

    print(json.dumps(result, sort_keys=True))

    assert result["env_token"] is False
    assert result["proc_token"] == 0
    assert not any(result["home_reads"].values())
    assert all("cc-socks" in p or "srt" in p for p in connected)
    assert not any(v for row in egress.values() for v in row.values())
    assert result["write_home"] is False
