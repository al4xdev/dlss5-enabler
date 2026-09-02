from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.console import Console

from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.core.pe import PeArch
from dlss5_enabler.core.record import InstallRecord
from dlss5_enabler.network.sources import (
    DgvoodooBundle,
    FeederBundle,
    LumeniteBundle,
    NgxBundle,
    RenoDxBundle,
    ReshadeBundle,
    ReshadeHeaders,
)
from dlss5_enabler.operations.uninstall import revert_record_mutations

if TYPE_CHECKING:
    from dlss5_enabler.operations.uninstall import InstallSnapshot

logger = get_logger("pipeline")
console = Console(highlight=False)


@dataclass
class PipelineContext:
    game_exe: Path
    install_lumenite: bool = True
    d3d9_translate: bool = False
    opengl: bool = False
    install_vulkan_layer: bool = False
    force_download: bool = False
    verbose: bool = False

    game_dir: Path = field(default_factory=Path)
    reshade_dir: Path = field(default_factory=Path)
    pe_arch: PeArch = PeArch.UNKNOWN
    is_32bit: bool = False
    reshade_api: str = "dxgi"
    reshade_dll_name: str = "dxgi.dll"
    need_reshade: bool = True

    reshade_bundle: ReshadeBundle | None = None
    feeder_bundle: FeederBundle | None = None
    renodx_bundle: RenoDxBundle | None = None
    ngx_bundle: NgxBundle | None = None
    headers_bundle: ReshadeHeaders | None = None
    dgvoodoo_bundle: DgvoodooBundle | None = None
    lumenite_bundle: LumeniteBundle | None = None

    record: InstallRecord = field(default_factory=lambda: InstallRecord(game_exe="", game_dir=""))

    step_timings: dict[str, float] = field(default_factory=lambda: cast(dict[str, float], {}))
    failed_step: str = ""
    error_message: str = ""
    exception: Exception | None = None
    previous_install_snapshot: InstallSnapshot | None = None


class PipelineStep(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @abstractmethod
    def execute(self, ctx: PipelineContext) -> bool:
        pass

    def rollback(self, ctx: PipelineContext) -> None:
        pass

    def commit(self, ctx: PipelineContext) -> None:
        pass


class PipelineRunner:
    def __init__(self, steps: Sequence[PipelineStep], name: str = "DLSS5 Enabler Pipeline"):
        self.steps: Sequence[PipelineStep] = steps
        self.name: str = name

    def run(self, ctx: PipelineContext) -> bool:
        logger.info(f"Starting {self.name} with {len(self.steps)} stages.")
        total_start = time.perf_counter()
        executed_steps: list[PipelineStep] = []

        for i, step in enumerate(self.steps, 1):
            stage_header = f"[Stage {i}/{len(self.steps)}] {step.name}: {step.description}"
            console.print(f"\n[bold cyan]>> {stage_header}[/bold cyan]")
            logger.info(f"--> [STAGE {i}/{len(self.steps)}] {step.name} STARTED")

            step_start = time.perf_counter()
            try:
                success = step.execute(ctx)
                duration = time.perf_counter() - step_start
                ctx.step_timings[step.name] = duration

                if not success:
                    ctx.failed_step = step.name
                    logger.error(f"<-- [STAGE {i}] {step.name} FAILED in {duration:.2f}s: {ctx.error_message}")
                    console.print(f"[bold red][X] Stage '{step.name}' returned failure: {ctx.error_message}[/bold red]")
                    self._handle_rollback([*executed_steps, step], ctx)
                    return False

                executed_steps.append(step)
                logger.info(f"<-- [STAGE {i}] {step.name} COMPLETED in {duration:.2f}s")
                console.print(
                    f"[bold green][OK] Stage '{step.name}' finished successfully ({duration:.2f}s).[/bold green]"
                )

            except Exception as e:
                duration = time.perf_counter() - step_start
                ctx.step_timings[step.name] = duration
                ctx.failed_step = step.name
                ctx.error_message = str(e)
                ctx.exception = e
                logger.exception(f"<-- [STAGE {i}] {step.name} CRASHED with exception: {e}")
                console.print(f"[bold red][X] Stage '{step.name}' raised an exception: {e}[/bold red]")
                self._handle_rollback([*executed_steps, step], ctx)
                return False

        total_duration = time.perf_counter() - total_start
        logger.info(f"Pipeline completed successfully in {total_duration:.2f}s.")
        console.print(
            f"\n[bold green][OK] All {len(self.steps)} pipeline stages completed in {total_duration:.2f}s![/bold green]"
        )
        for step in reversed(executed_steps):
            try:
                step.commit(ctx)
            except Exception as error:
                logger.error(f"Error committing step {step.name}: {error}")
        return True

    def _handle_rollback(self, executed_steps: list[PipelineStep], ctx: PipelineContext) -> None:
        logger.warning(f"Initiating rollback for {len(executed_steps)} executed steps...")
        console.print("[yellow]Initiating pipeline rollback to restore clean state...[/yellow]")
        if not revert_record_mutations(ctx.record, logger.info):
            logger.error("One or more recorded mutations could not be rolled back.")
        for step in reversed(executed_steps):
            try:
                logger.info(f"Rolling back step: {step.name}")
                step.rollback(ctx)
            except Exception as e:
                logger.error(f"Error during rollback of {step.name}: {e}")
