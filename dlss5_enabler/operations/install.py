from collections.abc import Sequence
from pathlib import Path

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineRunner, PipelineStep
from dlss5_enabler.operations.reshade import ensure_mv_provider_def, normalize_search_paths, reshade_headless_install
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
)

__all__ = [
    "build_install_pipeline",
    "ensure_mv_provider_def",
    "normalize_search_paths",
    "reshade_headless_install",
    "run_install",
]

logger = get_logger("install")


def build_install_pipeline() -> PipelineRunner:
    steps: Sequence[PipelineStep] = [
        StepValidateTarget(),
        StepFetchUpstream(),
        StepCleanPreviousInstall(),
        StepInstallReShade(),
        StepInstallD3D9Translation(),
        StepInjectFeederAndHeaders(),
        StepInjectRenoDxAndNgx(),
        StepConfigureMotionVectors(),
        StepInstallVulkanLayer(),
        StepMirrorDualLocations(),
        StepConfigureWineOverrides(),
        StepSaveRecord(),
    ]
    return PipelineRunner(steps, name="DLSS5 Enabler Installation Pipeline")


def run_install(
    game_exe_path: Path | str,
    install_lumenite: bool = True,
    d3d9_translate: bool = False,
    opengl: bool = False,
    install_vulkan_layer: bool = False,
    force_download: bool = False,
    verbose: bool = False,
) -> bool:
    game_exe = Path(game_exe_path).resolve()
    with resource_lock(game_exe.parent / ".dlss5-enabler-install-operation"):
        return _run_install_unlocked(
            game_exe,
            install_lumenite=install_lumenite,
            d3d9_translate=d3d9_translate,
            opengl=opengl,
            install_vulkan_layer=install_vulkan_layer,
            force_download=force_download,
            verbose=verbose,
        )


def _run_install_unlocked(
    game_exe_path: Path | str,
    install_lumenite: bool = True,
    d3d9_translate: bool = False,
    opengl: bool = False,
    install_vulkan_layer: bool = False,
    force_download: bool = False,
    verbose: bool = False,
) -> bool:
    game_exe = Path(game_exe_path).resolve()
    ctx = PipelineContext(
        game_exe=game_exe,
        install_lumenite=install_lumenite,
        d3d9_translate=d3d9_translate,
        opengl=opengl,
        install_vulkan_layer=install_vulkan_layer,
        force_download=force_download,
        verbose=verbose,
    )
    runner = build_install_pipeline()
    return runner.run(ctx)
