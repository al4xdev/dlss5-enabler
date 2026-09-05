import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

from dlss5_enabler.core.archive import safe_archive_destination
from dlss5_enabler.core.fileio import _atomic_copy_file_unlocked, atomic_write_bytes
from dlss5_enabler.core.ini import ini_set_exact
from dlss5_enabler.core.mutations import managed_file_lock, prepare_managed_path
from dlss5_enabler.core.pe import DetectedApi, PeArch
from dlss5_enabler.core.record import OptiScalerStrategyOptions
from dlss5_enabler.core.util import get_cache_dir, sha256_file, unblock_file
from dlss5_enabler.network.sources import fetch_ngx_dlls, fetch_optiscaler
from dlss5_enabler.operations.contexts import OptiScalerContext
from dlss5_enabler.operations.pipeline import PipelineRunner, PipelineStep
from dlss5_enabler.operations.steps_common import StepCleanPreviousInstall, StepSaveRecord, StepValidateTarget
from dlss5_enabler.platform import get_platform_adapter

_REQUIRED_MEMBERS = frozenset({"optiscaler.dll", "optiscaler.ini", "nvngx.dll_dlssnr.dll"})
_SKIPPED_MEMBERS = frozenset({"setup_windows.bat", "setup_linux.sh"})
_MAX_ARCHIVE_FILES = 2048
_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024


def _archive_members(archive: zipfile.ZipFile, stage: Path) -> dict[str, zipfile.ZipInfo]:
    selected: dict[str, zipfile.ZipInfo] = {}
    destinations: set[str] = set()
    total_size = 0
    for info in archive.infolist():
        normalized = info.filename.replace("\\", "/")
        if PureWindowsPath(normalized).drive or stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"Unsafe OptiScaler archive member: {info.filename}")
        target = safe_archive_destination(stage, normalized)
        canonical = target.as_posix().casefold()
        if canonical in destinations:
            raise ValueError(f"OptiScaler archive contains a path collision: {info.filename}")
        destinations.add(canonical)
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise ValueError(f"Encrypted OptiScaler archive member is unsupported: {info.filename}")
        total_size += info.file_size
        if len(selected) >= _MAX_ARCHIVE_FILES or total_size > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("OptiScaler archive exceeds safe extraction limits")
        relative = target.relative_to(stage).as_posix()
        if relative.casefold() not in _SKIPPED_MEMBERS:
            selected[relative] = info
    basenames = {Path(name).name.casefold() for name in selected}
    missing = _REQUIRED_MEMBERS - basenames
    if missing:
        raise ValueError(f"OptiScaler archive is missing required members: {', '.join(sorted(missing))}")
    return selected


def _extract_archive(archive_path: Path, destination: Path) -> dict[str, Path]:
    with zipfile.ZipFile(archive_path, "r") as archive:
        selected = _archive_members(archive, destination)
        results: dict[str, Path] = {}
        for relative, info in selected.items():
            target = safe_archive_destination(destination, relative)
            atomic_write_bytes(target, archive.read(info))
            results[relative] = target
    return results


def _find_unique(files: dict[str, Path], name: str) -> tuple[str, Path]:
    matches = tuple((relative, path) for relative, path in files.items() if path.name.casefold() == name.casefold())
    if len(matches) != 1:
        raise ValueError(f"OptiScaler archive must contain exactly one {name}")
    return matches[0]


def _install_destinations(ctx: OptiScalerContext) -> dict[str, Path]:
    dll_relative, _dll_path = _find_unique(ctx.staged_files, "OptiScaler.dll")
    _find_unique(ctx.staged_files, "OptiScaler.ini")
    _find_unique(ctx.staged_files, "nvngx.dll_dlssnr.dll")
    destinations = {
        relative: ctx.game_dir / ctx.proxy_name if relative == dll_relative else ctx.game_dir / Path(relative)
        for relative in ctx.staged_files
    }
    destinations["<nvidia-nr-runtime>"] = ctx.game_dir / "nvngx_dlssnr.dll"
    canonical = [path.resolve() for path in destinations.values()]
    if len(canonical) != len(set(canonical)):
        raise ValueError("OptiScaler sources collide after mapping files into the game directory")
    return destinations


class StepConfigureOptiScaler(PipelineStep[OptiScalerContext]):
    @property
    def name(self) -> str:
        return "ConfigureOptiScaler"

    @property
    def description(self) -> str:
        return "Validates native DLSS and selects the OptiScaler proxy"

    def execute(self, ctx: OptiScalerContext) -> bool:
        analysis = ctx.analysis
        error = ""
        if analysis is None:
            error = "Target analysis is required before selecting OptiScaler."
        elif get_platform_adapter().platform_name != "windows":
            error = "OptiScaler strategy currently supports Windows only."
        elif analysis.architecture is not PeArch.X64:
            error = "OptiScaler strategy currently requires an x64 game."
        elif not analysis.native_dlss:
            error = "OptiScaler strategy requires native DLSS input."
        elif not set(analysis.apis).intersection({DetectedApi.D3D11, DetectedApi.D3D12}):
            error = "OptiScaler strategy currently supports DirectX 11 or DirectX 12 only."
        elif ctx.proxy_name.casefold() not in {"dxgi.dll", "winmm.dll", "version.dll", "winhttp.dll"}:
            error = f"Unsupported OptiScaler proxy name: {ctx.proxy_name}"
        elif not 1 <= ctx.nr_passes <= 5:
            error = "OptiScaler NR passes must be between 1 and 5."
        else:
            reserved = next(
                (
                    path
                    for path in (ctx.game_dir / "dlssnr-capture", ctx.game_dir / "dlssnr-capture.trigger")
                    if path.exists()
                ),
                None,
            )
            if reserved is not None:
                error = f"OptiScaler may delete reserved capture path; move it before installing: {reserved}"
        if error:
            ctx.error_message = error
            return False
        return True


class StepFetchOptiScaler(PipelineStep[OptiScalerContext]):
    @property
    def name(self) -> str:
        return "FetchOptiScaler"

    @property
    def description(self) -> str:
        return "Resolves and validates the OptiScaler and NVIDIA NR artifacts"

    def execute(self, ctx: OptiScalerContext) -> bool:
        ctx.bundle = fetch_optiscaler(
            lambda _message: None,
            force=ctx.force_download,
            archive_path=ctx.archive_path,
            source_revision=ctx.source_revision,
        )
        ctx.ngx_bundle = fetch_ngx_dlls(lambda _message: None, force=ctx.force_download, include_sr=False)
        ctx.record.binaries.update(ctx.bundle.binaries)
        ctx.record.binaries.update(ctx.ngx_bundle.binaries)
        ctx.upstream_warnings.extend(ctx.bundle.warnings)
        ctx.upstream_warnings.extend(ctx.ngx_bundle.warnings)
        return True


class StepPrepareOptiScaler(PipelineStep[OptiScalerContext]):
    @property
    def name(self) -> str:
        return "PrepareOptiScaler"

    @property
    def description(self) -> str:
        return "Extracts and configures OptiScaler in isolated staging"

    def execute(self, ctx: OptiScalerContext) -> bool:
        if ctx.bundle is None or ctx.bundle.archive_path is None or ctx.ngx_bundle is None:
            raise ValueError("OptiScaler and NVIDIA NR bundles are required")
        if ctx.ngx_bundle.nr_dll_path is None or not ctx.ngx_bundle.nr_dll_path.is_file():
            raise ValueError("NVIDIA Neural Rendering runtime is missing")
        ctx.staging_directory = Path(tempfile.mkdtemp(prefix="dlss5-enabler-optiscaler-", dir=get_cache_dir()))
        ctx.staged_files = _extract_archive(ctx.bundle.archive_path, ctx.staging_directory)
        ini_relative, ini_path = _find_unique(ctx.staged_files, "OptiScaler.ini")
        settings = (
            ("Upscalers", "Dx11Upscaler", "dlss_12"),
            ("Upscalers", "Dx12Upscaler", "dlss"),
            ("DlssNr", "Enabled", "true"),
            ("DlssNr", "Passes", str(ctx.nr_passes)),
            ("DlssNr", "AutoCapture", "false"),
            ("FrameGen", "Enabled", "false"),
            ("Menu", "ShortcutKey", "0x2E"),
            ("Log", "LogToFile", "true"),
            ("Log", "LogFileName", "OptiScaler.log"),
            ("Plugins", "LoadReshade", "false"),
            ("Plugins", "LoadSpecialK", "false"),
            ("Inputs", "EnableDlssInputs", "true"),
            ("Inputs", "EnableXeSSInputs", "false"),
            ("Inputs", "EnableFsr2Inputs", "false"),
            ("Inputs", "EnableFsr3Inputs", "false"),
            ("Inputs", "EnableFfxInputs", "false"),
            ("Spoofing", "Dxgi", "false"),
            ("Hotfix", "CheckForUpdate", "false"),
        )
        if any(not ini_set_exact(ini_path, section, key, value) for section, key, value in settings):
            raise ValueError("Could not configure staged OptiScaler.ini")
        ctx.staged_files[ini_relative] = ini_path
        previous = ctx.analysis.previous_record if ctx.analysis is not None else None
        previous_paths: set[Path] = (
            {Path(item.path).resolve() for item in previous.files} if previous is not None else set()
        )
        destinations = _install_destinations(ctx)
        for relative in ctx.staged_files:
            destination = destinations[relative]
            if destination.exists() and destination.resolve() not in previous_paths:
                raise ValueError(f"OptiScaler destination is occupied by an unmanaged path: {destination}")
        nr_destination = destinations["<nvidia-nr-runtime>"]
        if nr_destination.exists() and nr_destination.resolve() not in previous_paths:
            raise ValueError(f"OptiScaler destination is occupied by an unmanaged path: {nr_destination}")
        return True

    def rollback(self, ctx: OptiScalerContext) -> None:
        self.cleanup(ctx)

    def cleanup(self, ctx: OptiScalerContext) -> None:
        if ctx.staging_directory is not None:
            stage = ctx.staging_directory.resolve()
            if stage.parent != get_cache_dir().resolve() or not stage.name.startswith("dlss5-enabler-optiscaler-"):
                raise ValueError(f"Invalid OptiScaler staging directory: {stage}")
            shutil.rmtree(stage)
            ctx.staging_directory = None
            ctx.staged_files.clear()


class StepInstallOptiScaler(PipelineStep[OptiScalerContext]):
    @property
    def name(self) -> str:
        return "InstallOptiScaler"

    @property
    def description(self) -> str:
        return "Installs the staged OptiScaler files and records every mutation"

    def execute(self, ctx: OptiScalerContext) -> bool:
        if ctx.bundle is None or ctx.ngx_bundle is None or ctx.ngx_bundle.nr_dll_path is None:
            raise ValueError("Prepared OptiScaler artifacts are required")
        destinations = _install_destinations(ctx)
        for relative, source in ctx.staged_files.items():
            destination = destinations[relative]
            with managed_file_lock(ctx.record, destination) as item:
                _atomic_copy_file_unlocked(source, destination)
                unblock_file(destination)
                item.size_bytes = destination.stat().st_size
                item.sha256 = sha256_file(destination)
        nr_destination = destinations["<nvidia-nr-runtime>"]
        with managed_file_lock(ctx.record, nr_destination) as item:
            _atomic_copy_file_unlocked(ctx.ngx_bundle.nr_dll_path, nr_destination)
            unblock_file(nr_destination)
            item.size_bytes = nr_destination.stat().st_size
            item.sha256 = sha256_file(nr_destination)
        prepare_managed_path(ctx.record, ctx.game_dir / "OptiScaler.log")
        ctx.record.install_type = "OptiScaler / native DLSS"
        ctx.record.native_dlss_detected = True
        ctx.record.strategy_options = OptiScalerStrategyOptions(
            variant="y4my4my4m-v3",
            proxy_name=ctx.proxy_name,
            nr_passes=ctx.nr_passes,
            source_revision=ctx.bundle.source_revision,
        )
        return True


def build_optiscaler_pipeline() -> PipelineRunner[OptiScalerContext]:
    return PipelineRunner(
        (
            StepValidateTarget(),
            StepConfigureOptiScaler(),
            StepFetchOptiScaler(),
            StepPrepareOptiScaler(),
            StepCleanPreviousInstall(),
            StepInstallOptiScaler(),
            StepSaveRecord(),
        ),
        name="OptiScaler Installation Pipeline",
    )
