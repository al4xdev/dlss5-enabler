import struct
import zipfile
from pathlib import Path
from unittest.mock import Mock

import py7zr
import pytest
from pytest_mock import MockerFixture

from dlss5_enabler.core.pe import IMAGE_DOS_SIGNATURE, IMAGE_FILE_MACHINE_AMD64, IMAGE_FILE_MACHINE_I386, PeArch
from dlss5_enabler.core.record import (
    IniTouch,
    InstallOptions,
    InstallRecord,
    RecordedFile,
    RegistryTouch,
    record_load,
    record_save,
)
from dlss5_enabler.network.resolver import ResolutionWarning, ResolutionWarningCode
from dlss5_enabler.network.sources import (
    DgvoodooBundle,
    FeederBundle,
    LumeniteBundle,
    NgxBundle,
    RenoDxBundle,
    ReshadeBundle,
    ReshadeHeaders,
)
from dlss5_enabler.operations.install import build_install_pipeline, run_install
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineRunner, PipelineStep
from dlss5_enabler.operations.reshade import (
    ensure_mv_provider_def,
    extract_reshade_dlls_from_installer,
    normalize_search_paths,
)
from dlss5_enabler.operations.steps import (
    StepCleanPreviousInstall,
    StepConfigureMotionVectors,
    StepConfigureWineOverrides,
    StepFetchUpstream,
    StepInjectFeederAndHeaders,
    StepInjectRenoDxAndNgx,
    StepInstallD3D9Translation,
    StepInstallReShade,
    StepInstallVulkanLayer,
    StepMirrorDualLocations,
    StepSaveRecord,
    StepValidateTarget,
    _place_file,
)
from dlss5_enabler.operations.uninstall import run_uninstall
from dlss5_enabler.operations.update import GameUpdateStatus, run_update
from dlss5_enabler.platform.proton import SteamPrefixInfo, WineRegParser


class DummySuccessStep(PipelineStep):
    @property
    def name(self) -> str:
        return "DummySuccess"

    @property
    def description(self) -> str:
        return "A dummy success step"

    def execute(self, ctx: PipelineContext) -> bool:
        return True


class DummyFailureStep(PipelineStep):
    @property
    def name(self) -> str:
        return "DummyFailure"

    @property
    def description(self) -> str:
        return "A dummy failure step"

    def execute(self, ctx: PipelineContext) -> bool:
        ctx.error_message = "Deliberate failure"
        return False


class DummyExceptionStep(PipelineStep):
    @property
    def name(self) -> str:
        return "DummyException"

    @property
    def description(self) -> str:
        return "A step that crashes"

    def execute(self, ctx: PipelineContext) -> bool:
        raise RuntimeError("Crash in step")


class DummyRollbackStep(PipelineStep):
    def __init__(self) -> None:
        self.rolled_back = False

    @property
    def name(self) -> str:
        return "DummyRollback"

    @property
    def description(self) -> str:
        return "A dummy step with rollback"

    def execute(self, ctx: PipelineContext) -> bool:
        return True

    def rollback(self, ctx: PipelineContext) -> None:
        self.rolled_back = True


def _mock_upstream_fetches(mocker: MockerFixture) -> dict[str, Mock]:
    bundles = {
        "fetch_reshade": ReshadeBundle(),
        "fetch_feeder": FeederBundle(),
        "fetch_renodx_dlss5": RenoDxBundle(),
        "fetch_ngx_dlls": NgxBundle(),
        "fetch_reshade_headers": ReshadeHeaders(),
        "fetch_dgvoodoo": DgvoodooBundle(),
        "fetch_lumenite": LumeniteBundle(),
    }
    return {
        name: mocker.patch(f"dlss5_enabler.operations.steps.{name}", return_value=bundle)
        for name, bundle in bundles.items()
    }


def _upstream_context(tmp_path: Path) -> PipelineContext:
    ctx = PipelineContext(
        game_exe=tmp_path / "game.exe",
        d3d9_translate=True,
        install_lumenite=True,
    )
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.need_reshade = True
    ctx.record = InstallRecord(game_exe=ctx.game_exe.as_posix(), game_dir=tmp_path.as_posix())
    return ctx


def _synthetic_pe(machine: int) -> bytes:
    dos_header = bytearray(64)
    struct.pack_into("<H", dos_header, 0, IMAGE_DOS_SIGNATURE)
    struct.pack_into("<I", dos_header, 0x3C, 64)
    file_header = bytearray(20)
    struct.pack_into("<H", file_header, 0, machine)
    struct.pack_into("<H", file_header, 16, 240)
    optional_header = bytearray(240)
    struct.pack_into("<H", optional_header, 0, 0x10B if machine == IMAGE_FILE_MACHINE_I386 else 0x20B)
    return bytes(dos_header + b"PE\x00\x00" + file_header + optional_header)


def _synthetic_bundles(artifacts: Path) -> dict[str, object]:
    artifacts.mkdir()

    def artifact(name: str) -> Path:
        path = artifacts / name
        path.write_bytes(f"synthetic:{name}".encode())
        return path

    reshade = ReshadeBundle()
    reshade.setup_exe_path = artifact("ReShade_Setup_Addon.exe")
    feeder = FeederBundle()
    feeder.addon32 = artifact("dlss5-feed.addon32")
    feeder.addon64 = artifact("dlss5-feed.addon64")
    feeder.fx_shader = artifact("DLSS5_Feed.fx")
    feeder.host64_exe = artifact("dlss5-feed-host64.exe")
    renodx = RenoDxBundle()
    renodx.addon64_path = artifact("renodx-dlss5.addon64")
    ngx = NgxBundle()
    ngx.nr_dll_path = artifact("nvngx_dlssnr.dll")
    ngx.sr_dll_path = artifact("nvngx_dlss.dll")
    headers = ReshadeHeaders()
    headers.fxh_path = artifact("ReShade.fxh")
    headers.ui_fxh_path = artifact("ReShadeUI.fxh")
    headers.drawtext_path = artifact("DrawText.fxh")
    return {
        "fetch_reshade": reshade,
        "fetch_feeder": feeder,
        "fetch_renodx_dlss5": renodx,
        "fetch_ngx_dlls": ngx,
        "fetch_reshade_headers": headers,
    }


def test_pipeline_runner_success(tmp_path: Path) -> None:
    exe = tmp_path / "game.exe"
    ctx = PipelineContext(game_exe=exe)

    step1 = DummySuccessStep()
    step2 = DummySuccessStep()
    runner = PipelineRunner([step1, step2])

    assert runner.run(ctx)
    assert not ctx.failed_step
    assert not ctx.error_message


def test_pipeline_success_summarizes_upstream_fallbacks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ctx = PipelineContext(game_exe=tmp_path / "game.exe")
    ctx.upstream_warnings.append(
        ResolutionWarning(
            code=ResolutionWarningCode.STABLE_FALLBACK_USED,
            component="feeder",
            provider="github",
            reason="latest layout was incompatible",
            latest_revision="v2",
            fallback_revision="v1",
            log_path=tmp_path / "install.log",
        )
    )

    assert PipelineRunner([DummySuccessStep()]).run(ctx)
    output = capsys.readouterr().out
    assert "Validated upstream fallbacks used" in output
    assert "UPSTREAM_STABLE_FALLBACK_USED" in output
    assert "provider=github" in output
    assert "latest=v2" in output
    assert "fallback=v1" in output


def test_step_fetch_upstream_runs_every_enabled_fetch(tmp_path: Path, mocker: MockerFixture) -> None:
    fetches = _mock_upstream_fetches(mocker)

    assert StepFetchUpstream().execute(_upstream_context(tmp_path))
    for fetch in fetches.values():
        fetch.assert_called_once()


def test_step_fetch_upstream_skips_feeder_components_for_native_dlss(tmp_path: Path, mocker: MockerFixture) -> None:
    fetches = _mock_upstream_fetches(mocker)
    ctx = _upstream_context(tmp_path)
    ctx.install_feeder = False

    assert StepFetchUpstream().execute(ctx)
    fetches["fetch_feeder"].assert_not_called()
    fetches["fetch_reshade_headers"].assert_called_once()
    fetches["fetch_lumenite"].assert_called_once()
    fetches["fetch_reshade"].assert_called_once()
    fetches["fetch_renodx_dlss5"].assert_called_once()
    fetches["fetch_ngx_dlls"].assert_called_once()


def test_d3d9_translation_disables_dgvoodoo_watermark(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    d3d9_dll = source_dir / "D3D9.dll"
    cpl = source_dir / "dgVoodooCpl.exe"
    conf = source_dir / "dgVoodoo.conf"
    d3d9_dll.write_bytes(b"D3D9")
    cpl.write_bytes(b"CPL")
    conf.write_text("[DirectX]\ndgVoodooWatermark=true\n", encoding="utf-8")
    bundle = DgvoodooBundle()
    bundle.d3d9_dll = d3d9_dll
    bundle.cpl = cpl
    bundle.conf = conf
    ctx = PipelineContext(game_exe=tmp_path / "game.exe", d3d9_translate=True)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.dgvoodoo_bundle = bundle
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))

    assert StepInstallD3D9Translation().execute(ctx)
    installed_conf = (tmp_path / "dgVoodoo.conf").read_text(encoding="utf-8")
    assert "dgVoodooWatermark=false" in installed_conf


def test_native_dlss_installs_headers_without_feeder_components(tmp_path: Path) -> None:
    headers = ReshadeHeaders()
    headers.fxh_path = tmp_path / "ReShade.fxh"
    headers.ui_fxh_path = tmp_path / "ReShadeUI.fxh"
    headers.drawtext_path = tmp_path / "DrawText.fxh"
    for path in (headers.fxh_path, headers.ui_fxh_path, headers.drawtext_path):
        path.write_bytes(path.name.encode())

    ctx = PipelineContext(game_exe=tmp_path / "game.exe", install_lumenite=True)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.install_feeder = False
    ctx.headers_bundle = headers
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))

    assert StepInjectFeederAndHeaders().execute(ctx)
    assert (tmp_path / "reshade-shaders" / "Shaders" / "ReShade.fxh").is_file()
    assert not (tmp_path / "dlss5-feed.addon64").exists()
    assert not (tmp_path / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx").exists()


def test_native_dlss_preserves_game_dlss_runtime(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    addon = artifacts / "renodx-dlss5.addon64"
    nr = artifacts / "nvngx_dlssnr.dll"
    sr = artifacts / "nvngx_dlss.dll"
    addon.write_bytes(b"ADDON")
    nr.write_bytes(b"NR")
    sr.write_bytes(b"NEW_SR")
    renodx = RenoDxBundle()
    renodx.addon64_path = addon
    ngx = NgxBundle()
    ngx.nr_dll_path = nr
    ngx.sr_dll_path = sr
    ctx = PipelineContext(game_exe=tmp_path / "game.exe")
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.install_feeder = False
    ctx.renodx_bundle = renodx
    ctx.ngx_bundle = ngx
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))
    target_sr = tmp_path / "nvngx_dlss.dll"
    target_sr.write_bytes(b"GAME_SR")

    assert StepInjectRenoDxAndNgx().execute(ctx)
    assert target_sr.read_bytes() == b"GAME_SR"
    assert (tmp_path / "renodx-dlss5.addon64").read_bytes() == b"ADDON"
    assert (tmp_path / "nvngx_dlssnr.dll").read_bytes() == b"NR"


def test_32bit_host_uses_reshade_extraction_fallback(tmp_path: Path, mocker: MockerFixture) -> None:
    setup = tmp_path / "ReShade_Setup_Addon.exe"
    host_exe = tmp_path / "dlss5-feed-host64.exe"
    addon = tmp_path / "renodx-dlss5.addon64"
    nr_dll = tmp_path / "nvngx_dlssnr.dll"
    sr_dll = tmp_path / "nvngx_dlss.dll"
    extracted_dll = tmp_path / "ReShade64.dll"
    for path in (setup, host_exe, addon, nr_dll, sr_dll, extracted_dll):
        path.write_bytes(path.name.encode())
    reshade = ReshadeBundle()
    reshade.setup_exe_path = setup
    feeder = FeederBundle()
    feeder.host64_exe = host_exe
    renodx = RenoDxBundle()
    renodx.addon64_path = addon
    ngx = NgxBundle()
    ngx.nr_dll_path = nr_dll
    ngx.sr_dll_path = sr_dll
    ctx = PipelineContext(game_exe=tmp_path / "game.exe")
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.is_32bit = True
    ctx.reshade_bundle = reshade
    ctx.feeder_bundle = feeder
    ctx.renodx_bundle = renodx
    ctx.ngx_bundle = ngx
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))
    mocker.patch("dlss5_enabler.operations.steps.get_cache_dir", return_value=tmp_path)
    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", return_value=False)
    mocker.patch(
        "dlss5_enabler.operations.steps.extract_reshade_dlls_from_installer",
        return_value={"reshade64.dll": extracted_dll},
    )

    assert StepInjectRenoDxAndNgx().execute(ctx)
    host_dir = tmp_path / "host64"
    assert (host_dir / "dxgi.dll").read_bytes() == b"ReShade64.dll"
    host_ini = (host_dir / "ReShade.ini").read_text(encoding="utf-8")
    assert "EffectSearchPaths=./reshade-shaders/Shaders/**" in host_ini
    assert "TextureSearchPaths=./reshade-shaders/Textures/**" in host_ini
    assert (host_dir / "renodx-dlss5.addon64").is_file()


def test_32bit_host_missing_extraction_rolls_back(tmp_path: Path, mocker: MockerFixture) -> None:
    setup = tmp_path / "ReShade_Setup_Addon.exe"
    host_exe = tmp_path / "dlss5-feed-host64.exe"
    addon = tmp_path / "renodx-dlss5.addon64"
    nr_dll = tmp_path / "nvngx_dlssnr.dll"
    sr_dll = tmp_path / "nvngx_dlss.dll"
    for path in (setup, host_exe, addon, nr_dll, sr_dll):
        path.write_bytes(path.name.encode())
    reshade = ReshadeBundle()
    reshade.setup_exe_path = setup
    feeder = FeederBundle()
    feeder.host64_exe = host_exe
    renodx = RenoDxBundle()
    renodx.addon64_path = addon
    ngx = NgxBundle()
    ngx.nr_dll_path = nr_dll
    ngx.sr_dll_path = sr_dll
    ctx = PipelineContext(game_exe=tmp_path / "game.exe")
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.is_32bit = True
    ctx.reshade_bundle = reshade
    ctx.feeder_bundle = feeder
    ctx.renodx_bundle = renodx
    ctx.ngx_bundle = ngx
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))
    mocker.patch("dlss5_enabler.operations.steps.get_cache_dir", return_value=tmp_path)
    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", return_value=False)
    mocker.patch("dlss5_enabler.operations.steps.extract_reshade_dlls_from_installer", return_value={})

    assert not PipelineRunner([StepInjectRenoDxAndNgx()]).run(ctx)
    assert not (tmp_path / "host64").exists()


def test_native_dlss_keeps_lumenite_without_feeder_definition(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    source = staging / "reshade-shaders" / "Shaders" / "lumenite_Kernel.fx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"KERNEL")
    ini = tmp_path / "ReShade.ini"
    ini.write_text("[GENERAL]\n", encoding="utf-8")
    lumenite = LumeniteBundle()
    lumenite.staging_dir = staging
    lumenite.files = [source]
    ctx = PipelineContext(game_exe=tmp_path / "game.exe", install_lumenite=True)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.install_feeder = False
    ctx.lumenite_bundle = lumenite
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))

    assert StepConfigureMotionVectors().execute(ctx)
    assert (tmp_path / "reshade-shaders" / "Shaders" / "lumenite_Kernel.fx").is_file()
    assert "DLSS5_MV_PROVIDER" not in ini.read_text(encoding="utf-8")


def test_install_pipeline_resolves_upstreams_before_cleaning_previous_install() -> None:
    names = tuple(step.name for step in build_install_pipeline().steps)

    assert names.index("FetchUpstream") < names.index("CleanPreviousInstall")


def test_fetch_failure_does_not_mutate_existing_installation(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    installed = tmp_path / "dxgi.dll"
    installed.write_bytes(b"OLD_INSTALLATION")
    ini = tmp_path / "ReShade.ini"
    ini.write_bytes(b"OLD_INI")
    registry = tmp_path / "user.reg"
    registry.write_bytes(b"OLD_REGISTRY")
    record = InstallRecord(
        game_exe=game_exe.as_posix(),
        game_dir=tmp_path.as_posix(),
        files=[RecordedFile(path=installed.as_posix())],
        ini_touched=[IniTouch(path=ini.as_posix(), section="GENERAL", key="Value", original="old")],
        registry_touched=[
            RegistryTouch(reg_path=registry.as_posix(), key="Software\\Wine\\DllOverrides", value_name="dxgi")
        ],
    )
    record.record_path().write_bytes(record.model_dump_json().encode())
    original = {path: path.read_bytes() for path in (installed, ini, registry, record.record_path())}
    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.record = InstallRecord(game_exe=game_exe.as_posix(), game_dir=tmp_path.as_posix())
    fetches = _mock_upstream_fetches(mocker)
    fetches["fetch_feeder"].side_effect = RuntimeError("both upstream candidates failed")
    clean = mocker.patch.object(StepCleanPreviousInstall, "execute")

    assert not PipelineRunner([StepFetchUpstream(), StepCleanPreviousInstall()]).run(ctx)
    clean.assert_not_called()
    assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize(
    "failed_fetch",
    (
        "fetch_reshade",
        "fetch_feeder",
        "fetch_renodx_dlss5",
        "fetch_ngx_dlls",
        "fetch_reshade_headers",
        "fetch_dgvoodoo",
        "fetch_lumenite",
    ),
)
def test_step_fetch_upstream_propagates_every_fetch_failure(
    failed_fetch: str, tmp_path: Path, mocker: MockerFixture
) -> None:
    fetches = _mock_upstream_fetches(mocker)
    fetches[failed_fetch].side_effect = RuntimeError(f"{failed_fetch} failed")

    ctx = _upstream_context(tmp_path)
    assert not PipelineRunner([StepFetchUpstream()]).run(ctx)
    assert ctx.failed_step == "FetchUpstream"
    assert ctx.error_message == f"{failed_fetch} failed"
    fetches[failed_fetch].assert_called_once()


def test_pipeline_runner_failure_triggers_rollback(tmp_path: Path) -> None:
    exe = tmp_path / "game.exe"
    ctx = PipelineContext(game_exe=exe)

    step1 = DummyRollbackStep()
    step2 = DummyFailureStep()
    runner = PipelineRunner([step1, step2])

    assert not runner.run(ctx)
    assert ctx.failed_step == "DummyFailure"
    assert ctx.error_message == "Deliberate failure"
    assert step1.rolled_back


def test_pipeline_runner_exception_triggers_rollback(tmp_path: Path) -> None:
    exe = tmp_path / "game.exe"
    ctx = PipelineContext(game_exe=exe)

    step1 = DummyRollbackStep()
    step2 = DummyExceptionStep()
    runner = PipelineRunner([step1, step2])

    assert not runner.run(ctx)
    assert ctx.failed_step == "DummyException"
    assert "Crash in step" in ctx.error_message
    assert step1.rolled_back


def test_pipeline_runner_rollback_exception_resilience(tmp_path: Path, mocker: MockerFixture) -> None:
    exe = tmp_path / "game.exe"
    ctx = PipelineContext(game_exe=exe)

    step1 = DummyRollbackStep()
    mocker.patch.object(step1, "rollback", side_effect=Exception("Rollback error"))

    step2 = DummyFailureStep()
    runner = PipelineRunner([step1, step2])

    assert not runner.run(ctx)
    assert ctx.failed_step == "DummyFailure"


def test_step_validate_target_success_x64(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "Control_DX12.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.operations.steps.file_is_writable", return_value=True)

    ctx = PipelineContext(game_exe=game_exe)
    step = StepValidateTarget()

    assert step.execute(ctx)
    assert ctx.pe_arch == PeArch.X64
    assert not ctx.is_32bit
    assert ctx.reshade_api == "dxgi"
    assert ctx.record.architecture == "x64"


def test_step_validate_target_selects_direct_path_for_native_dlss(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "native_dlss.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.operations.steps.detect_native_dlss", return_value=True)
    mocker.patch("dlss5_enabler.operations.steps.file_is_writable", return_value=True)

    ctx = PipelineContext(game_exe=game_exe)

    assert StepValidateTarget().execute(ctx)
    assert ctx.native_dlss_detected
    assert not ctx.install_feeder
    assert ctx.record.lumenite_installed
    assert ctx.record.native_dlss_detected


def test_step_validate_target_opengl_x86(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "gl_game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.X86)
    mocker.patch("dlss5_enabler.operations.steps.file_is_writable", return_value=True)

    ctx = PipelineContext(game_exe=game_exe, opengl=True)
    step = StepValidateTarget()

    assert step.execute(ctx)
    assert ctx.is_32bit
    assert ctx.reshade_api == "opengl"
    assert ctx.reshade_dll_name == "opengl32.dll"


def test_step_validate_target_missing_file(tmp_path: Path) -> None:
    game_exe = tmp_path / "missing.exe"
    ctx = PipelineContext(game_exe=game_exe)
    step = StepValidateTarget()

    assert not step.execute(ctx)
    assert "not found" in ctx.error_message


def test_step_validate_target_unsupported_arch(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "arm64_game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.ARM64)
    ctx = PipelineContext(game_exe=game_exe)
    step = StepValidateTarget()

    assert not step.execute(ctx)
    assert "Unsupported architecture" in ctx.error_message


def test_step_validate_target_locked_executable(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "running_game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.operations.steps.file_is_writable", return_value=False)

    ctx = PipelineContext(game_exe=game_exe)
    step = StepValidateTarget()

    assert not step.execute(ctx)
    assert "locked" in ctx.error_message


def test_step_validate_target_rejects_running_game(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "running_game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")
    adapter = mocker.Mock()
    adapter.is_game_running.return_value = True
    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.operations.steps.get_platform_adapter", return_value=adapter)

    ctx = PipelineContext(game_exe=game_exe)

    assert not StepValidateTarget().execute(ctx)
    assert "currently running" in ctx.error_message
    adapter.is_game_running.assert_called_once_with(game_exe.resolve())


def test_step_validate_target_write_protected_directory(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "protected_game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.detect_pe_arch", return_value=PeArch.X64)
    mocker.patch("dlss5_enabler.operations.steps.file_is_writable", return_value=True)
    mocker.patch("dlss5_enabler.operations.steps.is_directory_writable", return_value=False)

    ctx = PipelineContext(game_exe=game_exe)
    step = StepValidateTarget()

    assert not step.execute(ctx)
    assert "write-protected" in ctx.error_message


def test_step_clean_previous_install(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.record_exists", return_value=True)
    mocker.patch(
        "dlss5_enabler.operations.steps.record_load",
        return_value=InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir)),
    )
    mocker.patch("dlss5_enabler.operations.steps.capture_install_snapshot", return_value=mocker.MagicMock())
    mocker.patch("dlss5_enabler.operations.steps.run_uninstall", return_value=True)

    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = game_dir
    step = StepCleanPreviousInstall()

    assert step.execute(ctx)


def test_step_clean_previous_install_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"MZ_DUMMY")

    mocker.patch("dlss5_enabler.operations.steps.record_exists", return_value=True)
    mocker.patch(
        "dlss5_enabler.operations.steps.record_load",
        return_value=InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir)),
    )
    mocker.patch("dlss5_enabler.operations.steps.capture_install_snapshot", return_value=mocker.MagicMock())
    mocker.patch("dlss5_enabler.operations.steps.run_uninstall", return_value=False)

    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = game_dir
    step = StepCleanPreviousInstall()

    assert not step.execute(ctx)
    assert "Failed to cleanly remove" in ctx.error_message


def test_failed_refresh_restores_previous_installation(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"MZ")
    installed = game_dir / "dxgi.dll"
    installed.write_bytes(b"OLD_DLSS5_ENABLER")
    backup = game_dir / "dxgi.dll.dlss5-enabler.bak"
    backup.write_bytes(b"GAME_ORIGINAL")
    previous = InstallRecord(
        game_exe=str(game_exe),
        game_dir=str(game_dir),
        files=[RecordedFile(path=str(installed), backup=str(backup))],
    )
    previous.record_path().write_text(previous.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)
    mocker.patch("dlss5_enabler.operations.uninstall.index_add", return_value=True)
    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = game_dir
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir))

    assert not PipelineRunner([StepCleanPreviousInstall(), DummyFailureStep()]).run(ctx)
    assert installed.read_bytes() == b"OLD_DLSS5_ENABLER"
    assert backup.read_bytes() == b"GAME_ORIGINAL"
    assert previous.record_path().is_file()


def test_place_file_with_backup(tmp_path: Path) -> None:
    src = tmp_path / "source.dll"
    src.write_bytes(b"NEW_VERSION")

    dst = tmp_path / "game" / "target.dll"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"ORIGINAL_VERSION")

    ctx = PipelineContext(game_exe=tmp_path / "game" / "game.exe")
    _place_file(ctx, src, dst)

    assert dst.read_bytes() == b"NEW_VERSION"
    backup_file = dst.with_suffix(".dll.dlss5-enabler.bak")
    assert backup_file.is_file()
    assert backup_file.read_bytes() == b"ORIGINAL_VERSION"

    assert len(ctx.record.files) == 1
    assert ctx.record.files[0].backup == backup_file.as_posix()


def test_place_file_never_overwrites_unreconciled_backup(tmp_path: Path) -> None:
    src = tmp_path / "source.dll"
    src.write_bytes(b"NEW_VERSION")
    dst = tmp_path / "target.dll"
    dst.write_bytes(b"ORIGINAL_VERSION")
    old_backup = dst.with_suffix(".dll.dlss5-enabler.bak")
    old_backup.write_bytes(b"OLDER_ORIGINAL")
    ctx = PipelineContext(game_exe=tmp_path / "game.exe")

    _place_file(ctx, src, dst)

    assert old_backup.read_bytes() == b"OLDER_ORIGINAL"
    assert Path(ctx.record.files[0].backup).name == "target.dll.dlss5-enabler.bak.1"
    assert Path(ctx.record.files[0].backup).read_bytes() == b"ORIGINAL_VERSION"


def test_pipeline_failure_rolls_back_recorded_file(tmp_path: Path) -> None:
    src = tmp_path / "new.dll"
    src.write_bytes(b"NEW")
    dst = tmp_path / "game" / "target.dll"
    dst.parent.mkdir()
    dst.write_bytes(b"ORIGINAL")
    ctx = PipelineContext(game_exe=tmp_path / "game" / "game.exe")
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(dst.parent))

    class MutatingFailure(PipelineStep):
        @property
        def name(self) -> str:
            return "MutatingFailure"

        @property
        def description(self) -> str:
            return "Mutates and fails"

        def execute(self, ctx: PipelineContext) -> bool:
            _place_file(ctx, src, dst)
            return False

    assert not PipelineRunner([MutatingFailure()]).run(ctx)
    assert dst.read_bytes() == b"ORIGINAL"
    assert not Path(ctx.record.files[0].backup).exists()


def test_normalize_search_paths(tmp_path: Path) -> None:
    ini_file = tmp_path / "ReShade.ini"
    ini_file.write_text(
        "[GENERAL]\n"
        "EffectSearchPaths = .\\reshade-shaders\\Shaders\\**\\**\n"
        "TextureSearchPaths = .\\reshade-shaders\\Textures\\**/**\n",
        encoding="utf-8",
    )

    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")
    normalize_search_paths(ini_file, rec)

    content = ini_file.read_text(encoding="utf-8")
    assert ".\\reshade-shaders\\Shaders\\**" in content
    assert "**\\**" not in content
    assert "**/**" not in content
    assert len(rec.ini_touched) == 2


def test_normalize_search_paths_deduplicates_reshade_runtime_values(tmp_path: Path) -> None:
    ini_file = tmp_path / "ReShade.ini"
    ini_file.write_text(
        "[GENERAL]\n"
        "EffectSearchPaths=.\\reshade-shaders\\Shaders\\**\\**,./reshade-shaders/Shaders/**\n"
        "TextureSearchPaths=.\\reshade-shaders\\Textures\\**\\**,./reshade-shaders/Textures/**\n",
        encoding="utf-8",
    )
    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")

    assert normalize_search_paths(ini_file, rec)

    content = ini_file.read_text(encoding="utf-8")
    assert "EffectSearchPaths=.\\reshade-shaders\\Shaders\\**\n" in content
    assert "TextureSearchPaths=.\\reshade-shaders\\Textures\\**\n" in content
    assert "," not in content
    assert len(rec.ini_touched) == 2


def test_normalize_search_paths_reports_write_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    ini_file = tmp_path / "ReShade.ini"
    ini_file.write_text("[GENERAL]\nEffectSearchPaths=.\\\n", encoding="utf-8")
    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")
    mocker.patch("dlss5_enabler.operations.reshade.ini_set_exact", return_value=False)

    assert not normalize_search_paths(ini_file, rec)
    assert rec.ini_touched == []


def test_validate_refuses_unknown_existing_proxy(tmp_path: Path) -> None:
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(_synthetic_pe(IMAGE_FILE_MACHINE_AMD64))
    (tmp_path / "dxgi.dll").write_bytes(b"NOT_RESHADER")
    ctx = PipelineContext(game_exe=game_exe)

    assert not StepValidateTarget().execute(ctx)
    assert "refusing to assume it is ReShade" in ctx.error_message


def test_ensure_mv_provider_def(tmp_path: Path) -> None:
    ini_file = tmp_path / "ReShade.ini"
    ini_file.write_text(
        "[GENERAL]\n"
        "PreprocessorDefinitions = RESHADE_DEPTH_INPUT_IS_UPSIDE_DOWN=0\n"
        "PreProcessorDefinitions = OLD_TYPO\n",
        encoding="utf-8",
    )

    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")
    ensure_mv_provider_def(ini_file, rec)

    content = ini_file.read_text(encoding="utf-8")
    assert "DLSS5_MV_PROVIDER=3" in content
    assert "PreProcessorDefinitions=" not in content


def test_run_uninstall_full(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    placed_file = game_dir / "dxgi.dll"
    placed_file.write_bytes(b"MOD_DLL")

    backup_file = game_dir / "original.dll.dlss5-enabler.bak"
    backup_file.write_bytes(b"ORIGINAL_DLL")
    restored_target = game_dir / "original.dll"

    ini_file = game_dir / "ReShade.ini"
    ini_file.write_text("[GENERAL]\nKey=Modified\n", encoding="utf-8")

    rec = InstallRecord(
        game_exe=str(game_dir / "game.exe"),
        game_dir=str(game_dir),
        reshade_by_us=True,
        files=[
            RecordedFile(path=str(placed_file), backup=""),
            RecordedFile(path=str(restored_target), backup=str(backup_file)),
        ],
        ini_touched=[
            IniTouch(path=str(ini_file), section="GENERAL", key="Key", original="OriginalValue"),
        ],
    )
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)

    assert run_uninstall(game_dir)

    assert not placed_file.exists()
    assert restored_target.is_file()
    assert restored_target.read_bytes() == b"ORIGINAL_DLL"
    assert not backup_file.exists()

    ini_content = ini_file.read_text(encoding="utf-8")
    assert "Key=OriginalValue" in ini_content
    assert not rec.record_path().exists()


def test_run_uninstall_removes_feeder_host_runtime_artifacts(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    host_dir = game_dir / "host64"
    host_dir.mkdir(parents=True)
    host_log = host_dir / "dlss5-feed-host.log"
    screenshot = host_dir / "dlss5-feed-host64 2026-09-03 02-06-49_1.png"
    host_log.write_bytes(b"HOST_LOG")
    screenshot.write_bytes(b"SCREENSHOT")
    rec = InstallRecord(
        game_exe=str(game_dir / "game.exe"),
        game_dir=str(game_dir),
        reshade_dir=str(game_dir),
        reshade_by_us=True,
    )
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)

    assert run_uninstall(game_dir)
    assert not host_log.exists()
    assert not screenshot.exists()
    assert not host_dir.exists()


def test_failed_uninstall_restores_feeder_host_runtime_artifacts(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    host_dir = game_dir / "host64"
    host_dir.mkdir(parents=True)
    host_log = host_dir / "dlss5-feed-host.log"
    screenshot = host_dir / "dlss5-feed-host64 2026-09-03 02-06-49_1.png"
    host_log.write_bytes(b"HOST_LOG")
    screenshot.write_bytes(b"SCREENSHOT")
    rec = InstallRecord(
        game_exe=str(game_dir / "game.exe"),
        game_dir=str(game_dir),
        reshade_dir=str(game_dir),
        reshade_by_us=True,
    )
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)
    mocker.patch("dlss5_enabler.operations.uninstall.index_add", return_value=True)

    assert not run_uninstall(game_dir)
    assert host_log.read_bytes() == b"HOST_LOG"
    assert screenshot.read_bytes() == b"SCREENSHOT"
    assert rec.record_path().is_file()


def test_run_uninstall_failure_restores_installed_state(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    placed = game_dir / "dxgi.dll"
    placed.write_bytes(b"MOD")
    rec = InstallRecord(
        game_exe=str(game_dir / "game.exe"),
        game_dir=str(game_dir),
        files=[RecordedFile(path=str(placed), sha256="unused")],
    )
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")

    def partial_failure(_rec: InstallRecord, _log: object) -> bool:
        placed.unlink()
        return False

    mocker.patch("dlss5_enabler.operations.uninstall.revert_record_mutations", side_effect=partial_failure)
    mocker.patch("dlss5_enabler.operations.uninstall.index_add", return_value=True)

    assert not run_uninstall(game_dir)
    assert placed.read_bytes() == b"MOD"
    assert rec.record_path().is_file()


def test_uninstall_restores_config_backup_byte_for_byte(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    config = game_dir / "dgVoodoo.conf"
    original = b"[DirectX]\nCustomSetting=KeepMe\n"
    config.write_bytes(b"[DirectX]\nVRAM=1024\n")
    backup = game_dir / "dgVoodoo.conf.dlss5-enabler.bak"
    backup.write_bytes(original)
    rec = InstallRecord(
        game_exe=str(game_dir / "game.exe"),
        game_dir=str(game_dir),
        files=[RecordedFile(path=str(config), backup=str(backup))],
        ini_touched=[IniTouch(path=str(config), section="DirectX", key="VRAM", original="")],
    )
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)

    assert run_uninstall(game_dir)
    assert config.read_bytes() == original


def test_run_uninstall_no_record_is_idempotent(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_game"
    empty_dir.mkdir()
    messages: list[str] = []

    assert run_uninstall(empty_dir, log=messages.append)
    assert any("already uninstalled" in message for message in messages)


def test_run_uninstall_no_record_fails_when_index_cleanup_fails(tmp_path: Path, mocker: MockerFixture) -> None:
    empty_dir = tmp_path / "empty_game"
    empty_dir.mkdir()
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=False)

    assert not run_uninstall(empty_dir)


def test_run_uninstall_rejects_running_game(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(b"MZ")
    installed = game_dir / "dxgi.dll"
    installed.write_bytes(b"MOD")
    rec = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir))
    rec.files.append(RecordedFile(path=str(installed)))
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")
    adapter = mocker.Mock()
    adapter.is_game_running.return_value = True
    mocker.patch("dlss5_enabler.operations.uninstall.get_platform_adapter", return_value=adapter)
    messages: list[str] = []

    assert not run_uninstall(game_dir, log=messages.append)
    assert installed.read_bytes() == b"MOD"
    assert rec.record_path().is_file()
    assert any("Cannot uninstall while game.exe is running" in message for message in messages)
    adapter.is_game_running.assert_called_once_with(game_exe)


def test_step_configure_wine_overrides(tmp_path: Path, mocker: MockerFixture) -> None:
    game_exe = tmp_path / "game" / "game.exe"
    pfx_dir = tmp_path / "pfx"
    pfx_dir.mkdir(parents=True, exist_ok=True)
    user_reg = pfx_dir / "user.reg"
    user_reg.write_text("WINE REGISTRY Version 2\n", encoding="utf-8")

    prefix_info = SteamPrefixInfo(appid="123", prefix_path=pfx_dir, game_dir=tmp_path / "game")
    mocker.patch("dlss5_enabler.operations.steps.ProtonManager.find_prefix_for_game", return_value=prefix_info)

    ctx = PipelineContext(game_exe=game_exe)
    ctx.reshade_dll_name = "dxgi.dll"
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path / "game"))

    step = StepConfigureWineOverrides()
    assert step.execute(ctx)
    assert len(ctx.record.registry_touched) == 1
    assert ctx.record.registry_touched[0].value_name == "dxgi"
    assert ctx.record.proton_prefix == str(pfx_dir)

    overrides = WineRegParser.read_overrides(user_reg)
    assert overrides.get("dxgi") == "native,builtin"


def test_step_configure_wine_overrides_records_existing_value(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    pfx_dir = tmp_path / "pfx"
    pfx_dir.mkdir()
    user_reg = pfx_dir / "user.reg"
    user_reg.write_text(
        'WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\DllOverrides] 1 0\n"dxgi"="builtin"\n',
        encoding="utf-8",
    )
    prefix = SteamPrefixInfo(appid="123", prefix_path=pfx_dir, game_dir=game_dir)
    mocker.patch("dlss5_enabler.operations.steps.ProtonManager.find_prefix_for_game", return_value=prefix)
    ctx = PipelineContext(game_exe=game_exe)
    ctx.reshade_dll_name = "dxgi.dll"
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir))

    assert StepConfigureWineOverrides().execute(ctx)
    touch = ctx.record.registry_touched[0]
    assert touch.original_exists
    assert touch.original_value == "builtin"

    ctx.record.record_path().write_text(ctx.record.model_dump_json(), encoding="utf-8")
    assert run_uninstall(game_dir)
    assert WineRegParser.read_overrides(user_reg)["dxgi"] == "builtin"


def test_step_save_record_propagates_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    ctx = PipelineContext(game_exe=tmp_path / "game.exe")
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path))
    mocker.patch("dlss5_enabler.operations.steps.record_save", return_value=False)

    assert not StepSaveRecord().execute(ctx)
    assert "per-game install record" in ctx.error_message


def test_vulkan_request_fails_when_archive_is_unavailable(tmp_path: Path) -> None:
    ctx = PipelineContext(game_exe=tmp_path / "game.exe", install_vulkan_layer=True)
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path), vulkan_layer=True)

    assert not StepInstallVulkanLayer().execute(ctx)
    assert not ctx.record.vulkan_layer


def test_vulkan_install_backs_up_existing_file(tmp_path: Path) -> None:
    archive = tmp_path / "vulkan.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("layer.dll", b"NEW_LAYER")
    destination = tmp_path / "layer.dll"
    destination.write_bytes(b"ORIGINAL_LAYER")
    bundle = FeederBundle()
    bundle.vk_layer_zip = archive
    ctx = PipelineContext(game_exe=tmp_path / "game.exe", install_vulkan_layer=True)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.feeder_bundle = bundle
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path), vulkan_layer=True)

    assert StepInstallVulkanLayer().execute(ctx)
    item = next(recorded for recorded in ctx.record.files if Path(recorded.path) == destination)
    assert destination.read_bytes() == b"NEW_LAYER"
    assert Path(item.backup).read_bytes() == b"ORIGINAL_LAYER"


@pytest.mark.parametrize(
    ("is_32bit", "installed", "excluded"),
    (
        (False, "VkLayer_feed_vk.dll", "VkLayer_feed_vk32.dll"),
        (True, "VkLayer_feed_vk32.dll", "VkLayer_feed_vk.dll"),
    ),
)
def test_vulkan_install_selects_game_architecture(
    is_32bit: bool, installed: str, excluded: str, tmp_path: Path, mocker: MockerFixture
) -> None:
    archive = tmp_path / "feeder.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("layer-x64/VkLayer_feed_vk.dll", b"X64")
        zf.writestr("layer-x86/VkLayer_feed_vk32.dll", b"X86")
    bundle = FeederBundle()
    bundle.vk_layer_zip = archive
    ctx = PipelineContext(game_exe=tmp_path / "game.exe", install_vulkan_layer=True)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.is_32bit = is_32bit
    ctx.feeder_bundle = bundle
    ctx.record = InstallRecord(game_exe=str(ctx.game_exe), game_dir=str(tmp_path), vulkan_layer=True)
    mocker.patch("dlss5_enabler.operations.steps.get_cache_dir", return_value=tmp_path)

    assert StepInstallVulkanLayer().execute(ctx)
    assert (tmp_path / installed).is_file()
    assert not (tmp_path / excluded).exists()


def test_reshade_success_requires_installed_hook(tmp_path: Path, mocker: MockerFixture) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"SETUP")
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.need_reshade = True
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path))
    ctx.reshade_bundle = ReshadeBundle()
    ctx.reshade_bundle.setup_exe_path = setup
    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", return_value=True)

    assert not StepInstallReShade().execute(ctx)
    assert "without creating" in ctx.error_message


def test_reshade_existing_ini_is_restored_on_uninstall(tmp_path: Path, mocker: MockerFixture) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"SETUP")
    extracted_dll = tmp_path / "ReShade64.dll"
    extracted_dll.write_bytes(b"RESHADER")
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    game_ini = tmp_path / "ReShade.ini"
    original_ini = b"[GENERAL]\nUserSetting=Preserve\n"
    game_ini.write_bytes(original_ini)
    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.need_reshade = True
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path))
    ctx.reshade_bundle = ReshadeBundle()
    ctx.reshade_bundle.setup_exe_path = setup
    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", return_value=False)
    mocker.patch(
        "dlss5_enabler.operations.steps.extract_reshade_dlls_from_installer",
        return_value={"reshade64.dll": extracted_dll},
    )

    assert StepInstallReShade().execute(ctx)
    assert any(item.path == game_ini.as_posix() and item.backup for item in ctx.record.files)
    ctx.record.record_path().write_text(ctx.record.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)
    assert run_uninstall(tmp_path)
    assert game_ini.read_bytes() == original_ini


def test_reshade_records_and_removes_runtime_artifacts(tmp_path: Path, mocker: MockerFixture) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"SETUP")
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    ctx = PipelineContext(game_exe=game_exe)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.need_reshade = True
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path))
    ctx.reshade_bundle = ReshadeBundle()
    ctx.reshade_bundle.setup_exe_path = setup

    def install_reshade(_setup: Path, _target: Path, _api: str) -> bool:
        (tmp_path / "dxgi.dll").write_bytes(b"RESHADE")
        (tmp_path / "ReShade.ini").write_text("[GENERAL]\n", encoding="utf-8")
        (tmp_path / "ReShade.log").write_text("ReShade", encoding="utf-8")
        (tmp_path / "ReShadePreset.ini").write_text("Preset", encoding="utf-8")
        return True

    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", side_effect=install_reshade)

    assert StepInstallReShade().execute(ctx)
    artifact_names = {
        "ReShade.log",
        "ReShade.ini.bak",
        "ReShadePreset.ini",
        "dxgi.log",
        "dlss5-feed.cfg",
        "dlss5-feed.log",
    }
    assert artifact_names.issubset({Path(item.path).name for item in ctx.record.files})
    assert not any(item.backup for item in ctx.record.files if Path(item.path).name in artifact_names)
    for name in artifact_names:
        (tmp_path / name).write_text(name, encoding="utf-8")

    ctx.record.record_path().write_text(ctx.record.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)

    assert run_uninstall(tmp_path)
    assert not any((tmp_path / name).exists() for name in artifact_names)


def test_reshade_redirected_d3d9_install_relocates_then_mirrors(tmp_path: Path, mocker: MockerFixture) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"SETUP")
    game_exe = tmp_path / "game.exe"
    game_exe.write_bytes(b"MZ")
    redirected_dir = tmp_path / "bin"
    ctx = PipelineContext(game_exe=game_exe, d3d9_translate=True)
    ctx.game_dir = tmp_path
    ctx.reshade_dir = tmp_path
    ctx.need_reshade = True
    ctx.record = InstallRecord(game_exe=str(game_exe), game_dir=str(tmp_path))
    ctx.reshade_bundle = ReshadeBundle()
    ctx.reshade_bundle.setup_exe_path = setup

    def install_reshade(_setup: Path, _target: Path, _api: str) -> bool:
        redirected_dir.mkdir()
        (tmp_path / "ReShade.ini").write_text("[INSTALL]\nBasePath=bin\n", encoding="utf-8")
        (redirected_dir / "dxgi.dll").write_bytes(b"RESHADE")
        (redirected_dir / "ReShade.ini").write_text(
            "[GENERAL]\nEffectSearchPaths=.\\reshade-shaders\\Shaders\\**\\**\n",
            encoding="utf-8",
        )
        return True

    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", side_effect=install_reshade)

    assert StepInstallReShade().execute(ctx)
    assert ctx.reshade_dir == tmp_path
    assert (tmp_path / "dxgi.dll").read_bytes() == b"RESHADE"
    assert not (redirected_dir / "dxgi.dll").exists()
    assert StepMirrorDualLocations().execute(ctx)
    assert (redirected_dir / "dxgi.dll").read_bytes() == b"RESHADE"
    assert "EffectSearchPaths=.\\reshade-shaders\\Shaders\\**" in (redirected_dir / "ReShade.ini").read_text(
        encoding="utf-8"
    )
    assert not (redirected_dir / "bin").exists()


def test_mirror_dual_locations_backs_up_existing_file(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    alt_bin = game_dir / "bin"
    alt_bin.mkdir(parents=True)
    source = game_dir / "addon.dll"
    source.write_bytes(b"NEW")
    destination = alt_bin / "addon.dll"
    destination.write_bytes(b"ORIGINAL")
    ctx = PipelineContext(game_exe=game_dir / "game.exe", d3d9_translate=True)
    ctx.game_dir = game_dir
    ctx.reshade_dir = game_dir
    ctx.record = InstallRecord(
        game_exe=str(ctx.game_exe),
        game_dir=str(game_dir),
        files=[RecordedFile(path=str(source), sha256="source")],
    )

    assert StepMirrorDualLocations().execute(ctx)
    mirrored = next(item for item in ctx.record.files if Path(item.path) == destination)
    assert Path(mirrored.backup).read_bytes() == b"ORIGINAL"
    assert destination.read_bytes() == b"NEW"


def test_extract_reshade_dlls_from_installer_py7zr(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "ReShade64.dll").write_bytes(b"RESHADE64_BIN")
    (src_dir / "ReShade32.dll").write_bytes(b"RESHADE32_BIN")

    archive_path = tmp_path / "ReShade_Setup.exe"
    with py7zr.SevenZipFile(archive_path, mode="w") as archive:
        archive.write(src_dir / "ReShade64.dll", "ReShade64.dll")
        archive.write(src_dir / "ReShade32.dll", "ReShade32.dll")

    dest_dir = tmp_path / "dest"
    extracted = extract_reshade_dlls_from_installer(archive_path, dest_dir)
    assert "reshade64.dll" in extracted
    assert "reshade32.dll" in extracted
    assert any(dest_dir.rglob("ReShade64.dll"))


def test_run_uninstall_with_wine_overrides(tmp_path: Path, mocker: MockerFixture) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    pfx_dir = tmp_path / "pfx"
    pfx_dir.mkdir(parents=True, exist_ok=True)
    user_reg = pfx_dir / "user.reg"
    user_reg.write_text(
        "WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\DllOverrides] 1700000000 0\n"
        '"dxgi"="native,builtin"\n"d3d9"="native,builtin"\n',
        encoding="utf-8",
    )

    rec = InstallRecord(
        game_exe=str(game_dir / "game.exe"),
        game_dir=str(game_dir),
        proton_prefix=str(pfx_dir),
        registry_touched=[
            RegistryTouch(
                reg_path=str(user_reg),
                key=r"Software\Wine\DllOverrides",
                value_name="dxgi",
            )
        ],
    )
    rec.record_path().write_text(rec.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)

    assert run_uninstall(game_dir)
    overrides = WineRegParser.read_overrides(user_reg)
    assert "dxgi" not in overrides
    assert "d3d9" in overrides


@pytest.mark.parametrize(
    ("machine", "architecture"),
    [
        (IMAGE_FILE_MACHINE_I386, "x86"),
        (IMAGE_FILE_MACHINE_AMD64, "x64"),
    ],
)
def test_synthetic_install_and_uninstall_round_trip(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    machine: int,
    architecture: str,
) -> None:
    game_dir = tmp_path / architecture
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(_synthetic_pe(machine))
    original_executable = game_exe.read_bytes()
    bundles = _synthetic_bundles(tmp_path / f"artifacts-{architecture}")
    fetches = {
        name: mocker.patch(f"dlss5_enabler.operations.steps.{name}", return_value=bundle)
        for name, bundle in bundles.items()
    }

    def install_reshade(_setup_exe: Path, target_exe: Path, _api: str) -> bool:
        target_exe.parent.mkdir(parents=True, exist_ok=True)
        (target_exe.parent / "dxgi.dll").write_bytes(b"synthetic-reshade")
        (target_exe.parent / "ReShade.ini").write_text("[GENERAL]\n", encoding="utf-8")
        return True

    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", side_effect=install_reshade)
    mocker.patch("dlss5_enabler.operations.steps.ProtonManager.find_prefix_for_game", return_value=None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert run_install(game_exe, install_lumenite=False)
    assert (game_dir / "dxgi.dll").read_bytes() == b"synthetic-reshade"
    assert (game_dir / f"dlss5-feed.addon{'32' if architecture == 'x86' else '64'}").is_file()
    assert (game_dir / "reshade-shaders" / "Shaders" / "DLSS5_Feed.fx").is_file()
    assert (game_dir / "dlss5-enabler.install.json").is_file()
    if architecture == "x86":
        host_dir = game_dir / "host64"
        assert (host_dir / "dlss5-feed-host64.exe").is_file()
        assert (host_dir / "dxgi.dll").read_bytes() == b"synthetic-reshade"
        assert (host_dir / "renodx-dlss5.addon64").is_file()
        assert (host_dir / "nvngx_dlssnr.dll").is_file()
        assert (host_dir / "nvngx_dlss.dll").is_file()
    else:
        assert (game_dir / "renodx-dlss5.addon64").is_file()
        assert (game_dir / "nvngx_dlssnr.dll").is_file()
        assert (game_dir / "nvngx_dlss.dll").is_file()

    assert run_install(game_exe, install_lumenite=False)
    assert all(fetch.call_count == 2 for fetch in fetches.values())
    assert not tuple(game_dir.rglob("*.dlss5-enabler.bak*"))
    assert run_uninstall(game_exe)
    assert game_exe.read_bytes() == original_executable
    assert not (game_dir / "dxgi.dll").exists()
    assert not (game_dir / "dlss5-enabler.install.json").exists()
    assert not (game_dir / "host64").exists()


@pytest.mark.parametrize(
    ("machine", "architecture"),
    [
        (IMAGE_FILE_MACHINE_I386, "x86"),
        (IMAGE_FILE_MACHINE_AMD64, "x64"),
    ],
)
def test_synthetic_game_update_round_trip(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    machine: int,
    architecture: str,
) -> None:
    game_dir = tmp_path / architecture
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(_synthetic_pe(machine))
    bundles = _synthetic_bundles(tmp_path / f"update-artifacts-{architecture}")
    fetches = {
        name: mocker.patch(f"dlss5_enabler.operations.steps.{name}", return_value=bundle)
        for name, bundle in bundles.items()
    }

    def install_reshade(_setup_exe: Path, target_exe: Path, _api: str) -> bool:
        (target_exe.parent / "dxgi.dll").write_bytes(b"synthetic-reshade")
        (target_exe.parent / "ReShade.ini").write_text("[GENERAL]\n", encoding="utf-8")
        return True

    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", side_effect=install_reshade)
    mocker.patch("dlss5_enabler.operations.steps.ProtonManager.find_prefix_for_game", return_value=None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert run_install(game_exe, install_lumenite=False)
    previous = record_load(game_dir)
    assert previous is not None
    previous.tool_version = "1.0.0"
    previous.install_options = InstallOptions(lumenite=False)
    assert record_save(previous)
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    mocker.patch("dlss5_enabler.operations.steps.get_tool_version", return_value="1.1.0")

    result = run_update(game_dir)

    assert result.status is GameUpdateStatus.UPDATED
    updated = record_load(game_dir)
    assert updated is not None
    assert updated.tool_version == "1.1.0"
    assert updated.schema_version == 2
    assert updated.install_options == InstallOptions(lumenite=False)
    assert all(fetch.call_count == 2 for fetch in fetches.values())
    assert not tuple(game_dir.rglob("*.dlss5-enabler.bak*"))

    second = run_update(game_dir)

    assert second.status is GameUpdateStatus.ALREADY_CURRENT
    assert all(fetch.call_count == 2 for fetch in fetches.values())


def test_update_fetch_failure_preserves_existing_installation(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    game_exe = game_dir / "game.exe"
    game_exe.write_bytes(_synthetic_pe(IMAGE_FILE_MACHINE_AMD64))
    bundles = _synthetic_bundles(tmp_path / "failure-artifacts")
    for name, bundle in bundles.items():
        mocker.patch(f"dlss5_enabler.operations.steps.{name}", return_value=bundle)

    def install_reshade(_setup_exe: Path, target_exe: Path, _api: str) -> bool:
        (target_exe.parent / "dxgi.dll").write_bytes(b"synthetic-reshade")
        (target_exe.parent / "ReShade.ini").write_text("[GENERAL]\n", encoding="utf-8")
        return True

    mocker.patch("dlss5_enabler.operations.steps.reshade_headless_install", side_effect=install_reshade)
    mocker.patch("dlss5_enabler.operations.steps.ProtonManager.find_prefix_for_game", return_value=None)
    mocker.patch("dlss5_enabler.operations.update.get_tool_version", return_value="1.1.0")
    mocker.patch("dlss5_enabler.operations.steps.get_tool_version", return_value="1.1.0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert run_install(game_exe, install_lumenite=False)
    installed = record_load(game_dir)
    assert installed is not None
    installed.tool_version = "1.0.0"
    assert record_save(installed)
    before = {
        path.relative_to(game_dir).as_posix(): path.read_bytes() for path in game_dir.rglob("*") if path.is_file()
    }
    mocker.patch("dlss5_enabler.operations.steps.fetch_feeder", side_effect=RuntimeError("offline"))

    result = run_update(game_exe)

    after = {path.relative_to(game_dir).as_posix(): path.read_bytes() for path in game_dir.rglob("*") if path.is_file()}
    assert result.status is GameUpdateStatus.FAILED
    assert after == before
