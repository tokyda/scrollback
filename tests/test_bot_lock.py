"""Single-instance lock for `serve bot` (no real processes are signalled)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scrollback.bot.lock import (
    BotAlreadyRunningError,
    BotLock,
    is_bot_process,
    lock_path,
    read_lock,
)


def test_acquire_writes_pid_commit_and_release_removes(tmp_path: Path) -> None:
    lock = BotLock(tmp_path, commit="abc1234", pid=111, alive=lambda pid: True)
    with lock:
        info = read_lock(lock_path(tmp_path))
        assert info is not None and info.pid == 111 and info.commit == "abc1234"
        assert info.started_at and info.host
    assert not lock_path(tmp_path).exists()


def test_second_instance_refused_while_first_is_alive(tmp_path: Path) -> None:
    first = BotLock(tmp_path, commit="aaa", pid=111, alive=lambda pid: True)
    first.acquire()
    second = BotLock(tmp_path, pid=222, alive=lambda pid: pid == 111)
    with pytest.raises(BotAlreadyRunningError, match=r"PID 111.*commit aaa.*kill 111"):
        second.acquire()
    second.release()  # not the holder: must not remove the first bot's lock
    held = read_lock(lock_path(tmp_path))
    assert held is not None and held.pid == 111


def test_stale_lock_is_taken_over(tmp_path: Path) -> None:
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 111, "started_at": "x", "host": "h", "commit": None}))
    lock = BotLock(tmp_path, pid=222, alive=lambda pid: False)
    lock.acquire()
    assert lock.replaced is not None and lock.replaced.pid == 111
    held = read_lock(path)
    assert held is not None and held.pid == 222


def test_corrupt_lock_is_taken_over(tmp_path: Path) -> None:
    path = lock_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    lock = BotLock(tmp_path, pid=222, alive=lambda pid: True)
    lock.acquire()
    held = read_lock(path)
    assert held is not None and held.pid == 222


def test_is_bot_process_checks_liveness_and_command() -> None:
    assert is_bot_process(os.getpid()) is False  # alive, but pytest is not `scrollback serve`
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert is_bot_process(proc.pid) is False  # gone
