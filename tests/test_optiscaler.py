import zipfile
from pathlib import Path
from types import SimpleNamespace

import py7zr
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
from dlss5_enabler.network.manifest import load_upstream_manifest
from dlss5_enabler.network.resolver import ArtifactCandidate, ArtifactValidator
from dlss5_enabler.network.sources import DlssgBundle, NgxBundle, OptiScalerBundle, fetch_optiscaler
from dlss5_enabler.operations.contexts import OptiScalerContext
from dlss5_enabler.operations.optiscaler import (
    StepConfigureOptiScaler,
    StepConfigureOptiScalerWineOverrides,
    StepInstallOptiScaler,
    StepPrepareOptiScaler,
    _extract_archive,
)
from dlss5_enabler.operations.pipeline import TargetAnalysis
from dlss5_enabler.operations.uninstall import revert_record_mutations, run_uninstall
from dlss5_enabler.platform import NvidiaGpuGeneration, NvidiaGpuInfo, SteamPrefixInfo, WineRegParser
from dlss5_enabler.schemas.strategy import FrameGenerationMode, InstallStrategy, NrPlacement

EXPECTED_DIGEST = "f927b5aed15d09b23f559433d6740834f550d79bb2b75c7315602319819a3096"
V4_DIGEST = "9d7824cc9cfb15265bc6438b4638aad74ff9cd6d1d3488ab73724affb386a8b0"
V4_REVISION = "v10.0.0-dev-fork-y4my4my4m-v4"


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


def _seven_archive(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    members = {
        "OptiScaler.dll": b"proxy",
        "OptiScaler.ini": b"[Menu]\nShortcutKey=auto\n",
        "nvngx.dll_dlssnr.dll": b"forwarder",
        "OptiScaler/nvngx_dlss.dll": b"dlss",
        "OptiScaler/nvngx_dlssg.dll": b"dlssg",
        "OptiScaler/streamline/sl.interposer.dll": b"streamline",
    }
    members.update(extra or {})
    with py7zr.SevenZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(content, name)
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
        proxy_name="dxgi.dll",
        frame_generation=FrameGenerationMode.FSR,
    )


def test_archive_extraction_rejects_traversal(tmp_path: Path) -> None:
    archive_path = _archive(tmp_path / "unsafe.zip", {"../escape.dll": b"escape"})

    with pytest.raises(ValueError, match="Unsafe archive member path"):
        _extract_archive(archive_path, tmp_path / "stage")

    assert not (tmp_path / "escape.dll").exists()


def test_seven_zip_extraction_preserves_verified_layout(tmp_path: Path) -> None:
    archive_path = _seven_archive(tmp_path / "optiscaler.7z")

    extracted = _extract_archive(archive_path, tmp_path / "stage")

    assert extracted["OptiScaler.dll"].read_bytes() == b"proxy"
    assert extracted["OptiScaler/streamline/sl.interposer.dll"].read_bytes() == b"streamline"


def test_upstream_validator_accepts_supported_optiscaler_seven_zip(tmp_path: Path) -> None:
    archive_path = _seven_archive(tmp_path / "optiscaler.7z")
    policy = load_upstream_manifest().components["optiscaler"]
    candidate = ArtifactCandidate(
        provider="github",
        revision="fixture",
        name=archive_path.name,
        url="https://example.com/optiscaler.7z",
    )

    _digest, _size, archive_format = ArtifactValidator().validate(archive_path, policy, candidate)

    assert archive_format == 1


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
    assert ini_get_exact(ini, "DlssNr", "PreUpscale") == (True, "false")
    assert ini_get_exact(ini, "DlssNr", "DualFeature") == (True, "false")
    assert ini_get_exact(ini, "FrameGen", "Enabled") == (True, "true")
    assert ini_get_exact(ini, "FrameGen", "FGInput") == (True, "upscaler")
    assert ini_get_exact(ini, "FrameGen", "FGOutput") == (True, "fsrfg")
    assert ini_get_exact(ini, "OptiFG", "HUDFix") == (True, "true")
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
    assert installed.strategy_options.frame_generation is FrameGenerationMode.FSR
    assert installed.strategy_options.nr_placement is NrPlacement.AFTER
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


def test_configure_resolves_auto_to_fsr_and_records_detected_gpu(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    ctx.frame_generation = FrameGenerationMode.AUTO
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.get_platform_adapter",
        return_value=SimpleNamespace(platform_name="windows"),
    )
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.detect_nvidia_gpu_generation",
        return_value=NvidiaGpuInfo("NVIDIA GeForce RTX 5060 Ti", NvidiaGpuGeneration.RTX50),
    )

    assert StepConfigureOptiScaler().execute(ctx)
    assert ctx.frame_generation is FrameGenerationMode.FSR
    assert ctx.gpu_generation == "rtx50"


def test_configure_resolves_auto_proxy_dynamically(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    ctx.proxy_name = "auto"
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.get_platform_adapter",
        return_value=SimpleNamespace(platform_name="windows"),
    )
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.detect_nvidia_gpu_generation",
        return_value=NvidiaGpuInfo("RTX 4090", NvidiaGpuGeneration.RTX40),
    )

    assert StepConfigureOptiScaler().execute(ctx)
    assert ctx.proxy_name == "dxgi.dll"

    ctx.proxy_name = "auto"
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.detect_optiscaler_proxy",
        return_value="winmm.dll",
    )
    assert StepConfigureOptiScaler().execute(ctx)
    assert ctx.proxy_name == "winmm.dll"


def test_linux_configure_requires_dx12_and_resolves_proton_prefix(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    prefix = SteamPrefixInfo(appid="123", prefix_path=tmp_path / "pfx", game_dir=tmp_path)
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.get_platform_adapter",
        return_value=SimpleNamespace(platform_name="linux"),
    )
    find_prefix = mocker.patch(
        "dlss5_enabler.operations.optiscaler.ProtonManager.find_prefix_for_game",
        return_value=prefix,
    )

    assert StepConfigureOptiScaler().execute(ctx)
    assert ctx.proton_prefix == prefix
    find_prefix.assert_called_once_with(ctx.game_exe)

    ctx.analysis = TargetAnalysis(PeArch.X64, (DetectedApi.D3D11,), True)
    assert not StepConfigureOptiScaler().execute(ctx)
    assert "DirectX 12" in ctx.error_message


def test_linux_wine_override_round_trip_restores_original_registry(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    prefix_path = tmp_path / "pfx"
    prefix_path.mkdir()
    user_reg = prefix_path / "user.reg"
    original = "WINE REGISTRY Version 2\n"
    user_reg.write_text(original, encoding="utf-8")
    ctx.proton_prefix = SteamPrefixInfo(appid="123", prefix_path=prefix_path, game_dir=tmp_path)
    ctx.proxy_name = "winmm.dll"
    mocker.patch(
        "dlss5_enabler.operations.optiscaler.get_platform_adapter",
        return_value=SimpleNamespace(platform_name="linux"),
    )

    assert StepConfigureOptiScalerWineOverrides().execute(ctx)
    assert WineRegParser.read_overrides(user_reg)["winmm"] == "native,builtin"
    assert ctx.record.registry_touched[0].value_name == "winmm"
    assert ctx.record.proton_prefix == prefix_path.as_posix()
    assert revert_record_mutations(ctx.record)
    assert "winmm" not in WineRegParser.read_overrides(user_reg)


@pytest.mark.parametrize(
    ("placement", "pre_upscale", "dual_feature"),
    [
        (NrPlacement.AFTER, "false", "false"),
        (NrPlacement.BEFORE, "true", "false"),
        (NrPlacement.INSIDE, "false", "true"),
    ],
)
def test_prepare_configures_explicit_nr_placement(
    tmp_path: Path,
    mocker: MockerFixture,
    placement: NrPlacement,
    pre_upscale: str,
    dual_feature: str,
) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    ctx.nr_placement = placement
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.operations.optiscaler.get_cache_dir", return_value=cache)

    assert StepPrepareOptiScaler().execute(ctx)
    ini = next(path for path in ctx.staged_files.values() if path.name == "OptiScaler.ini")
    assert ini_get_exact(ini, "DlssNr", "PreUpscale") == (True, pre_upscale)
    assert ini_get_exact(ini, "DlssNr", "DualFeature") == (True, dual_feature)


def test_dlssg_runtime_is_installed_and_recorded_for_rtx40(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = _context(tmp_path, _archive(tmp_path / "opti.zip"))
    ctx.frame_generation = FrameGenerationMode.DLSSG
    ctx.fg_multiplier = 4
    ctx.gpu_generation = "rtx40"
    fg_path = tmp_path / "source-nvngx_dlssg.dll"
    fg_path.write_bytes(b"fg-runtime")
    dlssg = DlssgBundle()
    dlssg.dll_path = fg_path
    ctx.dlssg_bundle = dlssg
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.operations.optiscaler.get_cache_dir", return_value=cache)

    assert StepPrepareOptiScaler().execute(ctx)
    ini = next(path for path in ctx.staged_files.values() if path.name == "OptiScaler.ini")
    assert ini_get_exact(ini, "FrameGen", "FGOutput") == (True, "dlssg")
    assert ini_get_exact(ini, "DLSSG", "InterpolationCount") == (True, "3")
    assert ini_get_exact(ini, "DLSSG", "AdaMfgUnlock") == (True, "true")
    assert ini_get_exact(ini, "DLSSG", "AdaBlackwellKernels") == (True, "true")
    assert ini_get_exact(ini, "NvApi", "DisableFlipMetering") == (True, "true")
    assert StepInstallOptiScaler().execute(ctx)
    assert (tmp_path / "OptiScaler" / "streamline" / "nvngx_dlssg.dll").read_bytes() == b"fg-runtime"


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


def test_fetch_optiscaler_discovers_official_v4_with_dlss_asset(tmp_path: Path, mocker: MockerFixture) -> None:
    source = _seven_archive(tmp_path / "source.7z")
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=cache)
    metadata = mocker.patch(
        "dlss5_enabler.network.sources.http_get_json",
        return_value={
            "tag_name": V4_REVISION,
            "assets": [
                {
                    "name": "OptiScaler_v4.7z",
                    "browser_download_url": "https://example.com/plain.7z",
                },
                {
                    "name": "OptiScaler_v4_with_DLSS.7z",
                    "browser_download_url": "https://example.com/with-dlss.7z",
                },
            ],
        },
    )

    def download(_url: str, destination: Path | str, progress_fn: object = None) -> Path:
        del progress_fn
        output = Path(destination)
        output.write_bytes(source.read_bytes())
        return output

    downloaded = mocker.patch("dlss5_enabler.network.sources.http_download_file", side_effect=download)

    bundle = fetch_optiscaler(lambda _message: None)

    assert bundle.variant == "y4my4my4m-v4"
    assert bundle.source_revision == V4_REVISION
    assert bundle.includes_dlssg
    assert downloaded.call_args.args[0] == "https://example.com/with-dlss.7z"
    metadata.assert_called_once_with(
        "https://api.github.com/repos/y4my4my4m/OptiScaler_DLSSNR_Multipass_MFG/releases/latest"
    )


def test_local_v4_archive_is_accepted_by_verified_digest(tmp_path: Path, mocker: MockerFixture) -> None:
    source = _seven_archive(tmp_path / "source.7z")
    cache = tmp_path / "cache"
    cache.mkdir()
    mocker.patch("dlss5_enabler.network.sources.get_cache_dir", return_value=cache)
    mocker.patch("dlss5_enabler.network.sources.sha256_file", return_value=V4_DIGEST)

    bundle = fetch_optiscaler(lambda _message: None, archive_path=source)

    assert bundle.variant == "y4my4my4m-v4"
    assert bundle.source_revision == V4_REVISION
    assert bundle.includes_dlssg
    assert bundle.archive_path is not None and bundle.archive_path.suffix == ".7z"
