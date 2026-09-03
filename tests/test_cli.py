from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from dlss5_enabler.cli import app
from dlss5_enabler.core.pe import PeArch
from dlss5_enabler.core.record import BinaryInfo, IndexEntry, InstallOptions, InstallRecord
from dlss5_enabler.network.update_check import UpdateCheckResult
from dlss5_enabler.operations.update import GameUpdateResult, GameUpdateStatus
from dlss5_enabler.platform.proton import SteamPrefixInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def disable_update_checks(mocker: MockerFixture) -> MagicMock:
    return mocker.patch(
        "dlss5_enabler.cli.check_for_update",
        return_value=UpdateCheckResult(current_version="1.0.1"),
    )


def test_cli_help() -> None:
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "DLSS5 Enabler" in res.stdout


def test_cli_list_empty(mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.cli.index_load_active", return_value=[])
    res = runner.invoke(app, ["list"], terminal_width=200)
    assert res.exit_code == 0
    assert "No installed games found" in res.stdout


def test_cli_list_with_entries(mocker: MockerFixture) -> None:
    entries = [
        IndexEntry(
            game_exe="C:/games/Control/Control_DX12.exe",
            game_dir="C:/games/Control",
            timestamp="2026-09-01T12:00:00",
            architecture="x64",
            install_type="D3D11/D3D12",
            tool_version="1.0.0",
        ),
    ]
    mocker.patch("dlss5_enabler.cli.index_load_active", return_value=entries)
    mocker.patch("dlss5_enabler.cli.get_tool_version", return_value="1.1.0")
    res = runner.invoke(app, ["list"], terminal_width=200)
    assert res.exit_code == 0
    assert "Control_DX12.exe" in res.stdout
    assert "1.0.0" in res.stdout
    assert "Update" in res.stdout
    assert "available" in res.stdout


def test_cli_info(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.cli.file_is_writable", return_value=True)
    mocker.patch(
        "dlss5_enabler.cli.record_load",
        return_value=InstallRecord(
            game_exe=str(game_exe),
            game_dir=str(tmp_path),
            tool_version="1.0.0",
            install_options=InstallOptions(lumenite=False, d3d9=True, opengl=False, vulkan_layer=True),
            binaries={"DLSS": BinaryInfo(name="DLSS", version="310.8")},
        ),
    )
    mocker.patch("dlss5_enabler.cli.get_tool_version", return_value="1.1.0")

    res = runner.invoke(app, ["info", str(game_exe)])
    assert res.exit_code == 0
    assert "game.exe" in res.stdout
    assert "x64" in res.stdout
    assert "DLSS: 310.8" in res.stdout
    assert "Installed By Version" in res.stdout
    assert "1.0.0" in res.stdout
    assert "Update available" in res.stdout
    assert "D3D9=Yes" in res.stdout


def test_cli_cache_list_and_clean(tmp_path: Path, mocker: MockerFixture) -> None:
    cache_file = tmp_path / "test.zip"
    cache_file.write_bytes(b"DATA")
    update_marker = tmp_path / "update-check.lock"
    update_marker.touch()

    mocker.patch("dlss5_enabler.cli.get_cache_dir", return_value=tmp_path)

    res = runner.invoke(app, ["cache"])
    assert res.exit_code == 0
    assert "test.zip" in res.stdout

    res_clean = runner.invoke(app, ["cache", "--clean"])
    assert res_clean.exit_code == 0
    assert not cache_file.exists()
    assert not update_marker.exists()


def test_cli_install_command_success(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.run_install", return_value=True)

    res = runner.invoke(app, ["install", str(game_exe)])
    assert res.exit_code == 0
    assert "Activate ReShade effects" in res.stdout
    assert "LUMENITE: Kernel 2.0" in res.stdout
    assert "DLSS 5 Feed" in res.stdout


def test_cli_install_shows_native_dlss_activation_guide(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    mocker.patch("dlss5_enabler.cli.run_install", return_value=True)
    mocker.patch(
        "dlss5_enabler.cli.record_load",
        return_value=InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path), native_dlss_detected=True),
    )

    res = runner.invoke(app, ["install", str(game_exe)])

    assert res.exit_code == 0
    assert "Native DLSS path selected" in res.stdout
    assert "Test with it both enabled and disabled" in res.stdout
    assert "Required order" not in res.stdout


def test_cli_install_command_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.run_install", return_value=False)

    res = runner.invoke(app, ["install", str(game_exe)])
    assert res.exit_code == 1


def test_cli_rejects_d3d9_and_opengl_together(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    run_install = mocker.patch("dlss5_enabler.cli.run_install")

    result = runner.invoke(app, ["install", str(game_exe), "--d3d9", "--opengl"])

    assert result.exit_code == 2
    run_install.assert_not_called()


def test_cli_install_verbose_reconfigures_logger(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    setup = mocker.patch("dlss5_enabler.cli.setup_logger")
    mocker.patch("dlss5_enabler.cli.run_install", return_value=True)
    mocker.patch("dlss5_enabler.cli.record_load", return_value=None)

    result = runner.invoke(app, ["install", str(game_exe), "--verbose"])

    assert result.exit_code == 0
    assert any(call.kwargs.get("verbose") is True for call in setup.call_args_list)


def test_cli_uninstall_command(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.run_uninstall", return_value=True)

    res = runner.invoke(app, ["uninstall", str(game_exe)])
    assert res.exit_code == 0


def test_cli_uninstall_resolves_unique_managed_executable_name(mocker: MockerFixture) -> None:
    entry = IndexEntry(
        game_exe="C:/games/Control/Control_DX12.exe",
        game_dir="C:/games/Control",
        timestamp="2026-09-03T00:00:00+00:00",
    )
    mocker.patch("dlss5_enabler.cli.index_load_active", return_value=[entry])
    run_uninstall = mocker.patch("dlss5_enabler.cli.run_uninstall", return_value=True)

    result = runner.invoke(app, ["uninstall", "control_dx12.EXE"])

    assert result.exit_code == 0
    run_uninstall.assert_called_once_with(Path(entry.game_exe))


def test_cli_uninstall_rejects_ambiguous_executable_name(mocker: MockerFixture) -> None:
    entries = [
        IndexEntry(
            game_exe="C:/games/First/game.exe",
            game_dir="C:/games/First",
            timestamp="2026-09-03T00:00:00+00:00",
        ),
        IndexEntry(
            game_exe="D:/games/Second/GAME.EXE",
            game_dir="D:/games/Second",
            timestamp="2026-09-03T00:00:00+00:00",
        ),
    ]
    mocker.patch("dlss5_enabler.cli.index_load_active", return_value=entries)
    run_uninstall = mocker.patch("dlss5_enabler.cli.run_uninstall")

    result = runner.invoke(app, ["uninstall", "game.exe"])

    assert result.exit_code == 2
    assert "More than one managed executable" in result.stdout
    assert "C:/games/First/game.exe" in result.stdout
    assert "D:/games/Second/GAME.EXE" in result.stdout
    run_uninstall.assert_not_called()


def test_cli_info_proton(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.cli.file_is_writable", return_value=True)
    mocker.patch("dlss5_enabler.cli.record_load", return_value=None)
    mocker.patch(
        "dlss5_enabler.cli.ProtonManager.find_prefix_for_game",
        return_value=SteamPrefixInfo(appid="12345", prefix_path=tmp_path / "pfx", game_dir=tmp_path),
    )

    res = runner.invoke(app, ["info", str(game_exe)])
    assert res.exit_code == 0
    assert "12345" in res.stdout
    assert "WINEDLLOVERRIDES" in res.stdout


def test_cli_install_proton_output(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.run_install", return_value=True)
    mocker.patch(
        "dlss5_enabler.cli.record_load",
        return_value=InstallRecord(
            game_exe=str(game_exe),
            game_dir=str(tmp_path),
            proton_prefix=str(tmp_path / "pfx"),
        ),
    )

    res = runner.invoke(app, ["install", str(game_exe)])
    assert res.exit_code == 0
    assert "Steam / Proton Integration Active" in res.stdout
    assert "WINEDLLOVERRIDES" in res.stdout


def test_cli_does_not_announce_proton_without_prefix(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    mocker.patch("dlss5_enabler.cli.run_install", return_value=True)
    mocker.patch(
        "dlss5_enabler.cli.record_load",
        return_value=InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path), platform="linux"),
    )

    result = runner.invoke(app, ["install", str(game_exe)])

    assert result.exit_code == 0
    assert "Steam / Proton Integration Active" not in result.stdout


def test_cli_cache_clean_reports_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    cache_dir = tmp_path / "cache"
    blocked = cache_dir / "blocked"
    blocked.mkdir(parents=True)
    mocker.patch("dlss5_enabler.cli.get_cache_dir", return_value=cache_dir)
    mocker.patch("dlss5_enabler.cli.shutil.rmtree", side_effect=PermissionError("denied"))

    result = runner.invoke(app, ["cache", "--clean"])

    assert result.exit_code == 1
    assert "cleanup incomplete" in result.stdout.lower()


def test_cli_cache_size_includes_nested_files(tmp_path: Path, mocker: MockerFixture) -> None:
    nested = tmp_path / "stage" / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    mocker.patch("dlss5_enabler.cli.get_cache_dir", return_value=tmp_path)

    result = runner.invoke(app, ["cache"])

    assert result.exit_code == 0
    assert "2.00 MB" in result.stdout


def test_cli_update_warning_shows_uv_and_pip(
    mocker: MockerFixture,
    disable_update_checks: MagicMock,
) -> None:
    disable_update_checks.return_value = UpdateCheckResult(
        current_version="1.1.0",
        latest_version="1.2.0",
        check_performed=True,
        update_available=True,
    )
    mocker.patch("dlss5_enabler.cli.index_load_active", return_value=[])

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "DLSS5 Enabler 1.2.0 is available" in result.stdout
    assert "uv tool upgrade dlss5-enabler" in result.stdout
    assert "python -m pip install --upgrade dlss5-enabler" in result.stdout


def test_cli_update_check_failure_does_not_change_command_exit(
    mocker: MockerFixture,
    disable_update_checks: MagicMock,
) -> None:
    disable_update_checks.return_value = UpdateCheckResult(
        current_version="1.1.0",
        check_performed=True,
        error="offline",
    )
    mocker.patch("dlss5_enabler.cli.index_load_active", return_value=[])

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "No installed games" in result.stdout


def test_cli_version_does_not_check_without_flag(disable_update_checks: MagicMock) -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "DLSS5 Enabler" in result.stdout
    disable_update_checks.assert_not_called()


def test_cli_version_check_is_forced(disable_update_checks: MagicMock) -> None:
    disable_update_checks.return_value = UpdateCheckResult(
        current_version="1.0.1",
        latest_version="1.0.1",
        check_performed=True,
    )

    result = runner.invoke(app, ["version", "--check"])

    assert result.exit_code == 0
    assert "Latest published version: 1.0.1" in result.stdout
    disable_update_checks.assert_called_once_with(force=True)


def test_excluded_commands_do_not_check(
    tmp_path: Path,
    mocker: MockerFixture,
    disable_update_checks: MagicMock,
) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    mocker.patch("dlss5_enabler.cli.run_all_checks", return_value=True)
    mocker.patch("dlss5_enabler.cli.run_uninstall", return_value=True)
    mocker.patch("dlss5_enabler.cli.get_cache_dir", return_value=tmp_path)

    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["check"]).exit_code == 0
    assert runner.invoke(app, ["uninstall", str(game_exe)]).exit_code == 0
    assert runner.invoke(app, ["cache", "--clean"]).exit_code == 0
    disable_update_checks.assert_not_called()


def test_cli_update_command_replays_managed_install(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    update = mocker.patch(
        "dlss5_enabler.cli.run_update",
        return_value=GameUpdateResult(GameUpdateStatus.UPDATED, "Updated successfully."),
    )

    result = runner.invoke(app, ["update", str(game_exe), "--force-download", "--verbose"])

    assert result.exit_code == 0
    assert "Updated successfully" in result.stdout
    assert "Activate ReShade effects" in result.stdout
    assert update.call_args.kwargs["force_download"] is True
    assert update.call_args.kwargs["verbose"] is True


def test_cli_update_failure_has_nonzero_exit(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    mocker.patch(
        "dlss5_enabler.cli.run_update",
        return_value=GameUpdateResult(GameUpdateStatus.RECORD_INVALID, "Record invalid."),
    )

    result = runner.invoke(app, ["update", str(game_exe)])

    assert result.exit_code == 1
    assert "Record invalid" in result.stdout
    assert "Activate ReShade effects" not in result.stdout
