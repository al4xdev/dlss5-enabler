from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar

from rich.console import Console

from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.core.pe import DetectedApi, PeArch
from dlss5_enabler.core.record import InstallRecord
from dlss5_enabler.network.resolver import ResolutionWarning
from dlss5_enabler.operations.uninstall import InstallSnapshot, revert_record_mutations
from dlss5_enabler.schemas.strategy import InstallStrategy

logger = get_logger("pipeline")
console = Console(highlight=False)


@dataclass(frozen=True)
class TargetAnalysis:
    architecture: PeArch
    apis: tuple[DetectedApi, ...]
    native_dlss: bool
    previous_record: InstallRecord | None = None


class PipelineStatus(str, Enum):
    COMPLETED = "completed"
    CLEANUP_PENDING = "cleanup_pending"
    FAILED = "failed"
    RECOVERY_FAILED = "recovery_failed"


@dataclass(frozen=True)
class PipelineResult:
    status: PipelineStatus
    failed_step: str = ""
    message: str = ""
    recovery_errors: tuple[str, ...] = ()
    cleanup_errors: tuple[str, ...] = ()
    recovery_path: Path | None = None

    @property
    def success(self) -> bool:
        return self.status in {PipelineStatus.COMPLETED, PipelineStatus.CLEANUP_PENDING}


@dataclass
class PipelineContext:
    game_exe: Path
    force_download: bool = False
    verbose: bool = False
    strategy: InstallStrategy = InstallStrategy.RENODX
    game_dir: Path = field(default_factory=Path)
    analysis: TargetAnalysis | None = None
    record: InstallRecord = field(default_factory=lambda: InstallRecord(game_exe="", game_dir=""))
    step_timings: dict[str, float] = field(default_factory=dict[str, float])
    failed_step: str = ""
    error_message: str = ""
    exception: Exception | None = None
    previous_install_snapshot: InstallSnapshot | None = None
    upstream_warnings: list[ResolutionWarning] = field(default_factory=list[ResolutionWarning])
    recovery_errors: list[str] = field(default_factory=list[str])
    cleanup_errors: list[str] = field(default_factory=list[str])


ContextT_contra = TypeVar("ContextT_contra", bound=PipelineContext, contravariant=True)


class PipelineStep(ABC, Generic[ContextT_contra]):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @abstractmethod
    def execute(self, ctx: ContextT_contra) -> bool:
        pass

    def rollback(self, ctx: ContextT_contra) -> None:
        pass

    def commit(self, ctx: ContextT_contra) -> None:
        pass

    def cleanup(self, ctx: ContextT_contra) -> None:
        pass


class PipelineRunner(Generic[ContextT_contra]):
    def __init__(self, steps: Sequence[PipelineStep[ContextT_contra]], name: str = "DLSS5 Enabler Pipeline") -> None:
        self.steps = steps
        self.name = name

    def run(self, ctx: ContextT_contra) -> bool:
        return self.run_result(ctx).success

    def run_result(self, ctx: ContextT_contra) -> PipelineResult:
        logger.info(f"Starting {self.name} with {len(self.steps)} stages.")
        total_start = time.perf_counter()
        executed: list[PipelineStep[ContextT_contra]] = []
        for number, step in enumerate(self.steps, 1):
            console.print(f"\n[bold cyan]>> [Stage {number}/{len(self.steps)}] {step.name}: {step.description}[/]")
            started = time.perf_counter()
            executed.append(step)
            try:
                success = step.execute(ctx)
            except Exception as error:
                ctx.exception = error
                ctx.error_message = str(error)
                logger.exception(f"Stage {step.name} raised an exception")
                success = False
            ctx.step_timings[step.name] = time.perf_counter() - started
            if not success:
                ctx.failed_step = step.name
                return self._failure(executed, ctx)
            logger.info(f"Stage {step.name} completed in {ctx.step_timings[step.name]:.2f}s")

        for step in reversed(executed):
            try:
                step.commit(ctx)
            except Exception as error:
                ctx.failed_step = step.name
                ctx.error_message = f"Could not finalize {step.name}: {error}"
                ctx.exception = error
                return self._failure(executed, ctx)

        for step in reversed(executed):
            try:
                step.cleanup(ctx)
            except Exception as error:
                message = f"{step.name}: {error}"
                ctx.cleanup_errors.append(message)
                logger.error(f"Installation committed; recovery snapshot cleanup pending: {message}")

        elapsed = time.perf_counter() - total_start
        console.print(f"\n[bold green][OK] Installation committed in {elapsed:.2f}s.[/]")
        if ctx.upstream_warnings:
            console.print("[bold yellow]Validated upstream fallbacks used:[/]")
            for warning in ctx.upstream_warnings:
                console.print(f"[yellow]- {warning.render()}[/]")
        for message in ctx.cleanup_errors:
            console.print(f"[yellow]Installation is active; cleanup pending: {message}[/]")
        return PipelineResult(
            PipelineStatus.CLEANUP_PENDING if ctx.cleanup_errors else PipelineStatus.COMPLETED,
            cleanup_errors=tuple(ctx.cleanup_errors),
            recovery_path=ctx.previous_install_snapshot.root if ctx.previous_install_snapshot else None,
        )

    def _failure(self, executed: list[PipelineStep[ContextT_contra]], ctx: ContextT_contra) -> PipelineResult:
        logger.error(f"Stage {ctx.failed_step} failed: {ctx.error_message}")
        console.print(f"[bold red][X] Stage '{ctx.failed_step}' failed: {ctx.error_message}[/]")
        self._handle_rollback(executed, ctx)
        recovery_path = ctx.previous_install_snapshot.root if ctx.previous_install_snapshot else None
        for message in ctx.recovery_errors:
            console.print(f"[bold red]Recovery incomplete: {message}[/]")
        if recovery_path:
            console.print(f"[yellow]Recovery snapshot retained: {recovery_path}[/]")
        return PipelineResult(
            PipelineStatus.RECOVERY_FAILED if ctx.recovery_errors else PipelineStatus.FAILED,
            failed_step=ctx.failed_step,
            message=ctx.error_message,
            recovery_errors=tuple(ctx.recovery_errors),
            recovery_path=recovery_path,
        )

    def _handle_rollback(self, executed: list[PipelineStep[ContextT_contra]], ctx: ContextT_contra) -> None:
        console.print("[yellow]Restoring the state from before this operation...[/]")
        try:
            if not revert_record_mutations(ctx.record, logger.info):
                ctx.recovery_errors.append("One or more recorded mutations could not be restored.")
        except Exception as error:
            ctx.recovery_errors.append(f"Recorded mutations: {error}")
        for step in reversed(executed):
            try:
                step.rollback(ctx)
            except Exception as error:
                ctx.recovery_errors.append(f"{step.name}: {error}")
