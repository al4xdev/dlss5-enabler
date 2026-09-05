from collections import deque
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from pathlib import Path

from dlss5_enabler.core.pe import DetectedApi, PeArch, detect_imported_dlls, detect_pe_arch
from dlss5_enabler.core.record import InstallRecord, RecordedFile, record_load
from dlss5_enabler.core.util import sha256_file


class TemporalInput(str, Enum):
    DLSS = "dlss"
    FSR2_PLUS = "fsr2_plus"
    XESS = "xess"


class EvidenceLevel(str, Enum):
    DIRECT_IMPORT = "direct_import"
    DEPENDENCY_IMPORT = "dependency_import"
    MODULE_IMPORT = "module_import"
    FILE_ONLY = "file_only"
    UNKNOWN = "unknown"
    INSTALLER_OWNED = "installer_owned"


class EvidenceOrigin(str, Enum):
    EXISTING = "existing"
    BACKUP = "backup"
    INSTALLER = "installer"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TemporalEvidence:
    feature: TemporalInput
    level: EvidenceLevel
    path: Path
    runtime_name: str
    origin: EvidenceOrigin
    reason: str


@dataclass(frozen=True)
class ApiEvidence:
    api: DetectedApi
    level: EvidenceLevel
    path: Path
    origin: EvidenceOrigin


@dataclass(frozen=True)
class CapabilityLimits:
    max_directories: int = 128
    max_files: int = 512
    max_depth: int = 6
    max_directory_entries: int = 2048
    max_module_bytes: int = 128 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            min(
                self.max_directories,
                self.max_files,
                self.max_depth,
                self.max_directory_entries,
                self.max_module_bytes,
            )
            < 1
        ):
            raise ValueError("Capability scan limits must be positive")


@dataclass(frozen=True)
class GameCapabilities:
    game_exe: Path
    architecture: PeArch
    apis: tuple[DetectedApi, ...]
    api_evidence: tuple[ApiEvidence, ...]
    temporal_evidence: tuple[TemporalEvidence, ...]
    complete: bool
    warnings: tuple[str, ...]

    @property
    def temporal_inputs(self) -> tuple[TemporalInput, ...]:
        if not self.complete:
            return ()
        imported = {
            evidence.feature
            for evidence in self.temporal_evidence
            if evidence.level in {EvidenceLevel.DIRECT_IMPORT, EvidenceLevel.DEPENDENCY_IMPORT}
        }
        return tuple(feature for feature in TemporalInput if feature in imported)

    @property
    def has_reliable_temporal_input(self) -> bool:
        return bool(self.temporal_inputs)


@dataclass(frozen=True)
class _Module:
    path: Path
    source: Path
    origin: EvidenceOrigin
    architecture: PeArch
    imports: tuple[str, ...]


_TEMPORAL_DLLS = {
    "nvngx_dlss.dll": TemporalInput.DLSS,
    "libxess.dll": TemporalInput.XESS,
    "libxess_dx11.dll": TemporalInput.XESS,
    "ffx_fsr2_api_x64.dll": TemporalInput.FSR2_PLUS,
    "ffx_fsr2_api_dx12_x64.dll": TemporalInput.FSR2_PLUS,
    "ffx_fsr2_api_vk_x64.dll": TemporalInput.FSR2_PLUS,
    "ffx_fsr3upscaler_x64.dll": TemporalInput.FSR2_PLUS,
    "amd_fidelityfx_upscaler_dx12.dll": TemporalInput.FSR2_PLUS,
}
_AMBIGUOUS_FSR_DLLS = frozenset({"amd_fidelityfx_dx12.dll", "amd_fidelityfx_vk.dll"})
_GRAPHICS_DLLS = {
    "d3d9.dll": DetectedApi.D3D9,
    "d3d11.dll": DetectedApi.D3D11,
    "d3d12.dll": DetectedApi.D3D12,
    "opengl32.dll": DetectedApi.OPENGL,
    "vulkan-1.dll": DetectedApi.VULKAN,
}
_EXECUTABLE_DIRECTORIES = frozenset({"bin", "binaries", "win32", "win64", "x86", "x64"})
_SCAN_DIRECTORIES = _EXECUTABLE_DIRECTORIES | {"engine", "plugins"}
_PROXY_NAMES = frozenset(_GRAPHICS_DLLS) | {
    "dxgi.dll",
    "d3d10.dll",
    "winmm.dll",
    "version.dll",
    "dbghelp.dll",
    "winhttp.dll",
    "wininet.dll",
    "dinput8.dll",
    "nvngx.dll",
    "nvngx.dll_dlssnr.dll",
    "optiscaler.dll",
}
_DEFAULT_LIMITS = CapabilityLimits()


def _path_key(path: Path) -> str:
    return path.resolve().as_posix().casefold()


def _roots(game_exe: Path) -> tuple[Path, ...]:
    roots = [game_exe.parent]
    for _ in range(3):
        current = roots[-1]
        if current.name.casefold() not in _EXECUTABLE_DIRECTORIES or current.parent == current:
            break
        roots.append(current.parent)
    return tuple(roots)


def _scan_files(roots: tuple[Path, ...], limits: CapabilityLimits, warnings: list[str]) -> tuple[list[Path], bool]:
    pending = deque((root, 0, False) for root in roots)
    visited: set[str] = set()
    files: dict[str, Path] = {}
    complete = True
    for _ in range(limits.max_directories):
        if not pending:
            break
        directory, depth, recursive = pending.popleft()
        key = _path_key(directory)
        if key in visited:
            continue
        visited.add(key)
        try:
            if directory.is_symlink() or not any(directory.resolve().is_relative_to(root) for root in roots):
                continue
            children = list(islice(directory.iterdir(), limits.max_directory_entries + 1))
            if len(children) > limits.max_directory_entries:
                warnings.append(f"Directory entry limit reached: {directory}")
                complete = False
            for child in sorted(children[: limits.max_directory_entries], key=lambda path: path.name.casefold()):
                if child.is_symlink() or not any(child.resolve().is_relative_to(root) for root in roots):
                    continue
                if child.is_dir() and (recursive or child.name.casefold() in _SCAN_DIRECTORIES):
                    if depth < limits.max_depth:
                        pending.append((child, depth + 1, recursive or child.name.casefold() != "engine"))
                    else:
                        warnings.append(f"Directory depth limit reached: {child}")
                        complete = False
                elif child.is_file() and child.suffix.casefold() in {".dll", ".exe"}:
                    child_key = _path_key(child)
                    if child_key in files and files[child_key] != child:
                        warnings.append(f"Ambiguous Windows filename: {child}")
                        complete = False
                    files[child_key] = child
                    if len(files) > limits.max_files:
                        warnings.append("Module scan file limit reached")
                        return list(files.values())[: limits.max_files], False
        except OSError as error:
            warnings.append(f"Could not inspect {directory}: {error}")
            complete = False
    if pending:
        warnings.append("Module scan directory limit reached")
        complete = False
    return list(files.values()), complete


def _managed_files(previous: InstallRecord | None) -> dict[str, list[RecordedFile]]:
    managed: dict[str, list[RecordedFile]] = {}
    if previous is not None:
        for item in previous.files:
            managed.setdefault(_path_key(Path(item.path)), []).append(item)
    return managed


def _baseline_source(path: Path, records: list[RecordedFile]) -> tuple[Path | None, EvidenceOrigin, str]:
    if not records:
        return path, EvidenceOrigin.EXISTING, "Existing file; presence alone does not prove game integration"
    if len(records) != 1:
        return None, EvidenceOrigin.UNKNOWN, "Multiple managed entries describe this destination"
    item = records[0]
    if item.sha256 and path.is_file() and sha256_file(path) != item.sha256:
        return None, EvidenceOrigin.UNKNOWN, "Managed destination differs from its recorded digest"
    if not item.backup:
        return None, EvidenceOrigin.INSTALLER, "Created by the installer; excluded from native game capabilities"
    backup = Path(item.backup)
    if backup.is_symlink() or not backup.is_file():
        return None, EvidenceOrigin.UNKNOWN, "Managed destination has an unavailable or unsafe backup"
    return backup, EvidenceOrigin.BACKUP, "Preexisting backup; its ownership and native integration are unproven"


def _temporal_file(
    path: Path, source: Path | None, origin: EvidenceOrigin, reason: str, architecture: PeArch
) -> TemporalEvidence | None:
    name = path.name.casefold()
    feature = _TEMPORAL_DLLS.get(name)
    if feature is None and name not in _AMBIGUOUS_FSR_DLLS:
        return None
    level = EvidenceLevel.FILE_ONLY
    if origin is EvidenceOrigin.INSTALLER:
        level = EvidenceLevel.INSTALLER_OWNED
    elif origin in {EvidenceOrigin.BACKUP, EvidenceOrigin.UNKNOWN}:
        level = EvidenceLevel.UNKNOWN
    elif source is None or detect_pe_arch(source) not in {architecture, PeArch.X86, PeArch.X64}:
        level = EvidenceLevel.UNKNOWN
        reason = "Runtime is not a recognized x86/x64 PE DLL"
    elif detect_pe_arch(source) != architecture:
        level = EvidenceLevel.UNKNOWN
        reason = "Runtime architecture does not match the game executable"
    if feature is None:
        feature = TemporalInput.FSR2_PLUS
        level = EvidenceLevel.UNKNOWN if origin is not EvidenceOrigin.INSTALLER else level
        reason += "; a shared FidelityFX library does not establish FSR2+ input"
    return TemporalEvidence(feature, level, path, name, origin, reason)


def _check_module_size(path: Path, limits: CapabilityLimits) -> None:
    if path.stat().st_size > limits.max_module_bytes:
        raise ValueError(f"Module size limit reached: {path}")


def _read_module(path: Path, source: Path, origin: EvidenceOrigin, limits: CapabilityLimits) -> _Module:
    _check_module_size(source, limits)
    return _Module(path, source, origin, detect_pe_arch(source), tuple(detect_imported_dlls(source)))


def _dependency_levels(game_exe: Path, modules: list[_Module], architecture: PeArch) -> dict[str, EvidenceLevel]:
    usable = {
        _path_key(module.path): module
        for module in modules
        if module.architecture is architecture and module.origin is EvidenceOrigin.EXISTING
    }
    levels = {_path_key(game_exe): EvidenceLevel.DIRECT_IMPORT}
    pending = deque([_path_key(game_exe)])
    for _ in range(len(modules)):
        if not pending:
            break
        key = pending.popleft()
        module = usable.get(key)
        if module is None:
            continue
        for name in module.imports:
            if (
                name in _PROXY_NAMES
                or name in _TEMPORAL_DLLS
                or any(separator in name for separator in ("/", "\\", ":"))
            ):
                continue
            candidates = [
                candidate
                for candidate in usable.values()
                if candidate.path.name.casefold() == name
                and candidate.path.parent in {module.path.parent, game_exe.parent, game_exe.parent / "bin"}
            ]
            if len(candidates) == 1:
                candidate_key = _path_key(candidates[0].path)
                if candidate_key not in levels:
                    levels[candidate_key] = EvidenceLevel.DEPENDENCY_IMPORT
                    pending.append(candidate_key)
    return levels


def _module_evidence(
    modules: list[_Module],
    levels: dict[str, EvidenceLevel],
    architecture: PeArch,
    temporal: list[TemporalEvidence],
    warnings: list[str],
) -> list[ApiEvidence]:
    apis: list[ApiEvidence] = []
    for module in modules:
        if module.architecture is not architecture or architecture not in {PeArch.X86, PeArch.X64}:
            continue
        level = levels.get(_path_key(module.path), EvidenceLevel.MODULE_IMPORT)
        if module.origin is not EvidenceOrigin.EXISTING:
            level = EvidenceLevel.UNKNOWN
        for name in module.imports:
            api = _GRAPHICS_DLLS.get(name)
            if api is not None:
                apis.append(ApiEvidence(api, level, module.path, module.origin))
            feature = _TEMPORAL_DLLS.get(name)
            if feature is not None:
                temporal.append(
                    TemporalEvidence(
                        feature,
                        level,
                        module.path,
                        name,
                        module.origin,
                        "Static import is integration evidence; runtime support and execution are not verified",
                    )
                )
            if name in _AMBIGUOUS_FSR_DLLS:
                warnings.append(f"Shared FidelityFX import does not prove FSR2+ input: {module.path}")
        if "dxgi.dll" in module.imports and not {"d3d11.dll", "d3d12.dll"}.intersection(module.imports):
            warnings.append(f"DXGI alone does not identify D3D11 or D3D12: {module.path}")
    return apis


def analyze_capabilities(
    game_exe: Path | str,
    previous_record: InstallRecord | None = None,
    *,
    limits: CapabilityLimits = _DEFAULT_LIMITS,
) -> GameCapabilities:
    target = Path(game_exe).resolve()
    warnings: list[str] = []
    architecture = detect_pe_arch(target)
    if not target.is_file() or architecture not in {PeArch.X86, PeArch.X64}:
        return GameCapabilities(target, architecture, (), (), (), False, ("Target is not a supported x86/x64 PE",))
    previous = previous_record if previous_record is not None else record_load(target.parent)
    ownership_valid = True
    if previous is not None and _path_key(Path(previous.game_dir)) != _path_key(target.parent):
        warnings.append("Install record belongs to another game directory")
        previous = None
        ownership_valid = False
    if previous is None and (target.parent / "dlss5-enabler.install.json").exists():
        warnings.append("Existing install record could not establish managed file ownership")
        ownership_valid = False
    roots = _roots(target)
    paths, complete = _scan_files(roots, limits, warnings)
    managed = _managed_files(previous)
    candidates = {_path_key(path): path for path in paths}
    candidates[_path_key(target)] = target
    if previous is not None:
        for item in previous.files:
            path = Path(item.path)
            if path.suffix.casefold() == ".dll" and any(path.resolve().is_relative_to(root) for root in roots):
                if _path_key(path) not in candidates and len(candidates) >= limits.max_files:
                    warnings.append("Managed module scan file limit reached")
                    complete = False
                    break
                candidates.setdefault(_path_key(path), path)
    modules: list[_Module] = []
    temporal: list[TemporalEvidence] = []
    for path in sorted(candidates.values(), key=lambda candidate: candidate.as_posix().casefold()):
        try:
            if path.is_file():
                _check_module_size(path, limits)
            source, origin, reason = _baseline_source(path, managed.get(_path_key(path), []))
            file_evidence = _temporal_file(path, source, origin, reason, architecture)
            if file_evidence is not None:
                temporal.append(file_evidence)
            if source is not None:
                modules.append(_read_module(path, source, origin, limits))
            elif origin is EvidenceOrigin.UNKNOWN:
                warnings.append(f"{reason}: {path}")
                complete = False
        except (OSError, ValueError) as error:
            warnings.append(f"Could not inspect module {path}: {error}")
            complete = False
    levels = _dependency_levels(target, modules, architecture)
    api_evidence = _module_evidence(modules, levels, architecture, temporal, warnings)
    apis = tuple(api for api in DetectedApi if any(evidence.api is api for evidence in api_evidence))
    return GameCapabilities(
        target, architecture, apis, tuple(api_evidence), tuple(temporal), complete and ownership_valid, tuple(warnings)
    )
