from pathlib import Path
from typing import cast

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.operations.contexts import OptiScalerContext, RenoDxContext
from dlss5_enabler.operations.optiscaler import build_optiscaler_pipeline
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineResult, PipelineRunner
from dlss5_enabler.operations.renodx import build_renodx_pipeline
from dlss5_enabler.schemas.strategy import InstallStrategy

__all__ = [
    "build_install_pipeline",
    "run_install",
]

logger = get_logger("install")


def build_install_pipeline(strategy: InstallStrategy = InstallStrategy.RENODX) -> PipelineRunner[PipelineContext]:
    selected = InstallStrategy(strategy)
    if selected is InstallStrategy.RENODX:
        return cast(PipelineRunner[PipelineContext], build_renodx_pipeline())
    return cast(PipelineRunner[PipelineContext], build_optiscaler_pipeline())


def run_install(
    game_exe_path: Path | str,
    install_lumenite: bool = True,
    d3d9_translate: bool = False,
    opengl: bool = False,
    install_vulkan_layer: bool = False,
    force_download: bool = False,
    verbose: bool = False,
    strategy: InstallStrategy = InstallStrategy.RENODX,
    optiscaler_archive: Path | None = None,
    optiscaler_nr_passes: int = 1,
    optiscaler_proxy: str = "dxgi.dll",
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
            strategy=strategy,
            optiscaler_archive=optiscaler_archive,
            optiscaler_nr_passes=optiscaler_nr_passes,
            optiscaler_proxy=optiscaler_proxy,
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
    optiscaler_archive: Path | None = None,
    optiscaler_source_revision: str = "",
    optiscaler_nr_passes: int = 1,
    optiscaler_proxy: str = "dxgi.dll",
) -> PipelineResult:
    game_exe = Path(game_exe_path).resolve()
    selected = InstallStrategy(strategy)
    if selected is InstallStrategy.OPTISCALER:
        optiscaler_ctx = OptiScalerContext(
            game_exe=game_exe,
            force_download=force_download,
            verbose=verbose,
            strategy=selected,
            archive_path=optiscaler_archive,
            source_revision=optiscaler_source_revision,
            nr_passes=optiscaler_nr_passes,
            proxy_name=optiscaler_proxy,
        )
        return build_optiscaler_pipeline().run_result(optiscaler_ctx)
    ctx = RenoDxContext(
        game_exe=game_exe,
        install_lumenite=install_lumenite,
        d3d9_translate=d3d9_translate,
        opengl=opengl,
        install_vulkan_layer=install_vulkan_layer,
        force_download=force_download,
        verbose=verbose,
        strategy=selected,
    )
    return build_renodx_pipeline().run_result(ctx)
