from pathlib import Path

from pytest_mock import MockerFixture
from typer.testing import CliRunner

from dlss5_enabler.cli import app
from dlss5_enabler.core.pe import PeArch
from dlss5_enabler.core.record import BinaryInfo, IndexEntry, InstallRecord
from dlss5_enabler.platform.proton import SteamPrefixInfo

runner = CliRunner()


def test_cli_help() -> None:
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "DLSS5 Enabler" in res.stdout


def test_cli_list_empty(mocker: MockerFixture) -> None:
    mocker.patch("dlss5_enabler.cli.index_load", return_value=[])
    res = runner.invoke(app, ["list"])
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
        ),
    ]
    mocker.patch("dlss5_enabler.cli.index_load", return_value=entries)
    res = runner.invoke(app, ["list"])
    assert res.exit_code == 0
    assert "Control_DX12.exe" in res.stdout


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
            binaries={"DLSS": BinaryInfo(name="DLSS", version="310.8")},
        ),
    )

    res = runner.invoke(app, ["info", str(game_exe)])
    assert res.exit_code == 0
    assert "game.exe" in res.stdout
    assert "x64" in res.stdout
    assert "DLSS: 310.8" in res.stdout


def test_cli_cache_list_and_clean(tmp_path: Path, mocker: MockerFixture) -> None:
    cache_file = tmp_path / "test.zip"
    cache_file.write_bytes(b"DATA")

    mocker.patch("dlss5_enabler.cli.get_cache_dir", return_value=tmp_path)

    res = runner.invoke(app, ["cache"])
    assert res.exit_code == 0
    assert "test.zip" in res.stdout

    res_clean = runner.invoke(app, ["cache", "--clean"])
    assert res_clean.exit_code == 0
    assert not cache_file.exists()


def test_cli_install_command_success(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")

    mocker.patch("dlss5_enabler.cli.run_install", return_value=True)

    res = runner.invoke(app, ["install", str(game_exe)])
    assert res.exit_code == 0


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
