from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.core.record import (
    InstallOptions,
    InstallRecord,
    OptiScalerStrategyOptions,
    RenoDxStrategyOptions,
    record_load,
)
from dlss5_enabler.core.version import InstallVersionStatus, get_install_version_status, get_tool_version
from dlss5_enabler.operations.install import _run_install_unlocked
from dlss5_enabler.operations.pipeline import PipelineResult, PipelineStatus
from dlss5_enabler.schemas.strategy import InstallStrategy

LogFn = Callable[[str], None]


class GameUpdateStatus(str, Enum):
    UPDATED = "updated"
    REINSTALLED = "reinstalled"
    ALREADY_CURRENT = "already_current"
    DOWNGRADE_REFUSED = "downgrade_refused"
    RECORD_MISSING = "record_missing"
    RECORD_INVALID = "record_invalid"
    GAME_MISSING = "game_missing"
    FAILED = "failed"
    RECOVERY_FAILED = "recovery_failed"


@dataclass(frozen=True)
class GameUpdateResult:
    status: GameUpdateStatus
    message: str
    previous_version: str = ""
    current_version: str = ""
    options: InstallOptions | None = None
    installation: PipelineResult | None = None

    @property
    def success(self) -> bool:
        return self.status in {
            GameUpdateStatus.UPDATED,
            GameUpdateStatus.REINSTALLED,
            GameUpdateStatus.ALREADY_CURRENT,
        }


def _find_record_directory(target: Path) -> Path:
    return target if target.is_dir() else target.parent


def _eligibility_result(
    record: InstallRecord,
    current_version: str,
    reinstall: bool,
) -> GameUpdateResult | None:
    status = get_install_version_status(record.tool_version, current_version)
    if status is InstallVersionStatus.CURRENT and not reinstall:
        return GameUpdateResult(
            GameUpdateStatus.ALREADY_CURRENT,
            f"This game is already current at DLSS5 Enabler {current_version}.",
            record.tool_version,
            current_version,
            record.install_options,
        )
    if status is InstallVersionStatus.NEWER_THAN_CLI:
        return GameUpdateResult(
            GameUpdateStatus.DOWNGRADE_REFUSED,
            f"This game was installed by {record.tool_version}, newer than this CLI ({current_version}). "
            "Update the CLI before updating the game.",
            record.tool_version,
            current_version,
            record.install_options,
        )
    return None


def _selection_result(
    record: InstallRecord,
    current_version: str,
    selected: InstallStrategy,
    reinstall: bool,
    explicitly_selected: bool,
    optiscaler_options_requested: bool,
) -> GameUpdateResult | None:
    if optiscaler_options_requested and selected is not InstallStrategy.OPTISCALER:
        return GameUpdateResult(
            GameUpdateStatus.FAILED,
            "OptiScaler options require --engine optiscaler or a saved OptiScaler strategy.",
            record.tool_version,
            current_version,
            record.install_options,
        )
    return _eligibility_result(
        record, current_version, reinstall or explicitly_selected or optiscaler_options_requested
    )


def run_update(
    game_dir_or_exe: Path | str,
    *,
    reinstall: bool = False,
    force_download: bool = False,
    verbose: bool = False,
    log: LogFn = print,
    strategy: InstallStrategy | None = None,
    optiscaler_archive: Path | None = None,
    optiscaler_nr_passes: int | None = None,
    optiscaler_proxy: str | None = None,
) -> GameUpdateResult:
    target = Path(game_dir_or_exe).resolve()
    game_dir = _find_record_directory(target)
    record_path = game_dir / "dlss5-enabler.install.json"
    with resource_lock(game_dir / ".dlss5-enabler-install-operation"):
        if not record_path.is_file():
            return GameUpdateResult(
                GameUpdateStatus.RECORD_MISSING,
                f"No install record found in {game_dir}. Run 'dlss5-enabler install' first.",
            )
        record = record_load(game_dir)
        if record is None:
            return GameUpdateResult(
                GameUpdateStatus.RECORD_INVALID,
                f"Install record is invalid and was preserved: {record_path}",
            )
        current_version = get_tool_version()
        selected = InstallStrategy(strategy) if strategy is not None else record.strategy
        explicitly_selected = strategy is not None
        optiscaler_options_requested = any(
            value is not None for value in (optiscaler_archive, optiscaler_nr_passes, optiscaler_proxy)
        )
        selection = _selection_result(
            record,
            current_version,
            selected,
            reinstall,
            explicitly_selected,
            optiscaler_options_requested,
        )
        if selection is not None:
            return selection
        options = record.install_options
        game_exe = target if target.is_file() else Path(record.game_exe)
        if not game_exe.is_absolute():
            game_exe = game_dir / game_exe
        if not game_exe.is_file():
            return GameUpdateResult(
                GameUpdateStatus.GAME_MISSING,
                f"Recorded game executable was not found: {game_exe}",
                record.tool_version,
                current_version,
                options,
            )
        source_revision = ""
        nr_passes = optiscaler_nr_passes if optiscaler_nr_passes is not None else 1
        proxy = optiscaler_proxy or "dxgi.dll"
        if selected is InstallStrategy.OPTISCALER and isinstance(record.strategy_options, OptiScalerStrategyOptions):
            source_revision = record.strategy_options.source_revision
            nr_passes = optiscaler_nr_passes if optiscaler_nr_passes is not None else record.strategy_options.nr_passes
            proxy = optiscaler_proxy or record.strategy_options.proxy_name
        if selected is InstallStrategy.RENODX and isinstance(record.strategy_options, RenoDxStrategyOptions):
            options = record.strategy_options.as_install_options()
        log(
            f"Updating game from DLSS5 Enabler {record.tool_version} to {current_version}; "
            f"engine: {record.strategy.value} -> {selected.value}; "
            f"options: Lumenite={'yes' if options.lumenite else 'no'}, "
            f"D3D9={'yes' if options.d3d9 else 'no'}, OpenGL={'yes' if options.opengl else 'no'}, "
            f"Vulkan={'yes' if options.vulkan_layer else 'no'}"
        )
        if selected is InstallStrategy.OPTISCALER:
            installation = _run_install_unlocked(
                game_exe,
                force_download=force_download,
                verbose=verbose,
                strategy=selected,
                optiscaler_archive=optiscaler_archive,
                optiscaler_source_revision=source_revision,
                optiscaler_nr_passes=nr_passes,
                optiscaler_proxy=proxy,
            )
        else:
            installation = _run_install_unlocked(
                game_exe,
                install_lumenite=options.lumenite,
                d3d9_translate=options.d3d9,
                opengl=options.opengl,
                install_vulkan_layer=options.vulkan_layer,
                force_download=force_download,
                verbose=verbose,
                strategy=selected,
            )
        if not installation.success:
            recovery_failed = installation.status is PipelineStatus.RECOVERY_FAILED
            message = (
                "Game update failed and recovery is incomplete. " + "; ".join(installation.recovery_errors)
                if recovery_failed
                else "Game update failed; the state from before this operation was restored."
            )
            if installation.message:
                message += f" Cause: {installation.message}"
            if installation.recovery_path:
                message += f" Recovery snapshot: {installation.recovery_path}"
            return GameUpdateResult(
                GameUpdateStatus.RECOVERY_FAILED if recovery_failed else GameUpdateStatus.FAILED,
                message,
                record.tool_version,
                current_version,
                options,
                installation,
            )
        result_status = (
            GameUpdateStatus.REINSTALLED
            if reinstall or explicitly_selected or optiscaler_options_requested
            else GameUpdateStatus.UPDATED
        )
        message = f"Game installation now uses DLSS5 Enabler {current_version}; engine: {selected.value}."
        if installation.cleanup_errors:
            message += " Installation is active; cleanup pending: " + "; ".join(installation.cleanup_errors)
        return GameUpdateResult(
            result_status,
            message,
            record.tool_version,
            current_version,
            options,
            installation,
        )
