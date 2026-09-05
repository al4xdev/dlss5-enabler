from pathlib import Path

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.core.pe import PeArch, detect_game_apis, detect_native_dlss, detect_pe_arch
from dlss5_enabler.core.record import (
    CURRENT_RECORD_SCHEMA_VERSION,
    IndexEntrySnapshot,
    InstallOptions,
    InstallRecord,
    OptiScalerStrategyOptions,
    RenoDxStrategyOptions,
    capture_index_entry,
    index_add,
    record_exists,
    record_load,
    record_save,
    restore_index_entry,
)
from dlss5_enabler.core.util import file_is_writable, get_permission_guidance, is_directory_writable
from dlss5_enabler.core.version import get_tool_version
from dlss5_enabler.operations.capabilities import analyze_capabilities
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineStep, TargetAnalysis
from dlss5_enabler.operations.uninstall import (
    capture_install_snapshot,
    cleanup_install_snapshot,
    restore_install_snapshot,
    run_uninstall,
)
from dlss5_enabler.platform import get_platform_adapter
from dlss5_enabler.schemas.strategy import InstallStrategy

logger = get_logger("steps_common")


def _target_access_error(game_exe: Path) -> str:
    if get_platform_adapter().is_game_running(game_exe):
        return f"Game is currently running: {game_exe.name}. Close it before installing."
    if not file_is_writable(game_exe):
        return f"Game executable is locked: {game_exe.name}"
    if not is_directory_writable(game_exe.parent):
        return f"Game directory is write-protected. {get_permission_guidance(game_exe.parent)}"
    return ""


class StepValidateTarget(PipelineStep[PipelineContext]):
    @property
    def name(self) -> str:
        return "ValidateTarget"

    @property
    def description(self) -> str:
        return "Analyzes executable capabilities and validates access before selecting components"

    def execute(self, ctx: PipelineContext) -> bool:
        ctx.game_exe = ctx.game_exe.resolve()
        if not ctx.game_exe.is_file():
            ctx.error_message = f"Game executable not found: {ctx.game_exe}"
            return False
        ctx.game_dir = ctx.game_exe.parent
        architecture = detect_pe_arch(ctx.game_exe)
        if architecture not in {PeArch.X86, PeArch.X64}:
            ctx.error_message = f"Unsupported architecture ({architecture.value}). Supported: x86 and x64 Windows PE."
            return False
        previous = record_load(ctx.game_dir)
        if record_exists(ctx.game_dir) and previous is None:
            ctx.error_message = "Existing installation record is unreadable or unsupported; it was preserved."
            return False
        if previous is not None and Path(previous.game_dir).resolve() != ctx.game_dir:
            ctx.error_message = "Existing installation record belongs to another directory; it was preserved."
            return False
        access_error = _target_access_error(ctx.game_exe)
        if access_error:
            ctx.error_message = access_error
            return False
        managed_sr = (
            [item for item in previous.files if Path(item.path).name.lower() == "nvngx_dlss.dll"] if previous else []
        )
        native_dlss = any(item.backup for item in managed_sr) or (not managed_sr and detect_native_dlss(ctx.game_exe))
        apis = tuple(detect_game_apis(ctx.game_exe))
        if not apis:
            apis = analyze_capabilities(ctx.game_exe, previous).apis
        ctx.analysis = TargetAnalysis(architecture, apis, native_dlss, previous)
        install_options = InstallOptions()
        strategy_options = (
            OptiScalerStrategyOptions(proxy_name="dxgi.dll", source_revision="pending")
            if ctx.strategy is InstallStrategy.OPTISCALER
            else RenoDxStrategyOptions.from_install_options(install_options)
        )
        ctx.record = InstallRecord(
            schema_version=CURRENT_RECORD_SCHEMA_VERSION,
            tool_version=get_tool_version(),
            game_exe=ctx.game_exe.as_posix(),
            game_dir=ctx.game_dir.as_posix(),
            strategy=ctx.strategy,
            architecture="x86" if architecture is PeArch.X86 else "x64",
            is_32bit=architecture is PeArch.X86,
            platform=get_platform_adapter().platform_name,
            install_options=install_options,
            strategy_options=strategy_options,
        )
        logger.info(f"Target: {ctx.game_exe.name}; {architecture.value}; APIs: {', '.join(api.value for api in apis)}")
        return True


class StepCleanPreviousInstall(PipelineStep[PipelineContext]):
    @property
    def name(self) -> str:
        return "CleanPreviousInstall"

    @property
    def description(self) -> str:
        return "Captures the previous installation and removes its managed mutations"

    def execute(self, ctx: PipelineContext) -> bool:
        if record_exists(ctx.game_dir):
            previous = record_load(ctx.game_dir)
            if previous is None:
                ctx.error_message = "Existing installation record is unreadable; refusing to overwrite it."
                return False
            ctx.previous_install_snapshot = capture_install_snapshot(previous)
            if not run_uninstall(ctx.game_dir, log=logger.info, lock_operation=False):
                ctx.error_message = "Failed to cleanly remove previous installation prior to refresh."
                return False
        return True

    def rollback(self, ctx: PipelineContext) -> None:
        if ctx.previous_install_snapshot is not None:
            if not restore_install_snapshot(ctx.previous_install_snapshot, cleanup=not ctx.recovery_errors):
                raise RuntimeError(f"Could not restore previous installation: {ctx.previous_install_snapshot.root}")
            if not ctx.recovery_errors:
                ctx.previous_install_snapshot = None

    def cleanup(self, ctx: PipelineContext) -> None:
        if ctx.previous_install_snapshot is not None:
            cleanup_install_snapshot(ctx.previous_install_snapshot)
            ctx.previous_install_snapshot = None


class StepSaveRecord(PipelineStep[PipelineContext]):
    def __init__(self) -> None:
        self.record_written = False
        self.index_snapshot: IndexEntrySnapshot | None = None

    @property
    def name(self) -> str:
        return "SaveRecord"

    @property
    def description(self) -> str:
        return "Persists the install record and updates the global install index"

    def execute(self, ctx: PipelineContext) -> bool:
        self.record_written = False
        self.index_snapshot = None
        self.index_snapshot = capture_index_entry(ctx.game_dir)
        if not record_save(ctx.record):
            ctx.error_message = "Could not save the per-game install record."
            return False
        self.record_written = True
        if not index_add(ctx.record):
            ctx.error_message = "Could not update the global install index."
            return False
        return True

    def rollback(self, ctx: PipelineContext) -> None:
        if self.record_written:
            if self.index_snapshot is None or not restore_index_entry(ctx.game_dir, self.index_snapshot):
                raise RuntimeError("Could not restore the global install index.")
            with resource_lock(ctx.record.record_path()):
                ctx.record.record_path().unlink(missing_ok=True)
