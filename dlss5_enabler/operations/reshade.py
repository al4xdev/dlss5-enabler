import io
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

import py7zr

from dlss5_enabler.core.archive import safe_archive_destination
from dlss5_enabler.core.fileio import atomic_write_bytes
from dlss5_enabler.core.ini import ini_get_exact_unlocked as ini_get_exact
from dlss5_enabler.core.ini import ini_set_exact_unlocked as ini_set_exact
from dlss5_enabler.core.logger import get_logger
from dlss5_enabler.core.mutations import managed_file_lock
from dlss5_enabler.core.record import IniTouch, InstallRecord

logger = get_logger("reshade")


def _normalize_search_path_list(value: str, default: str) -> str:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in value.split(","):
        path = raw_path.strip().replace("/", "\\")
        while "**\\**" in path:
            path = path.replace("**\\**", "**")
        if path in {"", ".", ".\\"}:
            continue
        key = path.lower()
        if key not in seen:
            normalized.append(path)
            seen.add(key)
    return ",".join(normalized) if normalized else default


_RUNTIME_NAMES = frozenset({"reshade64.dll", "reshade32.dll", "reshade64.json", "reshade32.json"})


def _runtime_members(stage: Path, members: list[tuple[str, bool, bool]]) -> dict[str, str]:
    destinations: set[str] = set()
    selected: dict[str, str] = {}
    for name, is_directory, is_symlink in members:
        normalized = name.replace("\\", "/")
        if PureWindowsPath(normalized).drive or is_symlink:
            raise ValueError(f"Unsafe embedded archive member: {name}")
        target = safe_archive_destination(stage, normalized)
        canonical = target.as_posix().casefold()
        if canonical in destinations:
            raise ValueError(f"Duplicate embedded archive member: {name}")
        destinations.add(canonical)
        if is_directory:
            continue
        runtime_name = target.name.casefold()
        if runtime_name in _RUNTIME_NAMES:
            if runtime_name in selected:
                raise ValueError(f"Ambiguous ReShade runtime member: {name}")
            selected[runtime_name] = name
    if not any(name.endswith(".dll") for name in selected):
        raise ValueError("Embedded archive contains no ReShade32.dll or ReShade64.dll.")
    return selected


def _extract_zip_runtime(data: bytes, stage: Path) -> dict[str, Path] | None:
    for match in re.finditer(re.escape(b"PK\x03\x04"), data):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data[match.start() :]))
        except zipfile.BadZipFile:
            continue
        with archive:
            members = archive.infolist()
            if not any(Path(item.filename.replace("\\", "/")).name.casefold() in _RUNTIME_NAMES for item in members):
                continue
            selected = _runtime_members(
                stage,
                [(item.filename, item.is_dir(), stat.S_ISLNK(item.external_attr >> 16)) for item in members],
            )
            results: dict[str, Path] = {}
            for name, member in selected.items():
                target = safe_archive_destination(stage, name)
                atomic_write_bytes(target, archive.read(member))
                results[name] = target
            return results
    return None


def _extract_7z_runtime(setup_exe: Path, stage: Path) -> dict[str, Path]:
    with py7zr.SevenZipFile(setup_exe, mode="r") as archive:
        selected = _runtime_members(
            stage,
            [(item.filename, item.is_directory, getattr(item, "is_symlink", False) is True) for item in archive.list()],
        )
        archive.extract(path=stage, targets=list(selected.values()))
    results: dict[str, Path] = {}
    for name, member in selected.items():
        target = safe_archive_destination(stage, member)
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"Embedded ReShade runtime is not a regular file: {member}")
        results[name] = target
    return results


def _publish_runtime(stage: Path, dest_dir: Path, results: dict[str, Path]) -> dict[str, Path]:
    destination = safe_archive_destination(dest_dir, stage.parent.name)
    if destination.exists():
        raise FileExistsError(f"ReShade extraction destination already exists: {destination}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stage.replace(destination)
    return {name: destination / path.relative_to(stage) for name, path in results.items()}


def extract_reshade_dlls_from_installer(setup_exe: Path, dest_dir: Path) -> dict[str, Path]:
    try:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dlss5-enabler-reshade-extract-", dir=dest_dir.parent) as stage_name:
            root = Path(stage_name)
            stage = root / "runtime"
            stage.mkdir()
            results = _extract_zip_runtime(setup_exe.read_bytes(), stage)
            if results is None:
                results = _extract_7z_runtime(setup_exe, stage)
            return _publish_runtime(stage, dest_dir, results)
    except Exception as error:
        logger.error(f"Could not safely extract ReShade runtime from {setup_exe.name}: {error}")
        return {}


def normalize_search_paths(
    ini_path: Path,
    rec: InstallRecord,
) -> bool:
    if not ini_path.is_file():
        return True
    try:
        with managed_file_lock(rec, ini_path):
            return _normalize_search_paths_locked(ini_path, rec)
    except Exception as error:
        logger.error(f"Could not preserve or normalize {ini_path}: {error}")
        return False


def _normalize_search_paths_locked(ini_path: Path, rec: InstallRecord) -> bool:

    keys: list[tuple[str, str]] = [
        ("EffectSearchPaths", ".\\reshade-shaders\\Shaders\\**"),
        ("TextureSearchPaths", ".\\reshade-shaders\\Textures\\**"),
    ]
    for key_name, default_val in keys:
        had, orig = ini_get_exact(ini_path, "GENERAL", key_name)
        fixed = _normalize_search_path_list(orig if had else "", default_val)

        if fixed != orig:
            if not ini_set_exact(ini_path, "GENERAL", key_name, fixed):
                return False
            rec.ini_touched.append(IniTouch(path=ini_path.as_posix(), section="GENERAL", key=key_name, original=orig))
            logger.info(f"Fixed {key_name} in {ini_path.name} -> {fixed}")
    return True


def ensure_mv_provider_def(
    ini_path: Path,
    rec: InstallRecord,
) -> bool:
    try:
        with managed_file_lock(rec, ini_path):
            return _ensure_mv_provider_def_locked(ini_path, rec)
    except Exception as error:
        logger.error(f"Could not preserve or configure {ini_path}: {error}")
        return False


def _ensure_mv_provider_def_locked(ini_path: Path, rec: InstallRecord) -> bool:
    _had, orig = ini_get_exact(ini_path, "GENERAL", "PreprocessorDefinitions")
    tokens = [t.strip() for t in orig.split(",") if t.strip() and not t.strip().lower().startswith("dlss5_mv_provider")]
    tokens.append("DLSS5_MV_PROVIDER=3")
    new_val = ",".join(tokens)

    if new_val != orig:
        if not ini_set_exact(ini_path, "GENERAL", "PreprocessorDefinitions", new_val):
            return False
        rec.ini_touched.append(
            IniTouch(path=ini_path.as_posix(), section="GENERAL", key="PreprocessorDefinitions", original=orig)
        )
        logger.info(f"Set DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) in {ini_path.name}")

    had_wrong, wrong_value = ini_get_exact(ini_path, "GENERAL", "PreProcessorDefinitions")
    if had_wrong:
        if not ini_set_exact(ini_path, "GENERAL", "PreProcessorDefinitions", ""):
            return False
        rec.ini_touched.append(
            IniTouch(path=ini_path.as_posix(), section="GENERAL", key="PreProcessorDefinitions", original=wrong_value)
        )
        logger.info(f"Removed obsolete PreProcessorDefinitions key (wrong case) from {ini_path.name}")
    return True
