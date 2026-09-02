import os
from pathlib import Path
from unittest.mock import patch

import pytest

from dlss5_enabler.platform import LinuxAdapter, WindowsAdapter, get_platform_adapter


def test_windows_adapter_paths(tmp_path: Path) -> None:
    adapter = WindowsAdapter()
    assert adapter.platform_name == "windows"
    assert adapter.get_curl_command() == ["curl.exe"]
    assert not adapter.is_wsl()

    with patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path)}):
        data_dir = adapter.get_data_dir()
        assert data_dir == tmp_path / "DLSS5 Enabler"
        assert adapter.get_cache_dir() == tmp_path / "DLSS5 Enabler" / "downloads"
        assert adapter.get_log_dir() == tmp_path / "DLSS5 Enabler" / "logs"
        assert adapter.get_config_dir() == tmp_path / "DLSS5 Enabler"


def test_windows_adapter_unblock_and_exec(tmp_path: Path) -> None:
    adapter = WindowsAdapter()
    f = tmp_path / "dummy.txt"
    f.write_text("hello", encoding="utf-8")
    adapter.make_executable(f)
    adapter.unblock_file(f)
    assert f.is_file()


def test_linux_adapter_paths(tmp_path: Path) -> None:
    adapter = LinuxAdapter()
    assert adapter.platform_name == "linux"
    assert adapter.get_curl_command() == ["curl"]

    data_home = tmp_path / "data"
    cache_home = tmp_path / "cache"
    state_home = tmp_path / "state"
    config_home = tmp_path / "config"

    with patch.dict(
        os.environ,
        {
            "XDG_DATA_HOME": str(data_home),
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_STATE_HOME": str(state_home),
            "XDG_CONFIG_HOME": str(config_home),
        },
        clear=True,
    ):
        assert adapter.get_data_dir() == data_home / "dlss5-enabler"
        assert adapter.get_cache_dir() == cache_home / "dlss5-enabler" / "downloads"
        assert adapter.get_log_dir() == state_home / "dlss5-enabler" / "logs"
        assert adapter.get_config_dir() == config_home / "dlss5-enabler"


def test_linux_adapter_ignores_relative_xdg_paths(tmp_path: Path) -> None:
    adapter = LinuxAdapter()
    with (
        patch.dict(os.environ, {"XDG_DATA_HOME": "relative/data"}, clear=True),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        assert adapter.get_data_dir() == tmp_path / ".local" / "share" / "dlss5-enabler"


def test_linux_adapter_make_executable(tmp_path: Path) -> None:
    adapter = LinuxAdapter()
    script = tmp_path / "test.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    with patch.object(Path, "chmod") as mock_chmod:
        adapter.make_executable(script)
        mock_chmod.assert_called_once()


def test_linux_adapter_wsl_detection(tmp_path: Path) -> None:
    adapter = LinuxAdapter()

    with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
        assert adapter.is_wsl()

    proc_ver = tmp_path / "version"
    proc_ver.write_text("Linux version 5.15.0-microsoft-standard-WSL2", encoding="utf-8")

    with patch.dict(os.environ, {}, clear=True), patch("dlss5_enabler.platform.linux.Path") as mock_path:
        mock_path.return_value = proc_ver
        mock_path.home.return_value = tmp_path
        assert adapter.is_wsl()


def test_get_platform_adapter_forced() -> None:
    win_adapter = get_platform_adapter(force_platform="windows")
    assert isinstance(win_adapter, WindowsAdapter)

    linux_adapter = get_platform_adapter(force_platform="linux")
    assert isinstance(linux_adapter, LinuxAdapter)


def test_windows_adapter_permission_helpers(tmp_path: Path) -> None:
    adapter = WindowsAdapter()
    assert adapter.is_directory_writable(tmp_path)
    assert not adapter.is_directory_writable(tmp_path / "non_existent_dir")
    guidance = adapter.get_permission_guidance(tmp_path)
    assert "icacls" in guidance
    assert "Administrator" in guidance


def test_linux_adapter_permission_helpers(tmp_path: Path) -> None:
    adapter = LinuxAdapter()
    assert adapter.is_directory_writable(tmp_path)
    assert not adapter.is_directory_writable(tmp_path / "non_existent_dir")
    guidance = adapter.get_permission_guidance(tmp_path)
    assert "chmod -R u+w" in guidance
    assert "chown" in guidance


@pytest.mark.parametrize("adapter", [LinuxAdapter(), WindowsAdapter()])
def test_permission_probe_preserves_existing_file(tmp_path: Path, adapter: LinuxAdapter | WindowsAdapter) -> None:
    existing = tmp_path / ".dlss5-enabler-perm-probe.tmp"
    existing.write_text("KEEP", encoding="utf-8")

    assert adapter.is_directory_writable(tmp_path)
    assert existing.read_text(encoding="utf-8") == "KEEP"
