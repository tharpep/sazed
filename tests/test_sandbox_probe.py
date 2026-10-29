"""Sandbox probe: what can a sandboxed command reach? Prints names, counts and booleans only."""

import json
import os
import socket
from pathlib import Path

TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _env_checks() -> dict:
    return {
        "env_token": TOKEN_VAR in os.environ,
        "env_secret_names": sorted(
            name for name in os.environ if any(w in name for w in ("TOKEN", "KEY", "SECRET"))
        ),
    }


def _proc_token() -> dict:
    readable = 0
    with_token = 0
    needle = TOKEN_VAR.encode() + b"="
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
                with_token += 1
                break
    return {"proc_environ_readable": readable, "proc_token": with_token}


def _can_access(path: Path) -> bool:
    try:
        if path.is_dir():
            next(iter(path.iterdir()), None)
        else:
            with open(path, "rb") as f:
                f.read(0)
        return True
    except OSError:
        return False


def _home_reads() -> dict:
    home = Path.home()
    paths = {
        "~/.claude": home / ".claude",
        "~/.claude.json": home / ".claude.json",
        "~/.gemini": home / ".gemini",
        "~/.local": home / ".local",
        "~/.config": home / ".config",
        "/etc/sazed-eco": Path("/etc/sazed-eco"),
    }
    return {label: _can_access(p) for label, p in paths.items()}


def _network() -> bool:
    try:
        with socket.create_connection(("api.github.com", 443), timeout=3):
            return True
    except OSError:
        return False


def _can_write(directory: str) -> bool:
    path = os.path.join(directory, f".sandbox_probe_{os.getpid()}")
    try:
        with open(path, "w") as f:
            f.write("")
    except OSError:
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def test_sandbox_blocks_everything():
    results = _env_checks()
    results.update(_proc_token())
    results["home_reads"] = _home_reads()
    results["network"] = _network()
    results["write_outside"] = {
        "home": _can_write(str(Path.home())),
        "/tmp/../var/tmp": _can_write("/tmp/../var/tmp"),
    }
    print(json.dumps(results))

    assert results["env_token"] is False
    assert results["proc_token"] == 0
    assert not any(results["home_reads"].values())
    assert results["network"] is False
    assert results["write_outside"]["home"] is False
