from pathlib import Path

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.operations.contexts import RenoDxContext
from dlss5_enabler.operations.pipeline import PipelineResult, PipelineRunner
from dlss5_enabler.operations.renodx import build_renodx_pipeline
from dlss5_enabler.schemas.strategy import InstallStrategy

__all__ = [
    "build_install_pipeline",
    "run_install",
]

logger = get_logger("install")


def build_install_pipeline(strategy: InstallStrategy = InstallStrategy.RENODX) -> PipelineRunner[RenoDxContext]:
    selected = InstallStrategy(strategy)
    if selected is InstallStrategy.RENODX:
        return build_renodx_pipeline()
    raise ValueError(f"Unsupported installation strategy: {selected}")


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
        ).success


def _run_install_unlocked(
    game_exe_path: Path | str,
    install_lumenite: bool = True,
    d3d9_translate: bool = False,
    opengl: bool = False,
    install_vulkan_layer: bool = False,
    force_download: bool = False,
    verbose: bool = False,
    strategy: InstallStrategy = InstallStrategy.RENODX,
) -> PipelineResult:
    game_exe = Path(game_exe_path).resolve()
    ctx = RenoDxContext(
        game_exe=game_exe,
        install_lumenite=install_lumenite,
        d3d9_translate=d3d9_translate,
        opengl=opengl,
        install_vulkan_layer=install_vulkan_layer,
        force_download=force_download,
        verbose=verbose,
    )
    ctx.strategy = InstallStrategy(strategy)
    runner = build_install_pipeline(ctx.strategy)
    return runner.run_result(ctx)
