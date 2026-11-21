"""One bot per machine: a PID lock file for ``scrollback serve bot`` (DESIGN §8).

Telegram lets only one client long-poll a bot token. A second ``serve bot`` would fight the
first (both get 409 Conflicts, and updates go to whichever happens to be polling), so start-up
takes ``traces/bot/serve_bot.lock`` first. It is created atomically and holds the PID, start
time and git commit, so a running bot can be identified later ("which code is it running?").

A lock whose process is gone (killed without cleanup) is stale and is taken over. A lock whose
PID is alive but belongs to some other program (the PID was reused) is also stale.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOCK_NAME = "serve_bot.lock"


class BotAlreadyRunningError(RuntimeError):
    """Another ``serve bot`` holds the lock."""


@dataclass(frozen=True)
class LockInfo:
    pid: int
    started_at: str
    host: str
    commit: str | None


def lock_path(traces_dir: Path) -> Path:
    return traces_dir / "bot" / LOCK_NAME


def git_commit(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True,
            text=True, check=True, timeout=5,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def is_bot_process(pid: int) -> bool:
    """True if ``pid`` is alive and looks like a scrollback bot (not a reused PID)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, owned by someone else
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell: assume it is the bot (safer than starting a second one)
    command = out.stdout.strip()
    if not command:
        return False
    return "scrollback" in command and "serve" in command


def read_lock(path: Path) -> LockInfo | None:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(
            pid=int(data["pid"]),
            started_at=str(data.get("started_at", "")),
            host=str(data.get("host", "")),
            commit=data.get("commit"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


class BotLock:
    def __init__(
        self,
        traces_dir: Path,
        *,
        commit: str | None = None,
        pid: int | None = None,
        alive: Callable[[int], bool] | None = None,  # default: is_bot_process
    ) -> None:
        self.path = lock_path(traces_dir)
        self.info = LockInfo(
            pid=pid if pid is not None else os.getpid(),
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            host=socket.gethostname(),
            commit=commit,
        )
        self._alive = alive
        self.replaced: LockInfo | None = None  # a stale lock that was taken over

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                held = read_lock(self.path)
                alive = self._alive or is_bot_process  # looked up at call time
                if held is not None and held.pid != self.info.pid and alive(held.pid):
                    raise BotAlreadyRunningError(
                        f"another bot is already running (PID {held.pid}, started "
                        f"{held.started_at or 'at an unknown time'}"
                        f"{', commit ' + held.commit if held.commit else ''}). "
                        f"Stop it first (kill {held.pid}), then start this one."
                    ) from None
                self.replaced = held
                self.path.unlink(missing_ok=True)  # stale: its process is gone
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(self.info), fh)
            return
        raise BotAlreadyRunningError("could not take the bot lock (another bot is starting)")

    def release(self) -> None:
        held = read_lock(self.path)
        if held is not None and held.pid == self.info.pid:
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> BotLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
