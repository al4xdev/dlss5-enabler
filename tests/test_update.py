from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.core.record import (
    CURRENT_RECORD_SCHEMA_VERSION,
    InstallOptions,
    InstallRecord,
    OptiScalerStrategyOptions,
)
from dlss5_enabler.operations.pipeline import PipelineResult, PipelineStatus
from dlss5_enabler.operations.update import GameUpdateStatus, run_update
from dlss5_enabler.schemas.strategy import InstallStrategy


def _write_record(
    game_exe: Path,
    *,
    tool_version: str,
    options: InstallOptions | None = None,
) -> InstallRecord:
    record = InstallRecord(
        game_exe=game_exe.as_posix(),
        game_dir=game_exe.parent.as_posix(),
        tool_version=tool_version,
        install_options=options or InstallOptions(),
    )
    record.record_path().write_text(record.model_dump_json(), encoding="utf-8")
    return record


@pytest.mark.parametrize(
    "options",
    [
        InstallOptions(),
        InstallOptions(d3d9=True),
        InstallOptions(opengl=True),
        InstallOptions(vulkan_layer=True),
        InstallOptions(lumenite=False),
    ],
    ids=["default", "d3d9", "opengl", "vulkan", "no-lumenite"],
)
def test_update_replays_saved_options_from_directory(
    tmp_path: Path,
    mocker: MockerFixture,
    options: InstallOptions,
) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    _write_record(game_exe, tool_version="1.0.0", options=options)
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked", return_value=PipelineResult(PipelineStatus.COMPLETED)
    )

    result = run_update(tmp_path, force_download=True, verbose=True)

    assert result.status is GameUpdateStatus.UPDATED
    install.assert_called_once_with(
        game_exe,
        install_lumenite=options.lumenite,
        d3d9_translate=options.d3d9,
        opengl=options.opengl,
        install_vulkan_layer=options.vulkan_layer,
        force_download=True,
        verbose=True,
        strategy=InstallStrategy.RENODX,
    )


def test_equal_version_does_not_modify_game(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"ORIGINAL")
    record = _write_record(game_exe, tool_version="1.1.0")
    record_bytes = record.record_path().read_bytes()
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch("dlss5_enabler.operations.update._run_install_unlocked")

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.ALREADY_CURRENT
    assert game_exe.read_bytes() == b"ORIGINAL"
    assert record.record_path().read_bytes() == record_bytes
    install.assert_not_called()


def test_reinstall_reapplies_equal_version(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    _write_record(game_exe, tool_version="1.1.0")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked", return_value=PipelineResult(PipelineStatus.COMPLETED)
    )

    result = run_update(game_exe, reinstall=True)

    assert result.status is GameUpdateStatus.REINSTALLED
    install.assert_called_once()


def test_update_preserves_saved_optiscaler_source_and_options(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    strategy_options = OptiScalerStrategyOptions(proxy_name="winmm.dll", source_revision="f" * 64, nr_passes=4)
    record = InstallRecord(
        schema_version=CURRENT_RECORD_SCHEMA_VERSION,
        strategy=InstallStrategy.OPTISCALER,
        strategy_options=strategy_options,
        install_options=InstallOptions(),
        game_exe=game_exe.as_posix(),
        game_dir=tmp_path.as_posix(),
        tool_version="1.0.0",
    )
    record.record_path().write_text(record.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked",
        return_value=PipelineResult(PipelineStatus.COMPLETED),
    )

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.UPDATED
    install.assert_called_once_with(
        game_exe,
        force_download=False,
        verbose=False,
        strategy=InstallStrategy.OPTISCALER,
        optiscaler_archive=None,
        optiscaler_source_revision="f" * 64,
        optiscaler_nr_passes=4,
        optiscaler_proxy="winmm.dll",
    )


def test_explicit_switch_from_renodx_to_optiscaler_passes_archive(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    archive = tmp_path / "opti.zip"
    game_exe.write_bytes(b"MZ")
    archive.write_bytes(b"ZIP")
    _write_record(game_exe, tool_version="1.1.0")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked",
        return_value=PipelineResult(PipelineStatus.COMPLETED),
    )

    result = run_update(
        game_exe,
        strategy=InstallStrategy.OPTISCALER,
        optiscaler_archive=archive,
        optiscaler_nr_passes=2,
    )

    assert result.status is GameUpdateStatus.REINSTALLED
    assert install.call_args.kwargs["strategy"] is InstallStrategy.OPTISCALER
    assert install.call_args.kwargs["optiscaler_archive"] == archive
    assert install.call_args.kwargs["optiscaler_nr_passes"] == 2


def test_newer_record_refuses_downgrade(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    _write_record(game_exe, tool_version="2.0.0")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch("dlss5_enabler.operations.update._run_install_unlocked")

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.DOWNGRADE_REFUSED
    assert "Update the CLI" in result.message
    install.assert_not_called()


def test_unknown_legacy_version_can_be_updated(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    _write_record(game_exe, tool_version="legacy")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    install = mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked", return_value=PipelineResult(PipelineStatus.COMPLETED)
    )
    messages: list[str] = []

    result = run_update(game_exe, log=messages.append)

    assert result.status is GameUpdateStatus.UPDATED
    assert "options:" in messages[0]
    install.assert_called_once()


def test_missing_record_recommends_install(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    install = mocker.patch("dlss5_enabler.operations.update._run_install_unlocked")

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.RECORD_MISSING
    assert "install" in result.message
    install.assert_not_called()


def test_corrupted_record_is_preserved(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    record_path = tmp_path / "dlss5-enabler.install.json"
    record_path.write_bytes(b"{broken")
    install = mocker.patch("dlss5_enabler.operations.update._run_install_unlocked")

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.RECORD_INVALID
    assert record_path.read_bytes() == b"{broken"
    install.assert_not_called()


def test_unknown_future_record_schema_is_preserved(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    record = _write_record(game_exe, tool_version="2.0.0")
    record_path = record.record_path()
    future = record.model_copy(update={"schema_version": CURRENT_RECORD_SCHEMA_VERSION + 1}).model_dump_json()
    record_path.write_text(future, encoding="utf-8")
    install = mocker.patch("dlss5_enabler.operations.update._run_install_unlocked")

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.RECORD_INVALID
    assert record_path.read_text(encoding="utf-8") == future
    install.assert_not_called()


def test_install_failure_reports_restoration(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    record = _write_record(game_exe, tool_version="1.0.0")
    original = record.record_path().read_bytes()
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked", return_value=PipelineResult(PipelineStatus.FAILED)
    )

    result = run_update(game_exe)

    assert result.status is GameUpdateStatus.FAILED
    assert record.record_path().read_bytes() == original


def test_update_acquires_the_game_operation_lock_once(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    _write_record(game_exe, tool_version="1.0.0")
    lock_targets: list[Path] = []

    @contextmanager
    def track_lock(target: Path) -> Generator[None, None, None]:
        lock_targets.append(target)
        yield

    mocker.patch("dlss5_enabler.operations.update.resource_lock", side_effect=track_lock)
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    mocker.patch(
        "dlss5_enabler.operations.update._run_install_unlocked", return_value=PipelineResult(PipelineStatus.COMPLETED)
    )

    assert run_update(game_exe).success
    assert lock_targets == [tmp_path / ".dlss5-enabler-install-operation"]


@pytest.mark.parametrize("status", [PipelineStatus.FAILED, PipelineStatus.RECOVERY_FAILED])
def test_update_only_claims_restoration_when_recovery_succeeded(
    tmp_path: Path, mocker: MockerFixture, status: PipelineStatus
) -> None:
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    _write_record(exe, tool_version="1.0.0")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.2.0")
    incomplete = status is PipelineStatus.RECOVERY_FAILED
    installation = PipelineResult(
        status,
        message="simulated install failure",
        recovery_errors=("locked user file",) if incomplete else (),
        recovery_path=tmp_path / "recovery" if incomplete else None,
    )
    mocker.patch("dlss5_enabler.operations.update._run_install_unlocked", return_value=installation)
    result = run_update(exe)
    assert not result.success
    assert result.installation == installation
    assert ("was restored" in result.message) is not incomplete
    if incomplete:
        assert result.status is GameUpdateStatus.RECOVERY_FAILED
        assert "locked user file" in result.message
        assert str(tmp_path / "recovery") in result.message


def test_update_reports_active_installation_with_pending_cleanup(tmp_path: Path, mocker: MockerFixture) -> None:
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    _write_record(exe, tool_version="1.0.0")
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.2.0")
    installation = PipelineResult(PipelineStatus.CLEANUP_PENDING, cleanup_errors=("snapshot is locked",))
    mocker.patch("dlss5_enabler.operations.update._run_install_unlocked", return_value=installation)
    result = run_update(exe)
    assert result.success
    assert "cleanup pending" in result.message
    assert "snapshot is locked" in result.message
