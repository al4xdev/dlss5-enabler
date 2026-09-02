import zipfile
from pathlib import Path

import py7zr
from pytest_mock import MockerFixture

from dlss5_enabler.core.pe import PeArch
from dlss5_enabler.core.record import IniTouch, InstallRecord, RecordedFile, RegistryTouch
from dlss5_enabler.network.sources import FeederBundle, ReshadeBundle
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineRunner, PipelineStep
from dlss5_enabler.operations.reshade import (
    ensure_mv_provider_def,
    extract_reshade_dlls_from_installer,
    normalize_search_paths,
)
from dlss5_enabler.operations.steps import (
    StepCleanPreviousInstall,
    StepConfigureWineOverrides,
    StepInstallReShade,
    StepInstallVulkanLayer,
    StepMirrorDualLocations,
    StepSaveRecord,
    StepValidateTarget,
    _place_file,
)
from dlss5_enabler.operations.uninstall import run_uninstall
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


def test_pipeline_runner_success(tmp_path: Path) -> None:
    exe = tmp_path / "game.exe"
    ctx = PipelineContext(game_exe=exe)

    step1 = DummySuccessStep()
    step2 = DummySuccessStep()
    runner = PipelineRunner([step1, step2])

    assert runner.run(ctx)
    assert not ctx.failed_step
    assert not ctx.error_message


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
    assert ctx.record.files[0].backup == str(backup_file)


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
    assert "./reshade-shaders/Shaders/**" in content
    assert "**\\**" not in content
    assert "**/**" not in content
    assert len(rec.ini_touched) == 2


def test_normalize_search_paths_reports_write_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    ini_file = tmp_path / "ReShade.ini"
    ini_file.write_text("[GENERAL]\nEffectSearchPaths=.\\\n", encoding="utf-8")
    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")
    mocker.patch("dlss5_enabler.operations.reshade.ini_set_exact", return_value=False)

    assert not normalize_search_paths(ini_file, rec)
    assert rec.ini_touched == []


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


def test_run_uninstall_no_record(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_game"
    empty_dir.mkdir()
    assert not run_uninstall(empty_dir)


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
    assert any(item.path == str(game_ini) and item.backup for item in ctx.record.files)
    ctx.record.record_path().write_text(ctx.record.model_dump_json(), encoding="utf-8")
    mocker.patch("dlss5_enabler.operations.uninstall.index_remove", return_value=True)
    assert run_uninstall(tmp_path)
    assert game_ini.read_bytes() == original_ini


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
