import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.core.ini import ini_get_exact
from dlss5_enabler.core.pe import DetectedApi, PeArch
from dlss5_enabler.core.record import (
    CURRENT_RECORD_SCHEMA_VERSION,
    InstallOptions,
    InstallRecord,
    OptiScalerStrategyOptions,
    record_load,
    record_save,
)
from dlss5_enabler.network.sources import NgxBundle, OptiScalerBundle, fetch_optiscaler
from dlss5_enabler.operations.contexts import OptiScalerContext
from dlss5_enabler.operations.optiscaler import (
    StepConfigureOptiScaler,
    StepInstallOptiScaler,
    StepPrepareOptiScaler,
    _extract_archive,
)
from dlss5_enabler.operations.pipeline import TargetAnalysis
from dlss5_enabler.operations.uninstall import run_uninstall
from dlss5_enabler.schemas.strategy import InstallStrategy

EXPECTED_DIGEST = "f927b5aed15d09b23f559433d6740834f550d79bb2b75c7315602319819a3096"


def _archive(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    ini = "\n".join(
        (
            "[Upscalers]",
            "Dx11Upscaler=auto",
            "Dx12Upscaler=auto",
            "[DlssNr]",
            "Enabled=auto",
            "Passes=auto",
            "AutoCapture=auto",
            "[FrameGen]",
            "Enabled=auto",
            "[Menu]",
            "ShortcutKey=auto",
            "[Log]",
            "LogToFile=auto",
            "LogFileName=auto",
            "[Plugins]",
            "LoadReshade=auto",
            "LoadSpecialK=auto",
            "[Inputs]",
            "EnableDlssInputs=auto",
            "EnableXeSSInputs=auto",
            "EnableFsr2Inputs=auto",
            "EnableFsr3Inputs=auto",
            "EnableFfxInputs=auto",
            "[Spoofing]",
            "Dxgi=auto",
            "[Hotfix]",
            "CheckForUpdate=auto",
            "",
        )
    ).encode()
    members = {
        "OptiScaler.dll": b"proxy",
        "OptiScaler.ini": ini,
        "nvngx.dll_dlssnr.dll": b"forwarder",
        "OptiScaler/helper.dll": b"helper",
        "Licenses/license.txt": b"license",
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _context(tmp_path: Path, archive_path: Path) -> OptiScalerContext:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"game")
    bundle = OptiScalerBundle()
    bundle.archive_path = archive_path
    bundle.variant = "y4my4my4m-v3"
    bundle.source_revision = EXPECTED_DIGEST
    nr_path = tmp_path / "source-nvngx_dlssnr.dll"
    nr_path.write_bytes(b"nr-model")
    ngx = NgxBundle()
    ngx.nr_dll_path = nr_path
    options = OptiScalerStrategyOptions(proxy_name="dxgi.dll", source_revision="pending")
    record = InstallRecord(
        schema_version=CURRENT_RECORD_SCHEMA_VERSION,
        strategy=InstallStrategy.OPTISCALER,
        strategy_options=options,
        install_options=InstallOptions(),
        game_exe=game_exe.as_posix(),
        game_dir=tmp_path.as_posix(),
    )
    return OptiScalerContext(
        game_exe=game_exe,
        game_dir=tmp_path,
        strategy=InstallStrategy.OPTISCALER,
        analysis=TargetAnalysis(PeArch.X64, (DetectedApi.D3D12,), True),
        record=record,
        bundle=bundle,
        ngx_bundle=ngx,
        nr_passes=3,
    )


def test_archive_extraction_rejects_traversal(tmp_path: Path) -> None:
    archive_path = _archive(tmp_path / "unsafe.zip", {"../escape.dll": b"escape"})

    with pytest.raises(ValueError, match="Unsafe archive member path"):
        _extract_archive(archive_path, tmp_path / "stage")

    assert not (tmp_path / "escape.dll").exists()


def test_prepare_rejects_sources_that_map_to_same_proxy(tmp_path: Path, mocker: MockerFixture) -> None:
    archive_path = _archive(tmp_path / "collision.zip", {"dxgi.dll": b"second proxy"})
    ctx = _context(tmp_path, archive_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.operations.optiscaler.get_cache_dir", return_value=cache)

    with pytest.raises(ValueError, match="collide"):
        StepPrepareOptiScaler().execute(ctx)


def test_prepare_configures_conservative_native_dlss_profile(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.operations.optiscaler.get_cache_dir", return_value=cache)

    assert StepPrepareOptiScaler().execute(ctx)
    ini = next(path for path in ctx.staged_files.values() if path.name == "OptiScaler.ini")
    assert ini_get_exact(ini, "DlssNr", "Enabled") == (True, "true")
    assert ini_get_exact(ini, "DlssNr", "Passes") == (True, "3")
    assert ini_get_exact(ini, "DlssNr", "AutoCapture") == (True, "false")
    assert ini_get_exact(ini, "FrameGen", "Enabled") == (True, "false")
    assert ini_get_exact(ini, "Menu", "ShortcutKey") == (True, "0x2E")
    assert ini_get_exact(ini, "Spoofing", "Dxgi") == (True, "false")
    assert ini_get_exact(ini, "Hotfix", "CheckForUpdate") == (True, "false")


def test_install_uninstall_round_trip_records_strategy(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.operations.optiscaler.get_cache_dir", return_value=cache)

    assert StepPrepareOptiScaler().execute(ctx)
    assert StepInstallOptiScaler().execute(ctx)
    assert record_save(ctx.record)
    installed = record_load(tmp_path)
    assert installed is not None
    assert installed.strategy is InstallStrategy.OPTISCALER
    assert isinstance(installed.strategy_options, OptiScalerStrategyOptions)
    assert installed.strategy_options.nr_passes == 3
    assert (tmp_path / "dxgi.dll").read_bytes() == b"proxy"
    assert (tmp_path / "nvngx_dlssnr.dll").read_bytes() == b"nr-model"

    assert run_uninstall(tmp_path)
    assert (tmp_path / "game.exe").read_bytes() == b"game"
    assert not (tmp_path / "dxgi.dll").exists()
    assert not (tmp_path / "OptiScaler.ini").exists()
    assert not (tmp_path / "OptiScaler").exists()
    assert not (tmp_path / "Licenses").exists()


def test_configure_refuses_non_native_dlss_and_reserved_capture(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.get_platform_adapter",
        return_value=SimpleNamespace(platform_name="windows"),
    )
    ctx.analysis = TargetAnalysis(PeArch.X64, (DetectedApi.D3D12,), False)
    assert not StepConfigureOptiScaler().execute(ctx)
    assert "native DLSS" in ctx.error_message

    ctx.analysis = TargetAnalysis(PeArch.X64, (DetectedApi.D3D12,), True)
    (tmp_path / "dlssnr-capture").mkdir()
    assert not StepConfigureOptiScaler().execute(ctx)
    assert "may delete" in ctx.error_message


def test_local_archive_is_pinned_and_reused_from_hash_cache(tmp_path: Path, mocker: MockerFixture) -> None:
    source = _archive(tmp_path / "source.zip")
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=cache)
    mocker.patch("dlss5_enabler.network.sources.sha256_file", return_value=EXPECTED_DIGEST)

    first = fetch_optiscaler(lambda _message: None, archive_path=source)
    second = fetch_optiscaler(lambda _message: None, source_revision=EXPECTED_DIGEST)

    assert first.archive_path == second.archive_path
    assert first.archive_path is not None
    assert first.archive_path.read_bytes() == source.read_bytes()
    assert second.source_revision == EXPECTED_DIGEST
