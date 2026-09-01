from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "run-staging.sh"


def wrapper_environment(tmp_path: Path, state_dir: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    environment = dict(os.environ)
    environment.update({
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        "RELAY_AGENT_TOKEN": "relay-test-token",
        "RELAY_BASE_URL": "https://api.staging.relayapp.im",
        "RELAY_STATE_DIR": str(state_dir),
        "HERMES_CAPTURE": str(tmp_path / "hermes.capture"),
        "HERMES_READY": str(tmp_path / "hermes.ready"),
        "HERMES_GO": str(tmp_path / "hermes.go"),
    })
    return environment


def write_fake_hermes(tmp_path: Path, body: str) -> None:
    executable = tmp_path / "bin" / "hermes"
    executable.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    executable.chmod(0o755)


def test_staging_wrapper_delegates_all_state_path_operations():
    source = WRAPPER.read_text(encoding="utf-8")
    assert "\nmkdir " not in source
    assert "\nchmod " not in source
    assert "RelayInbox owns state-directory creation" in source


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and modes")
def test_staging_wrapper_does_not_follow_or_chmod_state_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o777)
    state_dir = tmp_path / "relay-state"
    state_dir.symlink_to(target, target_is_directory=True)
    environment = wrapper_environment(tmp_path, state_dir)
    write_fake_hermes(
        tmp_path,
        'printf "%s\\n" "$*" > "$HERMES_CAPTURE"\n',
    )

    completed = subprocess.run(
        ["sh", str(WRAPPER)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "hermes.capture").read_text().strip() == "gateway run"
    assert os.stat(target).st_mode & 0o777 == 0o777
    assert list(target.iterdir()) == []
    assert state_dir.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename and modes")
def test_staging_wrapper_never_touches_replaced_state_directory(tmp_path):
    state_dir = tmp_path / "relay-state"
    state_dir.mkdir()
    state_dir.chmod(0o777)
    displaced = tmp_path / "relay-state-displaced"
    environment = wrapper_environment(tmp_path, state_dir)
    write_fake_hermes(
        tmp_path,
        ': > "$HERMES_READY"\n'
        'while [ ! -e "$HERMES_GO" ]; do sleep 0.01; done\n'
        'printf "%s\\n" "$*" > "$HERMES_CAPTURE"\n',
    )

    process = subprocess.Popen(
        ["sh", str(WRAPPER)],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    ready = tmp_path / "hermes.ready"
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "fake Hermes did not start"
    assert os.stat(state_dir).st_mode & 0o777 == 0o777

    state_dir.rename(displaced)
    state_dir.mkdir()
    state_dir.chmod(0o777)
    (tmp_path / "hermes.go").touch()
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, (stdout, stderr)
    assert (tmp_path / "hermes.capture").read_text().strip() == "gateway run"
    assert os.stat(displaced).st_mode & 0o777 == 0o777
    assert os.stat(state_dir).st_mode & 0o777 == 0o777
    assert list(displaced.iterdir()) == []
    assert list(state_dir.iterdir()) == []
