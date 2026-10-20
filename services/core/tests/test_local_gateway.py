"""本机网关凭据文件回落：只对回环地址、本人拥有且他人不可写的文件生效。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from loomvec.core.local_gateway import (
    ADMIN_TOKEN_FILE,
    API_KEY_FILE,
    default_state_dir,
    is_loopback_url,
    local_gateway_secret,
)

LOOP = "http://127.0.0.1:38095"


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("http://127.0.0.1:38095", True),
        ("http://localhost:38095", True),
        ("http://[::1]:38095", True),
        ("http://127.1.2.3", True),
        ("http://192.168.1.5:38095", False),
        ("https://gw.example.com", False),
        ("http://0.0.0.0:38095", False),
        ("not a url", False),
    ],
)
def test_is_loopback_url(url: str, want: bool) -> None:
    assert is_loopback_url(url) is want


def test_reads_and_strips_secret(tmp_path: Path) -> None:
    _write(tmp_path / API_KEY_FILE, "  acpgw_abc \n")
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) == "acpgw_abc"


def test_remote_gateway_never_reads_local_files(tmp_path: Path) -> None:
    _write(tmp_path / API_KEY_FILE, "acpgw_abc")
    assert local_gateway_secret("https://gw.example.com", API_KEY_FILE, str(tmp_path)) is None


def test_missing_empty_or_directory_is_none(tmp_path: Path) -> None:
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None
    _write(tmp_path / API_KEY_FILE, " \n")
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None
    (tmp_path / ADMIN_TOKEN_FILE).mkdir()
    assert local_gateway_secret(LOOP, ADMIN_TOKEN_FILE, str(tmp_path)) is None


@pytest.mark.parametrize("mode", [0o666, 0o620, 0o602])
def test_file_writable_by_others_is_not_trusted(tmp_path: Path, mode: int) -> None:
    _write(tmp_path / API_KEY_FILE, "acpgw_abc", mode)
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None


def test_group_readable_but_not_writable_is_ok(tmp_path: Path) -> None:
    _write(tmp_path / API_KEY_FILE, "acpgw_abc", 0o640)
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) == "acpgw_abc"


def test_default_state_dir_follows_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_state_dir() == tmp_path / "acp-gateway"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert default_state_dir() == Path.home() / ".local" / "share" / "acp-gateway"


def test_relative_xdg_data_home_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")
    assert default_state_dir() == Path.home() / ".local" / "share" / "acp-gateway"
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert default_state_dir() == Path.home() / ".local" / "share" / "acp-gateway"


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    _write(target, "stolen")
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / API_KEY_FILE).symlink_to(target)
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(state)) is None


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_state_dir_writable_by_others_is_not_trusted(tmp_path: Path, mode: int) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _write(state / API_KEY_FILE, "acpgw_abc")
    state.chmod(mode)
    try:
        assert local_gateway_secret(LOOP, API_KEY_FILE, str(state)) is None
    finally:
        state.chmod(0o700)


def test_file_owned_by_someone_else_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / API_KEY_FILE, "acpgw_abc")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None


def test_state_dir_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / "gw"
    state.mkdir(mode=0o700)
    _write(state / API_KEY_FILE, "acpgw_abc")
    assert local_gateway_secret(LOOP, API_KEY_FILE, "~/gw") == "acpgw_abc"


def test_directory_target_does_not_leak_fds(tmp_path: Path) -> None:
    (tmp_path / API_KEY_FILE).mkdir()
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None
    assert len(os.listdir("/proc/self/fd")) <= before


def test_fifo_target_does_not_block(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / API_KEY_FILE, 0o600)
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None


def test_non_utf8_file_is_none(tmp_path: Path) -> None:
    (tmp_path / API_KEY_FILE).write_bytes(b"\xff\xfe\x00")
    (tmp_path / API_KEY_FILE).chmod(0o600)
    assert local_gateway_secret(LOOP, API_KEY_FILE, str(tmp_path)) is None
