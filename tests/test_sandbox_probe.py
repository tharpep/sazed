"""Sandbox probe: what can a sandboxed command reach? Prints names, counts and booleans only."""

import json
import os
import socket
import uuid
from pathlib import Path

HOME = Path.home()
HOME_PATHS = [
    "~/.claude",
    "~/.claude.json",
    "~/.gemini",
    "~/.local",
    "~/.config",
    "~/jobs",
    "/etc/sazed-eco",
]


def _can_read(path: Path) -> bool:
    try:
        if path.is_dir():
            list(path.iterdir())
        else:
            with open(path, "rb") as f:
                f.read(1)
        return True
    except OSError:
        return False


def _proc_environ() -> tuple[int, int]:
    readable = token = 0
    needle = b"CLAUDE_CODE_OAUTH_TOKEN="
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            data = (entry / "environ").read_bytes()
        except OSError:
            continue
        readable += 1
        for item in data.split(b"\0"):
            if item.startswith(needle) and len(item) > len(needle):
                token += 1
                break
    return readable, token


def _network() -> bool:
    try:
        socket.create_connection(("api.github.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _write_home() -> bool:
    path = HOME / f".sandbox_probe_{uuid.uuid4().hex}"
    try:
        path.write_text("x")
    except OSError:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def test_sandbox_probe():
    readable, token = _proc_environ()
    results = {
        "env_token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
        "env_secret_names": sorted(
            n for n in os.environ if any(w in n.upper() for w in ("TOKEN", "KEY", "SECRET"))
        ),
        "proc_readable": readable,
        "proc_token": token,
        "home_reads": {p: _can_read(Path(p).expanduser()) for p in HOME_PATHS},
        "network": _network(),
        "write_home": _write_home(),
    }
    print(json.dumps(results))

    assert results["env_token"] is False
    assert results["proc_token"] == 0
    assert not any(results["home_reads"].values())
    assert results["network"] is False
    assert results["write_home"] is False
