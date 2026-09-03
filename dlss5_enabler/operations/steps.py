import tempfile
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from dlss5_enabler.core.fileio import atomic_copy_file, resource_lock, unique_backup_path
from dlss5_enabler.core.ini import ini_get_exact, ini_set_exact
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.core.pe import PeArch, check_api_mismatches, detect_game_apis, detect_native_dlss, detect_pe_arch
from dlss5_enabler.core.record import (
    IniTouch,
    InstallOptions,
    InstallRecord,
    RecordedFile,
    RegistryTouch,
    index_add,
    record_exists,
    record_load,
    record_save,
)
from dlss5_enabler.core.util import (
    file_is_writable,
    get_cache_dir,
    get_permission_guidance,
    is_directory_writable,
    sha256_file,
    unblock_file,
)
from dlss5_enabler.core.version import get_tool_version
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
from dlss5_enabler.operations.pipeline import PipelineContext, PipelineStep
from dlss5_enabler.operations.reshade import (
    ensure_mv_provider_def,
    extract_reshade_dlls_from_installer,
    normalize_search_paths,
    reshade_headless_install,
)
from dlss5_enabler.operations.uninstall import (
    capture_install_snapshot,
    cleanup_install_snapshot,
    restore_install_snapshot,
    run_uninstall,
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


def _prepare_managed_path(ctx: PipelineContext, dst: Path) -> RecordedFile:
    with resource_lock(dst):
        existing = next((item for item in ctx.record.files if Path(item.path).resolve() == dst.resolve()), None)
        if existing is not None:
            return existing
        backup_str = ""
        if dst.exists():
            backup = unique_backup_path(dst)
            atomic_copy_file(dst, backup)
            backup_str = str(backup)
            logger.info(f"Backed up existing file: {dst.name} -> {backup.name}")
        item = RecordedFile(path=str(dst), backup=backup_str)
        ctx.record.files.append(item)
        return item


def _place_file(ctx: PipelineContext, src: Path, dst: Path) -> None:
    item = _prepare_managed_path(ctx, dst)
    atomic_copy_file(src, dst)
    unblock_file(dst)
    item.size_bytes = dst.stat().st_size
    item.sha256 = sha256_file(dst)
    logger.debug(f"Placed file: {dst}")


def _prepare_reshade_runtime_artifacts(ctx: PipelineContext) -> None:
    directories = [ctx.reshade_dir]
    if ctx.is_32bit:
        directories.append(ctx.reshade_dir / "host64")
    for directory in directories:
        for name in _RESHADER_RUNTIME_ARTIFACTS:
            _prepare_managed_path(ctx, directory / name)


def _relocate_redirected_d3d9_reshade(ctx: PipelineContext, game_ini: Path) -> bool:
    if not ctx.d3d9_translate or ctx.reshade_dir == ctx.game_dir:
        return True
    redirected_dir = ctx.reshade_dir
    redirected_dll = redirected_dir / ctx.reshade_dll_name
    redirected_ini = redirected_dir / "ReShade.ini"
    if not redirected_dll.is_file():
        ctx.error_message = f"ReShade redirect did not create {ctx.reshade_dll_name}."
        return False
    _place_file(ctx, redirected_dll, ctx.game_dir / ctx.reshade_dll_name)
    if redirected_ini.is_file():
        _place_file(ctx, redirected_ini, game_ini)
    try:
        redirected_dll.unlink()
        redirected_ini.unlink(missing_ok=True)
        (redirected_dir / "ReShadePreset.ini").unlink(missing_ok=True)
    except OSError as error:
        ctx.error_message = f"Could not relocate ReShade output from {redirected_dir.name}: {error}"
        return False
    ctx.reshade_dir = ctx.game_dir
    logger.info("Moved redirected ReShade output into the game directory for D3D9 mirroring.")
    return True


class StepValidateTarget(PipelineStep):
    @property
    def name(self) -> str:
        return "ValidateTarget"

    @property
    def description(self) -> str:
        return "Validates game executable architecture, permissions, and environment"

    def execute(self, ctx: PipelineContext) -> bool:
        ctx.game_exe = ctx.game_exe.resolve()
        if (ctx.d3d9_translate and ctx.opengl) or not ctx.game_exe.is_file():
            ctx.error_message = (
                "D3D9 translation and OpenGL mode cannot be enabled together."
                if ctx.d3d9_translate and ctx.opengl
                else f"Game executable not found: {ctx.game_exe}"
            )
            return False

        ctx.game_dir = ctx.game_exe.parent
        ctx.reshade_dir = ctx.game_dir
        ctx.pe_arch = detect_pe_arch(ctx.game_exe)
        ctx.is_32bit = ctx.pe_arch == PeArch.X86
        ctx.reshade_api = "opengl" if ctx.opengl else "dxgi"
        ctx.reshade_dll_name = "opengl32.dll" if ctx.opengl else "dxgi.dll"

        previous = record_load(ctx.game_dir) if record_exists(ctx.game_dir) else None
        previous_native_dlss = bool(
            previous
            and any(Path(item.path).name.lower() == "nvngx_dlss.dll" and bool(item.backup) for item in previous.files)
        )
        managed_dlss_only = bool(
            previous
            and any(Path(item.path).name.lower() == "nvngx_dlss.dll" and not item.backup for item in previous.files)
        )
        ctx.native_dlss_detected = previous_native_dlss or (not managed_dlss_only and detect_native_dlss(ctx.game_exe))
        ctx.install_feeder = not ctx.native_dlss_detected
        if ctx.pe_arch in (PeArch.UNKNOWN, PeArch.ARM64) or (ctx.native_dlss_detected and ctx.is_32bit):
            ctx.error_message = (
                "Native DLSS was detected, but direct RenoDX installation requires a 64-bit game."
                if ctx.native_dlss_detected and ctx.is_32bit
                else f"Unsupported architecture ({ctx.pe_arch.value}). Supported: x86 (32-bit), x64 (64-bit)."
            )
            return False
        if ctx.native_dlss_detected:
            logger.info("Native DLSS detected; installing RenoDX directly without DLSS5-Feeder.")
            console.print(
                "[bold cyan]Native DLSS detected.[/bold cyan] Skipping DLSS5-Feeder to avoid a duplicate DLSS evaluate."
            )

        existing_proxy = ctx.game_dir / ctx.reshade_dll_name
        if existing_proxy.is_file() and not (ctx.game_dir / "ReShade.ini").is_file():
            ctx.error_message = (
                f"Existing {ctx.reshade_dll_name} has no ReShade.ini; refusing to assume it is ReShade. "
                "Remove or rename it before installing."
            )
            return False

        game_is_running = get_platform_adapter().is_game_running(ctx.game_exe)
        if game_is_running or not file_is_writable(ctx.game_exe):
            ctx.error_message = (
                f"Game is currently running: {ctx.game_exe.name}. Close it before installing."
                if game_is_running
                else f"Game executable is locked (game is currently running): {ctx.game_exe.name}"
            )
            return False

        if not is_directory_writable(ctx.game_dir):
            guidance = get_permission_guidance(ctx.game_dir)
            console.print(
                Panel(
                    f"[bold red]Permission Denied[/bold red]\n"
                    f"DLSS5 Enabler cannot write files to the game directory:\n"
                    f"  [yellow]{ctx.game_dir}[/yellow]\n\n"
                    f"{guidance}",
                    title="Write-Protected Directory",
                    border_style="red",
                )
            )
            ctx.error_message = (
                f"Game directory '{ctx.game_dir.name}' is write-protected. Apply suggested permissions and re-run."
            )
            return False

        detected_apis = detect_game_apis(ctx.game_exe)
        if detected_apis:
            logger.info(f"Detected Graphics APIs: {', '.join(a.value for a in detected_apis)}")

        warnings = check_api_mismatches(
            ctx.game_exe,
            d3d9=ctx.d3d9_translate,
            opengl=ctx.opengl,
            vulkan_layer=ctx.install_vulkan_layer,
        )
        for w in warnings:
            logger.warning(f"API Heuristic Warning: {w}")
            console.print(f"[bold yellow][WARNING] {w}[/bold yellow]")

        install_type = "D3D9 (dgVoodoo2)" if ctx.d3d9_translate else ("OpenGL" if ctx.opengl else "D3D11/D3D12")
        if ctx.install_vulkan_layer:
            install_type += " + Vulkan Layer"

        ctx.record = InstallRecord(
            tool_version=get_tool_version(),
            game_exe=str(ctx.game_exe),
            game_dir=str(ctx.game_dir),
            architecture="x86" if ctx.is_32bit else "x64",
            is_32bit=ctx.is_32bit,
            install_type=install_type,
            d3d9_translate=ctx.d3d9_translate,
            opengl=ctx.opengl,
            vulkan_layer=ctx.install_vulkan_layer,
            lumenite_installed=ctx.install_lumenite,
            native_dlss_detected=ctx.native_dlss_detected,
            install_options=InstallOptions(
                lumenite=ctx.install_lumenite,
                d3d9=ctx.d3d9_translate,
                opengl=ctx.opengl,
                vulkan_layer=ctx.install_vulkan_layer,
            ),
            platform=get_platform_adapter().platform_name,
        )

        logger.info(
            f"Target validated: {ctx.game_exe.name} [{ctx.pe_arch.value}] in {ctx.game_dir}; "
            f"native_dlss={ctx.native_dlss_detected}"
        )
        return True


class StepCleanPreviousInstall(PipelineStep):
    @property
    def name(self) -> str:
        return "CleanPreviousInstall"

    @property
    def description(self) -> str:
        return "Checks and cleanly uninstalls prior DLSS5 Enabler installation if refreshing"

    def execute(self, ctx: PipelineContext) -> bool:
        if record_exists(ctx.game_dir):
            logger.info("Existing installation record found. Performing clean pre-uninstall...")
            previous = record_load(ctx.game_dir)
            if previous is None:
                ctx.error_message = "Existing installation record is unreadable; refusing to overwrite it."
                return False
            ctx.previous_install_snapshot = capture_install_snapshot(previous)
            un_ok = run_uninstall(ctx.game_dir, log=logger.info, lock_operation=False)
            if not un_ok:
                ctx.error_message = "Failed to cleanly remove previous installation prior to refresh."
                return False
        return True

    def rollback(self, ctx: PipelineContext) -> None:
        if ctx.previous_install_snapshot is not None:
            if not restore_install_snapshot(ctx.previous_install_snapshot):
                raise RuntimeError("Could not restore the previous DLSS5 Enabler installation snapshot")
            ctx.previous_install_snapshot = None

    def commit(self, ctx: PipelineContext) -> None:
        if ctx.previous_install_snapshot is not None:
            cleanup_install_snapshot(ctx.previous_install_snapshot)
            ctx.previous_install_snapshot = None


class StepFetchUpstream(PipelineStep):
    @property
    def name(self) -> str:
        return "FetchUpstream"

    @property
    def description(self) -> str:
        return "Fetches/validates required upstream components from GitHub & ReShade"

    def execute(self, ctx: PipelineContext) -> bool:
        dxgi = ctx.game_dir / ctx.reshade_dll_name
        previous = record_load(ctx.game_dir) if record_exists(ctx.game_dir) else None
        ctx.need_reshade = not dxgi.is_file() or (previous is not None and previous.reshade_by_us)

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


class StepInstallReShade(PipelineStep):
    @property
    def name(self) -> str:
        return "InstallReShade"

    @property
    def description(self) -> str:
        return "Installs ReShade with Addon support via headless setup and normalizes paths"

    def execute(self, ctx: PipelineContext) -> bool:
        game_ini = ctx.game_dir / "ReShade.ini"

        if ctx.need_reshade and ctx.reshade_bundle and ctx.reshade_bundle.setup_exe_path:
            primary_dll = ctx.game_dir / ctx.reshade_dll_name
            _prepare_managed_path(ctx, primary_dll)
            if game_ini.is_file():
                had_base, original_base = ini_get_exact(game_ini, "INSTALL", "BasePath")
                if had_base and original_base.strip():
                    base_path = Path(original_base.strip())
                    redirect_hint = base_path if base_path.is_absolute() else ctx.game_dir / base_path
                    if redirect_hint.is_dir():
                        _prepare_managed_path(ctx, redirect_hint / ctx.reshade_dll_name)
                        _prepare_managed_path(ctx, redirect_hint / "ReShade.ini")
                _prepare_managed_path(ctx, game_ini)
                game_ini.unlink()
            else:
                _prepare_managed_path(ctx, game_ini)

            _prepare_reshade_runtime_artifacts(ctx)
            if not reshade_headless_install(ctx.reshade_bundle.setup_exe_path, ctx.game_exe, ctx.reshade_api):
                logger.info("ReShade unattended setup failed; attempting in-process extraction fallback...")
                with tempfile.TemporaryDirectory(prefix="dlss5-enabler-reshade-", dir=get_cache_dir()) as stage_name:
                    stage_dir = Path(stage_name)
                    dlls = extract_reshade_dlls_from_installer(ctx.reshade_bundle.setup_exe_path, stage_dir)
                    bitness_key = "reshade32.dll" if ctx.is_32bit else "reshade64.dll"
                    if bitness_key not in dlls:
                        ctx.error_message = "ReShade unattended setup failed and direct extraction could not find DLL."
                        return False
                    _place_file(ctx, dlls[bitness_key], primary_dll)
                if not game_ini.is_file():
                    effect_ok = ini_set_exact(game_ini, "GENERAL", "EffectSearchPaths", "./reshade-shaders/Shaders/**")
                    texture_ok = ini_set_exact(
                        game_ini, "GENERAL", "TextureSearchPaths", "./reshade-shaders/Textures/**"
                    )
                    if not effect_ok or not texture_ok:
                        ctx.error_message = "Could not create the ReShade configuration."
                        return False

            reshade_dll = ctx.game_dir / ctx.reshade_dll_name
            if not reshade_dll.is_file() and game_ini.is_file():
                had, base = ini_get_exact(game_ini, "INSTALL", "BasePath")
                if had and base.strip():
                    base_path = Path(base.strip())
                    redirected = base_path if base_path.is_absolute() else ctx.game_dir / base_path
                    if redirected.is_dir():
                        ctx.reshade_dir = redirected.resolve()
                        logger.info(f"ReShade redirected install to: {ctx.reshade_dir}")

            if not _relocate_redirected_d3d9_reshade(ctx, game_ini):
                return False

            ctx.record.reshade_by_us = True
            ctx.record.reshade_dir = str(ctx.reshade_dir)

            target_dll = ctx.reshade_dir / ctx.reshade_dll_name
            if not target_dll.is_file():
                ctx.error_message = f"ReShade setup completed without creating {ctx.reshade_dll_name}."
                return False
            managed_dll = _prepare_managed_path(ctx, target_dll)
            managed_dll.size_bytes = target_dll.stat().st_size
            managed_dll.sha256 = sha256_file(target_dll)
            if ctx.reshade_dir != ctx.game_dir:
                redirected_ini = ctx.reshade_dir / "ReShade.ini"
                managed_ini = _prepare_managed_path(ctx, redirected_ini)
                if redirected_ini.is_file():
                    managed_ini.size_bytes = redirected_ini.stat().st_size
                    managed_ini.sha256 = sha256_file(redirected_ini)
        else:
            ctx.record.reshade_dir = str(ctx.reshade_dir)
            logger.info("Existing ReShade installation detected and preserved.")

        if not normalize_search_paths(ctx.reshade_dir / "ReShade.ini", ctx.record):
            ctx.error_message = "Could not normalize the ReShade search paths."
            return False
        return True


class StepInstallD3D9Translation(PipelineStep):
    @property
    def name(self) -> str:
        return "InstallD3D9Translation"

    @property
    def description(self) -> str:
        return "Configures dgVoodoo2 D3D9->D3D11 translation layer"

    def execute(self, ctx: PipelineContext) -> bool:
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
            ("DirectX", "dgVoodooWatermark", "true"),
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


class StepInjectFeederAndHeaders(PipelineStep):
    @property
    def name(self) -> str:
        return "InjectFeederAndHeaders"

    @property
    def description(self) -> str:
        return "Installs shader headers and DLSS5-Feeder only when the game has no native DLSS"

    def execute(self, ctx: PipelineContext) -> bool:
        if not ctx.headers_bundle:
            if not ctx.install_feeder and not ctx.install_lumenite:
                return True
            ctx.error_message = "ReShade headers bundle missing."
            return False

        shaders_dir = ctx.reshade_dir / "reshade-shaders" / "Shaders"
        if ctx.install_feeder:
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


class StepInjectRenoDxAndNgx(PipelineStep):
    @property
    def name(self) -> str:
        return "InjectRenoDxAndNgx"

    @property
    def description(self) -> str:
        return "Installs RenoDX DLSS 5 add-on and NVIDIA DLSS NR/SR binaries"

    def execute(self, ctx: PipelineContext) -> bool:
        if not ctx.renodx_bundle or not ctx.ngx_bundle or (ctx.install_feeder and not ctx.feeder_bundle):
            ctx.error_message = "RenoDX or NGX bundle missing."
            return False

        if ctx.is_32bit:
            if ctx.feeder_bundle is None:
                ctx.error_message = "Feeder bundle missing for the 32-bit host."
                return False
            host_dir = ctx.reshade_dir / "host64"
            host_dir.mkdir(parents=True, exist_ok=True)
            if ctx.feeder_bundle.host64_exe:
                _place_file(ctx, ctx.feeder_bundle.host64_exe, host_dir / "dlss5-feed-host64.exe")

            host_dxgi = host_dir / ctx.reshade_dll_name
            if not host_dxgi.is_file() and ctx.reshade_bundle and ctx.reshade_bundle.setup_exe_path:
                host_ini = host_dir / "ReShade.ini"
                managed_host_dll = _prepare_managed_path(ctx, host_dxgi)
                _prepare_managed_path(ctx, host_ini)
                installed = reshade_headless_install(
                    ctx.reshade_bundle.setup_exe_path, host_dir / "dlss5-feed-host64.exe", ctx.reshade_api
                )
                if not installed or not host_dxgi.is_file():
                    ctx.error_message = f"Host ReShade setup did not create {ctx.reshade_dll_name}."
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


class StepConfigureMotionVectors(PipelineStep):
    @property
    def name(self) -> str:
        return "ConfigureMotionVectors"

    @property
    def description(self) -> str:
        return "Installs optional LumeniteFX motion vectors and configures DLSS5-Feeder when used"

    def execute(self, ctx: PipelineContext) -> bool:
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


class StepInstallVulkanLayer(PipelineStep):
    @property
    def name(self) -> str:
        return "InstallVulkanLayer"

    @property
    def description(self) -> str:
        return "Extracts Vulkan layer fallback binaries if requested"

    def execute(self, ctx: PipelineContext) -> bool:
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


class StepMirrorDualLocations(PipelineStep):
    @property
    def name(self) -> str:
        return "MirrorDualLocations"

    @property
    def description(self) -> str:
        return "Mirrors placed files into bin/ subfolder for Source engine D3D9 games"

    def execute(self, ctx: PipelineContext) -> bool:
        if ctx.d3d9_translate:
            alt_bin = ctx.game_dir / "bin"
            if alt_bin.is_dir() and alt_bin != ctx.reshade_dir:
                mirrored = 0
                for recorded in list(ctx.record.files):
                    p = Path(recorded.path)
                    try:
                        if p.is_file() and p.is_relative_to(ctx.reshade_dir):
                            rel = p.relative_to(ctx.reshade_dir)
                            dst = alt_bin / rel
                            _place_file(ctx, p, dst)
                            mirrored += 1
                    except Exception as error:
                        ctx.error_message = f"Could not mirror {p.name} into bin/: {error}"
                        return False
                logger.info(f"Mirrored {mirrored} files into bin/ directory.")
        return True


class StepConfigureWineOverrides(PipelineStep):
    @property
    def name(self) -> str:
        return "ConfigureWineOverrides"

    @property
    def description(self) -> str:
        return "Configures Wine/Proton registry DLL overrides for custom runtime hooks"

    def execute(self, ctx: PipelineContext) -> bool:
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


class StepSaveRecord(PipelineStep):
    @property
    def name(self) -> str:
        return "SaveRecord"

    @property
    def description(self) -> str:
        return "Persists dlss5-enabler.install.json and registers the game in the global install index"

    def execute(self, ctx: PipelineContext) -> bool:
        rec_ok = record_save(ctx.record)
        if not rec_ok:
            ctx.error_message = "Could not save the per-game install record."
            return False
        idx_ok = index_add(ctx.record)
        if not idx_ok:
            record_path = ctx.record.record_path()
            with resource_lock(record_path):
                record_path.unlink(missing_ok=True)
            ctx.error_message = "Could not update the global install index."
            return False
        logger.info(f"Install record saved: {ctx.record.record_path()} ({len(ctx.record.files)} files recorded)")
        return True
