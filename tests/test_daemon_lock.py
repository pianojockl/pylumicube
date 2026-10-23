"""Tests for the daemon's single-instance lock (no hardware).

Two daemons on one cube is not a benign mistake: the second opens the serial
port the first is using before it discovers the socket is taken, so the
running daemon's link is disturbed first and the intruder fails second.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pylumicube.daemon import acquire_instance_lock


def test_the_lock_is_exclusive_within_a_process(tmp_path) -> None:
    socket_path = str(tmp_path / "control.sock")
    fd = acquire_instance_lock(socket_path)
    try:
        assert os.path.exists(f"{socket_path}.lock")
        with pytest.raises(RuntimeError) as error:
            acquire_instance_lock(socket_path)
        assert "already owns" in str(error.value)
    finally:
        os.close(fd)


def test_the_lock_is_released_when_the_owner_closes_it(tmp_path) -> None:
    socket_path = str(tmp_path / "control.sock")
    fd = acquire_instance_lock(socket_path)
    os.close(fd)
    fd = acquire_instance_lock(socket_path)          # a restart may take over
    try:
        assert fd > 0
    finally:
        os.close(fd)


def test_the_lock_records_the_owner(tmp_path) -> None:
    socket_path = str(tmp_path / "control.sock")
    fd = acquire_instance_lock(socket_path)
    try:
        with open(f"{socket_path}.lock") as handle:
            assert handle.read().strip() == str(os.getpid())
    finally:
        os.close(fd)


def test_a_second_process_is_refused(tmp_path) -> None:
    """The real case: a stray launch next to the systemd unit."""
    socket_path = str(tmp_path / "control.sock")
    fd = acquire_instance_lock(socket_path)
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from pylumicube.daemon import acquire_instance_lock;"
             f"acquire_instance_lock({socket_path!r})"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0, "a second daemon was allowed to start"
        assert "already owns" in result.stderr
    finally:
        os.close(fd)
