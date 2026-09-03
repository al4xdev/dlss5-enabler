import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import py7zr

from dlss5_enabler.core.archive import safe_archive_destination
from dlss5_enabler.core.fileio import atomic_write_bytes
from dlss5_enabler.core.ini import ini_get_exact, ini_set_exact
from dlss5_enabler.core.logger import get_logger
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


def _register_archive_destination(destinations: set[Path], target: Path, member: str) -> None:
    if target in destinations:
        raise ValueError(f"Duplicate embedded archive member: {member}")
    destinations.add(target)


def extract_reshade_dlls_from_installer(
    setup_exe: Path,
    dest_dir: Path,
) -> dict[str, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}

    try:
        data = setup_exe.read_bytes()
        idx = 0
        while True:
            idx = data.find(b"PK\x03\x04", idx)
            if idx == -1:
                break
            try:
                with zipfile.ZipFile(io.BytesIO(data[idx:])) as z:
                    names = z.namelist()
                    if any("reshade" in n.lower() for n in names):
                        destinations: set[Path] = set()
                        for info in z.infolist():
                            if info.is_dir():
                                continue
                            target = safe_archive_destination(dest_dir, info.filename)
                            _register_archive_destination(destinations, target, info.filename)
                            atomic_write_bytes(target, z.read(info))
                        for name in ["ReShade64.dll", "ReShade32.dll", "ReShade64.json", "ReShade32.json"]:
                            matches = list(dest_dir.rglob(name))
                            if matches:
                                results[name.lower()] = matches[0]
                        if "reshade64.dll" in results or "reshade32.dll" in results:
                            return results
            except Exception:
                pass
            idx += 4
    except Exception as e:
        logger.debug(f"Embedded zip extraction failed: {e}")

    try:
        with py7zr.SevenZipFile(setup_exe, mode="r") as archive:
            for name in archive.getnames():
                safe_archive_destination(dest_dir, name)
            archive.extractall(path=dest_dir)
    except Exception as e:
        logger.debug(f"py7zr extraction failed: {e}")

    for name in ["ReShade64.dll", "ReShade32.dll", "ReShade64.json", "ReShade32.json"]:
        matches = list(dest_dir.rglob(name))
        if matches:
            results[name.lower()] = matches[0]

    if "reshade64.dll" in results or "reshade32.dll" in results:
        return results

    return results


def reshade_headless_install(
    setup_exe: Path,
    target_exe: Path,
    api: str,
) -> bool:
    logger.info(f"Running ReShade setup (unattended) for {target_exe.name} [api {api}]...")
    cmd = [str(setup_exe), "--headless", str(target_exe), "--api", api]
    if sys.platform != "win32" and shutil.which("wine"):
        cmd = ["wine", *cmd]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
        for line in proc.stdout.splitlines():
            t = line.strip()
            if t:
                logger.info(f"  reshade: {t}")
        if proc.returncode != 0:
            logger.error(f"ReShade setup failed with exit code {proc.returncode}")
            return False
        return True
    except Exception:
        logger.exception("ReShade setup execution failed")
        return False


def normalize_search_paths(
    ini_path: Path,
    rec: InstallRecord,
) -> bool:
    if not ini_path.is_file():
        return True

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
            rec.ini_touched.append(IniTouch(path=str(ini_path), section="GENERAL", key=key_name, original=orig.strip()))
            logger.info(f"Fixed {key_name} in {ini_path.name} -> {fixed}")
    return True


def ensure_mv_provider_def(
    ini_path: Path,
    rec: InstallRecord,
) -> bool:
    _had, orig = ini_get_exact(ini_path, "GENERAL", "PreprocessorDefinitions")
    tokens = [t.strip() for t in orig.split(",") if t.strip() and not t.strip().lower().startswith("dlss5_mv_provider")]
    tokens.append("DLSS5_MV_PROVIDER=3")
    new_val = ",".join(tokens)

    if new_val != orig:
        if not ini_set_exact(ini_path, "GENERAL", "PreprocessorDefinitions", new_val):
            return False
        rec.ini_touched.append(
            IniTouch(path=str(ini_path), section="GENERAL", key="PreprocessorDefinitions", original=orig)
        )
        logger.info(f"Set DLSS5_MV_PROVIDER=3 (LumeniteFX Kernel) in {ini_path.name}")

    had_wrong, _ = ini_get_exact(ini_path, "GENERAL", "PreProcessorDefinitions")
    if had_wrong:
        if not ini_set_exact(ini_path, "GENERAL", "PreProcessorDefinitions", ""):
            return False
        logger.info(f"Removed obsolete PreProcessorDefinitions key (wrong case) from {ini_path.name}")
    return True
