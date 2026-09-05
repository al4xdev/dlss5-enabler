from dataclasses import dataclass, field
from pathlib import Path

from dlss5_enabler.core.pe import PeArch
from dlss5_enabler.network.sources import (
    DgvoodooBundle,
    DlssgBundle,
    FeederBundle,
    LumeniteBundle,
    NgxBundle,
    OptiScalerBundle,
    RenoDxBundle,
    ReshadeBundle,
    ReshadeHeaders,
)
from dlss5_enabler.operations.pipeline import PipelineContext
from dlss5_enabler.schemas.strategy import FrameGenerationMode, GpuGeneration, NrPlacement


@dataclass
class RenoDxContext(PipelineContext):
    install_lumenite: bool = True
    d3d9_translate: bool = False
    opengl: bool = False
    install_vulkan_layer: bool = False
    reshade_dir: Path = field(default_factory=Path)
    pe_arch: PeArch = PeArch.UNKNOWN
    is_32bit: bool = False
    reshade_api: str = "dxgi"
    reshade_dll_name: str = "dxgi.dll"
    need_reshade: bool = True
    native_dlss_detected: bool = False
    install_feeder: bool = True
    reshade_bundle: ReshadeBundle | None = None
    feeder_bundle: FeederBundle | None = None
    renodx_bundle: RenoDxBundle | None = None
    ngx_bundle: NgxBundle | None = None
    headers_bundle: ReshadeHeaders | None = None
    dgvoodoo_bundle: DgvoodooBundle | None = None
    lumenite_bundle: LumeniteBundle | None = None
    prepared_reshade: dict[str, Path] = field(default_factory=dict[str, Path])
    staging_directory: Path | None = None


@dataclass
class OptiScalerContext(PipelineContext):
    archive_path: Path | None = None
    source_revision: str = ""
    nr_passes: int = 1
    proxy_name: str = "dxgi.dll"
    frame_generation: FrameGenerationMode = FrameGenerationMode.AUTO
    fg_multiplier: int = 2
    nr_placement: NrPlacement = NrPlacement.AFTER
    gpu_generation: GpuGeneration = "unknown"
    bundle: OptiScalerBundle | None = None
    ngx_bundle: NgxBundle | None = None
    dlssg_bundle: DlssgBundle | None = None
    staging_directory: Path | None = None
    staged_files: dict[str, Path] = field(default_factory=dict[str, Path])
