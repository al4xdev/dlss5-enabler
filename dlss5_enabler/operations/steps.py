import shutil
import tempfile
from pathlib import Path

from rich.console import Console

from dlss5_enabler.core.fileio import _atomic_copy_file_unlocked
from dlss5_enabler.core.ini import ini_get_exact, ini_set_exact
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.core.mutations import (
    managed_file_lock,
    prepare_managed_path,
    prepare_runtime_artifacts,
    track_created_directories,
)
from dlss5_enabler.core.pe import DetectedApi, PeArch, check_api_mismatches
from dlss5_enabler.core.record import (
    IniTouch,
    InstallOptions,
    RecordedFile,
    RegistryTouch,
    RenoDxStrategyOptions,
)
from dlss5_enabler.core.util import (
    get_cache_dir,
    sha256_file,
    unblock_file,
)
from dlss5_enabler.network.sources import (
    fetch_dgvoodoo,
    fetch_feeder,
    fetch_lumenite,
    fetch_ngx_dlls,
    fetch_renodx_dlss5,
    fetch_reshade,
    fetch_reshade_headers,
    zip_extract_matching,
    zip_has_matching,
)
from dlss5_enabler.operations.contexts import RenoDxContext
from dlss5_enabler.operations.pipeline import PipelineStep
from dlss5_enabler.operations.reshade import (
    ensure_mv_provider_def,
    extract_reshade_dlls_from_installer,
    normalize_search_paths,
)
from dlss5_enabler.platform import ProtonManager, get_platform_adapter

logger = get_logger("steps")
console = Console(highlight=False)
_RESHADER_RUNTIME_ARTIFACTS = (
    "ReShade.log",
    "ReShade.ini.bak",
    "ReShadePreset.ini",
    "dxgi.log",
    "dlss5-feed.cfg",
    "dlss5-feed.log",
)


def _prepare_managed_path(ctx: RenoDxContext, dst: Path) -> RecordedFile:
    return prepare_managed_path(ctx.record, dst)


def _place_file(ctx: RenoDxContext, src: Path, dst: Path) -> None:
    with managed_file_lock(ctx.record, dst) as item:
        _atomic_copy_file_unlocked(src, dst)
        unblock_file(dst)
        item.size_bytes = dst.stat().st_size
        item.sha256 = sha256_file(dst)
    logger.debug(f"Placed file: {dst}")


def _prepare_reshade_runtime_artifacts(ctx: RenoDxContext) -> None:
    directories = [ctx.reshade_dir]
    if ctx.is_32bit:
        directories.append(ctx.reshade_dir / "host64")
    for directory in directories:
        for name in _RESHADER_RUNTIME_ARTIFACTS:
            _prepare_managed_path(ctx, directory / name)


def _install_reshade_from_extraction(
    ctx: RenoDxContext,
    setup_exe: Path,
    target_dll: Path,
    ini_path: Path,
    bitness_key: str,
) -> bool:
    logger.info("Placing the architecture-specific ReShade runtime from isolated extraction.")
    if ctx.prepared_reshade:
        source_dll = ctx.prepared_reshade.get(bitness_key)
        if source_dll is None:
            return False
        _place_file(ctx, source_dll, target_dll)
    else:
        with tempfile.TemporaryDirectory(prefix="dlss5-enabler-reshade-", dir=get_cache_dir()) as stage_name:
            dlls = extract_reshade_dlls_from_installer(setup_exe, Path(stage_name))
            source_dll = dlls.get(bitness_key)
            if source_dll is None:
                return False
            _place_file(ctx, source_dll, target_dll)
    if ini_path.is_file():
        return True
    _prepare_managed_path(ctx, ini_path)
    effect_ok = ini_set_exact(ini_path, "GENERAL", "EffectSearchPaths", "./reshade-shaders/Shaders/**")
    texture_ok = ini_set_exact(ini_path, "GENERAL", "TextureSearchPaths", "./reshade-shaders/Textures/**")
    return effect_ok and texture_ok


class StepConfigureRenoDx(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "ConfigureRenoDx"

    @property
    def description(self) -> str:
        return "Selects the RenoDX components and hook from the target analysis"

    def execute(self, ctx: RenoDxContext) -> bool:
        analysis = ctx.analysis
        if analysis is None:
            ctx.error_message = "Target analysis is required before selecting the RenoDX installation."
            return False
        if ctx.d3d9_auto:
            ctx.d3d9_translate = DetectedApi.D3D9 in analysis.apis and not ctx.opengl
            logger.info(
                "DirectX 9 translation auto-detection: " + ("enabled" if ctx.d3d9_translate else "not required")
            )
        if ctx.d3d9_translate and ctx.opengl:
            ctx.error_message = "D3D9 translation and OpenGL mode cannot be enabled together."
            return False
        ctx.reshade_dir = ctx.game_dir
        ctx.pe_arch = analysis.architecture
        ctx.is_32bit = ctx.pe_arch == PeArch.X86
        ctx.reshade_api = "opengl" if ctx.opengl else "dxgi"
        ctx.reshade_dll_name = "opengl32.dll" if ctx.opengl else "dxgi.dll"
        ctx.native_dlss_detected = analysis.native_dlss
        ctx.install_feeder = not ctx.native_dlss_detected
        if ctx.native_dlss_detected and ctx.is_32bit:
            ctx.error_message = "Native DLSS was detected, but direct RenoDX installation requires a 64-bit game."
            return False
        if ctx.native_dlss_detected:
            logger.info("Native DLSS detected; installing RenoDX directly without DLSS5-Feeder.")
        existing_proxy = ctx.game_dir / ctx.reshade_dll_name
        if existing_proxy.is_file() and not (ctx.game_dir / "ReShade.ini").is_file():
            ctx.error_message = (
                f"Existing {ctx.reshade_dll_name} has no ReShade.ini; refusing to assume it is ReShade. "
                "Remove or rename it before installing."
            )
            return False
        previous = analysis.previous_record
        ctx.need_reshade = not existing_proxy.is_file() or (previous is not None and previous.reshade_by_us)
        for warning in check_api_mismatches(
            ctx.game_exe,
            d3d9=ctx.d3d9_translate,
            opengl=ctx.opengl,
            vulkan_layer=ctx.install_vulkan_layer,
        ):
            logger.warning(warning)
            console.print(f"[bold yellow][WARNING] {warning}[/]")
        install_type = "D3D9 (dgVoodoo2)" if ctx.d3d9_translate else ("OpenGL" if ctx.opengl else "D3D11/D3D12")
        if ctx.install_vulkan_layer:
            install_type += " + Vulkan Layer"
        ctx.record.install_type = install_type
        ctx.record.d3d9_translate = ctx.d3d9_translate
        ctx.record.opengl = ctx.opengl
        ctx.record.vulkan_layer = ctx.install_vulkan_layer
        ctx.record.lumenite_installed = ctx.install_lumenite
        ctx.record.native_dlss_detected = ctx.native_dlss_detected
        ctx.record.install_options = InstallOptions(
            lumenite=ctx.install_lumenite,
            d3d9=ctx.d3d9_translate,
            opengl=ctx.opengl,
            vulkan_layer=ctx.install_vulkan_layer,
        )
        ctx.record.strategy_options = RenoDxStrategyOptions.from_install_options(ctx.record.install_options)
        logger.info(
            f"Selected engine: {ctx.strategy.value}; native_dlss={analysis.native_dlss}; proxy={ctx.reshade_dll_name}"
        )
        return True


class StepFetchUpstream(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "FetchUpstream"

    @property
    def description(self) -> str:
        return "Fetches/validates required upstream components from GitHub & ReShade"

    def execute(self, ctx: RenoDxContext) -> bool:
        if ctx.need_reshade or ctx.is_32bit:
            logger.info("Fetching ReShade Addon installer...")
            ctx.reshade_bundle = fetch_reshade(logger.info, force=ctx.force_download)
            ctx.record.binaries.update(ctx.reshade_bundle.binaries)
            ctx.upstream_warnings.extend(ctx.reshade_bundle.warnings)

        if ctx.install_feeder:
            logger.info("Fetching DLSS5-Feeder bundle...")
            ctx.feeder_bundle = fetch_feeder(logger.info, force=ctx.force_download)
            ctx.record.binaries.update(ctx.feeder_bundle.binaries)
            ctx.upstream_warnings.extend(ctx.feeder_bundle.warnings)

        logger.info("Fetching RenoDX DLSS 5 addon...")
        ctx.renodx_bundle = fetch_renodx_dlss5(logger.info, force=ctx.force_download)
        ctx.record.binaries.update(ctx.renodx_bundle.binaries)
        ctx.upstream_warnings.extend(ctx.renodx_bundle.warnings)

        logger.info("Fetching DLSS Neural Rendering and SR DLLs...")
        ctx.ngx_bundle = fetch_ngx_dlls(logger.info, force=ctx.force_download)
        ctx.record.binaries.update(ctx.ngx_bundle.binaries)
        if not ctx.install_feeder:
            ctx.record.binaries.pop("nvngx_dlss.dll", None)
        ctx.upstream_warnings.extend(ctx.ngx_bundle.warnings)

        if ctx.install_feeder or ctx.install_lumenite:
            logger.info("Fetching standard ReShade shader headers...")
            ctx.headers_bundle = fetch_reshade_headers(logger.info, force=ctx.force_download)
            ctx.record.binaries.update(ctx.headers_bundle.binaries)
            ctx.upstream_warnings.extend(ctx.headers_bundle.warnings)

        if ctx.d3d9_translate:
            logger.info("Fetching dgVoodoo2 bundle...")
            architecture = "x86" if ctx.is_32bit else "x64"
            ctx.dgvoodoo_bundle = fetch_dgvoodoo(logger.info, force=ctx.force_download, architecture=architecture)
            ctx.record.binaries.update(ctx.dgvoodoo_bundle.binaries)
            ctx.upstream_warnings.extend(ctx.dgvoodoo_bundle.warnings)

        if ctx.install_lumenite:
            logger.info("Fetching LumeniteFX motion-vector shaders...")
            ctx.lumenite_bundle = fetch_lumenite(logger.info, force=ctx.force_download)
            ctx.record.binaries.update(ctx.lumenite_bundle.binaries)
            ctx.upstream_warnings.extend(ctx.lumenite_bundle.warnings)

        return True


class StepPrepareRenoDx(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "PrepareRenoDx"

    @property
    def description(self) -> str:
        return "Validates the selected files and stages ReShade before changing the game"

    def execute(self, ctx: RenoDxContext) -> bool:
        required: list[Path | None] = []
        if not ctx.renodx_bundle or not ctx.ngx_bundle:
            raise ValueError("RenoDX and NGX bundles are required.")
        required.extend((ctx.renodx_bundle.addon64_path, ctx.ngx_bundle.nr_dll_path))
        if ctx.install_feeder:
            if not ctx.feeder_bundle:
                raise ValueError("Feeder bundle is required.")
            required.extend(
                (
                    ctx.feeder_bundle.addon32 if ctx.is_32bit else ctx.feeder_bundle.addon64,
                    ctx.feeder_bundle.fx_shader,
                    ctx.ngx_bundle.sr_dll_path,
                )
            )
            if ctx.is_32bit:
                required.append(ctx.feeder_bundle.host64_exe)
            if ctx.install_vulkan_layer:
                required.append(ctx.feeder_bundle.vk_layer_zip)
        if ctx.install_feeder or ctx.install_lumenite:
            if not ctx.headers_bundle:
                raise ValueError("ReShade shader headers are required.")
            required.extend(
                (ctx.headers_bundle.fxh_path, ctx.headers_bundle.ui_fxh_path, ctx.headers_bundle.drawtext_path)
            )
        if ctx.d3d9_translate:
            if not ctx.dgvoodoo_bundle:
                raise ValueError("dgVoodoo2 bundle is required.")
            required.extend((ctx.dgvoodoo_bundle.d3d9_dll, ctx.dgvoodoo_bundle.conf, ctx.dgvoodoo_bundle.cpl))
        if ctx.install_lumenite:
            if not ctx.lumenite_bundle or not ctx.lumenite_bundle.files:
                raise ValueError("LumeniteFX shader bundle is required.")
            required.extend(ctx.lumenite_bundle.files)
        for path in required:
            if path is None or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Required RenoDX installation file is missing or empty: {path}")
        if ctx.need_reshade or ctx.is_32bit:
            if not ctx.reshade_bundle or not ctx.reshade_bundle.setup_exe_path:
                raise ValueError("ReShade Addon bundle is required.")
            ctx.staging_directory = Path(tempfile.mkdtemp(prefix="dlss5-enabler-reshade-", dir=get_cache_dir()))
            ctx.prepared_reshade = extract_reshade_dlls_from_installer(
                ctx.reshade_bundle.setup_exe_path, ctx.staging_directory
            )
            expected = ("reshade32.dll", "reshade64.dll") if ctx.is_32bit else ("reshade64.dll",)
            if any(name not in ctx.prepared_reshade for name in expected):
                raise ValueError("ReShade archive is missing the required architecture runtimes.")
        return True

    def rollback(self, ctx: RenoDxContext) -> None:
        self.cleanup(ctx)

    def cleanup(self, ctx: RenoDxContext) -> None:
        if ctx.staging_directory is not None:
            stage = ctx.staging_directory.resolve()
            if stage.parent != get_cache_dir().resolve() or not stage.name.startswith("dlss5-enabler-reshade-"):
                raise ValueError(f"Invalid staging directory: {stage}")
            shutil.rmtree(stage)
            ctx.staging_directory = None
            ctx.prepared_reshade.clear()


class StepInstallReShade(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "InstallReShade"

    @property
    def description(self) -> str:
        return "Places the staged ReShade runtime and configures its managed INI"

    def execute(self, ctx: RenoDxContext) -> bool:
        game_ini = ctx.reshade_dir / "ReShade.ini"
        if ctx.need_reshade:
            if not ctx.reshade_bundle or not ctx.reshade_bundle.setup_exe_path:
                ctx.error_message = "ReShade Addon installer bundle is missing."
                return False
            _prepare_reshade_runtime_artifacts(ctx)
            bitness_key = "reshade32.dll" if ctx.is_32bit else "reshade64.dll"
            if not _install_reshade_from_extraction(
                ctx, ctx.reshade_bundle.setup_exe_path, ctx.reshade_dir / ctx.reshade_dll_name, game_ini, bitness_key
            ):
                ctx.error_message = "ReShade extraction could not create the required runtime."
                return False
            ctx.record.reshade_by_us = True
        ctx.record.reshade_dir = ctx.reshade_dir.as_posix()
        if not normalize_search_paths(game_ini, ctx.record):
            ctx.error_message = "Could not normalize the ReShade search paths."
            return False
        return True


class StepInstallD3D9Translation(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "InstallD3D9Translation"

    @property
    def description(self) -> str:
        return "Configures dgVoodoo2 D3D9->D3D11 translation layer"

    def execute(self, ctx: RenoDxContext) -> bool:
        if not ctx.d3d9_translate or not ctx.dgvoodoo_bundle:
            return True

        logger.info(f"Installing dgVoodoo2 {ctx.dgvoodoo_bundle.version}...")
        if ctx.dgvoodoo_bundle.d3d9_dll:
            _place_file(ctx, ctx.dgvoodoo_bundle.d3d9_dll, ctx.reshade_dir / "d3d9.dll")
        if ctx.dgvoodoo_bundle.cpl:
            _place_file(ctx, ctx.dgvoodoo_bundle.cpl, ctx.reshade_dir / "dgVoodooCpl.exe")
        if ctx.dgvoodoo_bundle.conf:
            _place_file(ctx, ctx.dgvoodoo_bundle.conf, ctx.reshade_dir / "dgVoodoo.conf")

        placed_conf = ctx.reshade_dir / "dgVoodoo.conf"
        dg_settings = [
            ("DirectX", "DisableAndPassThru", "false"),
            ("DirectX", "VideoCard", "internal3D"),
            ("DirectX", "VRAM", "1024"),
            ("DirectX", "dgVoodooWatermark", "false"),
            ("General", "OutputAPI", "d3d11_fl11_0"),
        ]
        for sec, k, v in dg_settings:
            _had, orig = ini_get_exact(placed_conf, sec, k)
            if orig.strip().lower() != v.lower():
                if not ini_set_exact(placed_conf, sec, k, v):
                    ctx.error_message = f"Could not configure {placed_conf.name}."
                    return False
                ctx.record.ini_touched.append(
                    IniTouch(path=str(placed_conf), section=sec, key=k, original=orig.strip())
                )
        return True


class StepInjectFeederAndHeaders(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "InjectFeederAndHeaders"

    @property
    def description(self) -> str:
        return "Installs shader headers and DLSS5-Feeder only when the game has no native DLSS"

    def execute(self, ctx: RenoDxContext) -> bool:
        if not ctx.headers_bundle:
            if not ctx.install_feeder and not ctx.install_lumenite:
                return True
            ctx.error_message = "ReShade headers bundle missing."
            return False

        shaders_dir = ctx.reshade_dir / "reshade-shaders" / "Shaders"
        if ctx.install_feeder:
            for name in ("dlss5-feed.cfg", "dlss5-feed.log"):
                _prepare_managed_path(ctx, ctx.reshade_dir / name)
            if not ctx.feeder_bundle:
                ctx.error_message = "Feeder bundle missing."
                return False
            if ctx.is_32bit:
                if ctx.feeder_bundle.addon32:
                    _place_file(ctx, ctx.feeder_bundle.addon32, ctx.reshade_dir / "dlss5-feed.addon32")
            elif ctx.feeder_bundle.addon64:
                _place_file(ctx, ctx.feeder_bundle.addon64, ctx.reshade_dir / "dlss5-feed.addon64")
            if ctx.feeder_bundle.fx_shader:
                _place_file(ctx, ctx.feeder_bundle.fx_shader, shaders_dir / "DLSS5_Feed.fx")
        else:
            logger.info("Native DLSS path selected; DLSS5-Feeder add-on and feed shader were not installed.")
        if ctx.headers_bundle.fxh_path:
            _place_file(ctx, ctx.headers_bundle.fxh_path, shaders_dir / "ReShade.fxh")
        if ctx.headers_bundle.ui_fxh_path:
            _place_file(ctx, ctx.headers_bundle.ui_fxh_path, shaders_dir / "ReShadeUI.fxh")
        if ctx.headers_bundle.drawtext_path:
            _place_file(ctx, ctx.headers_bundle.drawtext_path, shaders_dir / "DrawText.fxh")
        return True


class StepInjectRenoDxAndNgx(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "InjectRenoDxAndNgx"

    @property
    def description(self) -> str:
        return "Installs RenoDX DLSS 5 add-on and NVIDIA DLSS NR/SR binaries"

    def execute(self, ctx: RenoDxContext) -> bool:
        if not ctx.renodx_bundle or not ctx.ngx_bundle or (ctx.install_feeder and not ctx.feeder_bundle):
            ctx.error_message = "RenoDX or NGX bundle missing."
            return False

        if ctx.is_32bit:
            if ctx.feeder_bundle is None:
                ctx.error_message = "Feeder bundle missing for the 32-bit host."
                return False
            host_dir = ctx.reshade_dir / "host64"
            track_created_directories(ctx.record, host_dir)
            _prepare_managed_path(ctx, host_dir / "dlss5-feed-host.log")
            prepare_runtime_artifacts(ctx.record, host_dir, "dlss5-feed-host64*.png")
            host_dir.mkdir(parents=True, exist_ok=True)
            if ctx.feeder_bundle.host64_exe:
                _place_file(ctx, ctx.feeder_bundle.host64_exe, host_dir / "dlss5-feed-host64.exe")

            host_dxgi = host_dir / ctx.reshade_dll_name
            if not host_dxgi.is_file() and ctx.reshade_bundle and ctx.reshade_bundle.setup_exe_path:
                host_ini = host_dir / "ReShade.ini"
                managed_host_dll = _prepare_managed_path(ctx, host_dxgi)
                _prepare_managed_path(ctx, host_ini)
                for name in _RESHADER_RUNTIME_ARTIFACTS:
                    _prepare_managed_path(ctx, host_dir / name)
                if not _install_reshade_from_extraction(
                    ctx, ctx.reshade_bundle.setup_exe_path, host_dxgi, host_ini, "reshade64.dll"
                ):
                    ctx.error_message = "Could not install 64-bit ReShade runtime for the feeder host."
                    return False
                managed_host_dll.size_bytes = host_dxgi.stat().st_size
                managed_host_dll.sha256 = sha256_file(host_dxgi)

            if ctx.renodx_bundle.addon64_path:
                _place_file(ctx, ctx.renodx_bundle.addon64_path, host_dir / "renodx-dlss5.addon64")
            if ctx.ngx_bundle.nr_dll_path:
                _place_file(ctx, ctx.ngx_bundle.nr_dll_path, host_dir / "nvngx_dlssnr.dll")
            if ctx.ngx_bundle.sr_dll_path:
                _place_file(ctx, ctx.ngx_bundle.sr_dll_path, host_dir / "nvngx_dlss.dll")
        else:
            if ctx.renodx_bundle.addon64_path:
                _place_file(ctx, ctx.renodx_bundle.addon64_path, ctx.reshade_dir / "renodx-dlss5.addon64")
            if ctx.ngx_bundle.nr_dll_path:
                _place_file(ctx, ctx.ngx_bundle.nr_dll_path, ctx.reshade_dir / "nvngx_dlssnr.dll")
            if ctx.ngx_bundle.sr_dll_path and ctx.install_feeder:
                _place_file(ctx, ctx.ngx_bundle.sr_dll_path, ctx.reshade_dir / "nvngx_dlss.dll")

        return True


class StepConfigureMotionVectors(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "ConfigureMotionVectors"

    @property
    def description(self) -> str:
        return "Installs optional LumeniteFX motion vectors and configures DLSS5-Feeder when used"

    def execute(self, ctx: RenoDxContext) -> bool:
        if ctx.install_lumenite and ctx.lumenite_bundle and ctx.lumenite_bundle.staging_dir:
            logger.info("Placing LumeniteFX shaders into reshade-shaders layout...")
            for src in ctx.lumenite_bundle.files:
                rel = src.relative_to(ctx.lumenite_bundle.staging_dir)
                _place_file(ctx, src, ctx.reshade_dir / rel)

        if not ctx.install_feeder:
            logger.info(
                "Native DLSS path selected; LumeniteFX remains available, but DLSS5_MV_PROVIDER was not configured."
            )
            return True

        reshade_ini = ctx.reshade_dir / "ReShade.ini"
        if reshade_ini.is_file() and not ensure_mv_provider_def(reshade_ini, ctx.record):
            ctx.error_message = "Could not configure motion vectors in ReShade.ini."
            return False

        if ctx.is_32bit:
            host_ini = ctx.reshade_dir / "host64" / "ReShade.ini"
            if host_ini.is_file() and (
                not normalize_search_paths(host_ini, ctx.record) or not ensure_mv_provider_def(host_ini, ctx.record)
            ):
                ctx.error_message = "Could not configure motion vectors in the host ReShade.ini."
                return False
        return True


class StepInstallVulkanLayer(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "InstallVulkanLayer"

    @property
    def description(self) -> str:
        return "Extracts Vulkan layer fallback binaries if requested"

    def execute(self, ctx: RenoDxContext) -> bool:
        if not ctx.install_feeder:
            if ctx.install_vulkan_layer:
                logger.warning("Ignoring --vulkan-layer because the native DLSS path does not use DLSS5-Feeder.")
                ctx.record.vulkan_layer = False
            return True
        if ctx.install_vulkan_layer:
            if ctx.feeder_bundle and ctx.feeder_bundle.vk_layer_zip and ctx.feeder_bundle.vk_layer_zip.is_file():
                logger.info("Extracting Vulkan layer files...")
                with tempfile.TemporaryDirectory(prefix="dlss5-enabler-vulkan-", dir=get_cache_dir()) as stage_name:
                    stage = Path(stage_name)
                    architecture = "x86" if ctx.is_32bit else "x64"
                    architecture_patterns = [f"layer-{architecture}/*"]
                    patterns = (
                        architecture_patterns
                        if zip_has_matching(ctx.feeder_bundle.vk_layer_zip, architecture_patterns)
                        else ["*"]
                    )
                    vk_files = zip_extract_matching(ctx.feeder_bundle.vk_layer_zip, stage, patterns, flatten=True)
                    for source in vk_files:
                        _place_file(ctx, source, ctx.reshade_dir / source.name)
            else:
                ctx.record.vulkan_layer = False
                ctx.error_message = "Vulkan layer requested but unavailable in this Feeder release."
                return False
        return True


class StepMirrorDualLocations(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "MirrorDualLocations"

    @property
    def description(self) -> str:
        return "Mirrors placed files into bin/ subfolder for Source engine D3D9 games"

    def execute(self, ctx: RenoDxContext) -> bool:
        if ctx.d3d9_translate:
            alt_bin = ctx.game_dir / "bin"
            if alt_bin.is_dir() and alt_bin != ctx.reshade_dir:
                mirrored = 0
                for recorded in list(ctx.record.files):
                    p = Path(recorded.path)
                    try:
                        if p.is_relative_to(ctx.reshade_dir) and not p.is_relative_to(alt_bin):
                            rel = p.relative_to(ctx.reshade_dir)
                            dst = alt_bin / rel
                            if p.is_file():
                                _place_file(ctx, p, dst)
                                mirrored += 1
                            else:
                                _prepare_managed_path(ctx, dst)
                    except Exception as error:
                        ctx.error_message = f"Could not mirror {p.name} into bin/: {error}"
                        return False
                if ctx.is_32bit and ctx.install_feeder:
                    prepare_runtime_artifacts(ctx.record, alt_bin / "host64", "dlss5-feed-host64*.png")
                logger.info(f"Mirrored {mirrored} files into bin/ directory.")
        return True


class StepConfigureWineOverrides(PipelineStep[RenoDxContext]):
    @property
    def name(self) -> str:
        return "ConfigureWineOverrides"

    @property
    def description(self) -> str:
        return "Configures Wine/Proton registry DLL overrides for custom runtime hooks"

    def execute(self, ctx: RenoDxContext) -> bool:
        prefix_info = ProtonManager.find_prefix_for_game(ctx.game_exe)
        ctx.record.platform = get_platform_adapter().platform_name

        if prefix_info:
            ctx.record.proton_prefix = str(prefix_info.prefix_path)
            overrides: dict[str, str] = {}
            if ctx.opengl:
                overrides["opengl32"] = "native,builtin"
            elif ctx.reshade_dll_name.lower().startswith("dxgi"):
                overrides["dxgi"] = "native,builtin"
            elif ctx.reshade_dll_name.lower().startswith("d3d9"):
                overrides["d3d9"] = "native,builtin"

            if ctx.d3d9_translate:
                overrides["d3d9"] = "native,builtin"

            injected, originals = ProtonManager.inject_overrides_with_originals(prefix_info, overrides)
            if overrides and set(injected) != set(overrides):
                ctx.error_message = "Could not persist the required Wine DLL overrides."
                return False
            for dll in injected:
                existed, original = originals[dll]
                ctx.record.registry_touched.append(
                    RegistryTouch(
                        reg_path=str(prefix_info.user_reg_path),
                        key=r"Software\Wine\DllOverrides",
                        value_name=dll,
                        original_value=original,
                        original_exists=existed,
                    )
                )
            logger.info(
                f"Configured Proton/Wine DLL overrides in {prefix_info.user_reg_path.name}: {', '.join(injected)}"
            )

        return True
