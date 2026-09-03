import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dlss5_enabler.core.fileio import atomic_copy_file, resource_lock
from dlss5_enabler.core.ini import ini_set_exact
from dlss5_enabler.core.record import InstallRecord, RecordedFile, index_add, index_remove, record_load
from dlss5_enabler.core.util import remove_dir_if_empty
from dlss5_enabler.platform import get_platform_adapter
from dlss5_enabler.platform.proton import ProtonManager, WineRegParser

LogFn = Callable[[str], None] | Any


@dataclass
class InstallSnapshot:
    root: Path
    record: InstallRecord
    files: dict[str, Path]


def _recorded_files(rec: InstallRecord) -> list[RecordedFile]:
    selected: dict[str, RecordedFile] = {}
    order: list[str] = []
    for item in rec.files:
        key = str(Path(item.path).resolve())
        if key not in selected:
            order.append(key)
            selected[key] = item
        elif item.backup and not selected[key].backup:
            selected[key] = item
    return [selected[key] for key in reversed(order)]


def capture_install_snapshot(rec: InstallRecord) -> InstallSnapshot:
    root = Path(tempfile.mkdtemp(prefix="dlss5-enabler-install-snapshot-"))
    try:
        paths: list[Path] = [Path(item.path) for item in rec.files]
        paths.extend(Path(item.backup) for item in rec.files if item.backup)
        paths.extend(Path(item.path) for item in rec.ini_touched)
        paths.extend(Path(item.reg_path) for item in rec.registry_touched)
        paths.append(rec.record_path())
        reshade_dir = rec.effective_reshade_dir()
        paths.extend(reshade_dir / name for name in ["ReShade.log", "ReShade.ini.bak", "dxgi.log"])
        files: dict[str, Path] = {}
        for path in paths:
            resolved = str(path.resolve())
            if resolved in files or not path.is_file():
                continue
            saved = root / str(len(files))
            with resource_lock(path):
                shutil.copy2(path, saved)
            files[resolved] = saved
        return InstallSnapshot(root=root, record=rec.model_copy(deep=True), files=files)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def cleanup_install_snapshot(snapshot: InstallSnapshot) -> None:
    shutil.rmtree(snapshot.root, ignore_errors=True)


def restore_install_snapshot(snapshot: InstallSnapshot) -> bool:
    try:
        for original, saved in snapshot.files.items():
            atomic_copy_file(saved, Path(original))
        if not index_add(snapshot.record):
            return False
        cleanup_install_snapshot(snapshot)
        return True
    except Exception:
        return False


def revert_record_mutations(rec: InstallRecord, log: LogFn = print) -> bool:
    success = True
    for ini_touch in reversed(rec.ini_touched):
        ini_path = Path(ini_touch.path)
        if ini_path.is_file():
            if ini_set_exact(ini_path, ini_touch.section, ini_touch.key, ini_touch.original):
                log(f"Restored {ini_touch.key} in {ini_path.name}")
            else:
                log(f"Could not restore {ini_touch.key} in {ini_path.name}")
                success = False

    if rec.registry_touched:
        for reg_touch in reversed(rec.registry_touched):
            value = reg_touch.original_value if reg_touch.original_exists else None
            if WineRegParser.restore_overrides(reg_touch.reg_path, {reg_touch.value_name: value}):
                log(f"Restored Wine registry override: {reg_touch.value_name}")
            else:
                log(f"Could not restore Wine registry override: {reg_touch.value_name}")
                success = False
    elif rec.proton_prefix:
        prefix = Path(rec.proton_prefix)
        if prefix.is_dir() and not ProtonManager.revert_overrides(prefix, ["dxgi", "d3d9", "opengl32"]):
            success = False

    for item in _recorded_files(rec):
        path = Path(item.path)
        if path.exists():
            try:
                path.unlink()
                log(f"Removed {path.name}")
            except Exception as error:
                log(f"Could not remove {path.name} (locked?): {error}")
                success = False
                continue
        if item.backup:
            backup = Path(item.backup)
            if backup.is_file():
                try:
                    atomic_copy_file(backup, path)
                    backup.unlink()
                    log(f"Restored backup -> {path.name}")
                except Exception as error:
                    log(f"Could not restore backup for {path.name}: {error}")
                    success = False
            else:
                log(f"Backup missing for {path.name}: {backup}")
                success = False
    return success


def _cleanup_empty_directories(rec: InstallRecord, game_dir: Path) -> None:
    reshade_dir = rec.effective_reshade_dir()
    for directory in [
        reshade_dir / "reshade-shaders" / "Shaders" / "include",
        reshade_dir / "reshade-shaders" / "Shaders",
        reshade_dir / "reshade-shaders" / "Textures",
        reshade_dir / "reshade-shaders",
        reshade_dir / "host64",
        game_dir / "reshade-shaders" / "Shaders" / "include",
        game_dir / "reshade-shaders" / "Shaders",
        game_dir / "reshade-shaders" / "Textures",
        game_dir / "reshade-shaders",
        game_dir / "host64",
    ]:
        remove_dir_if_empty(directory)


def _finalize_uninstall(rec: InstallRecord, game_dir: Path, snapshot: InstallSnapshot, log: LogFn) -> bool:
    reshade_dir = rec.effective_reshade_dir()
    if rec.reshade_by_us:
        for name in ["ReShade.log", "ReShade.ini.bak", "dxgi.log"]:
            extra = reshade_dir / name
            if extra.is_file():
                try:
                    extra.unlink()
                except Exception as error:
                    restore_install_snapshot(snapshot)
                    log(f"Could not remove {extra.name}: {error}")
                    return False
    _cleanup_empty_directories(rec, game_dir)
    if not index_remove(game_dir):
        restore_install_snapshot(snapshot)
        log("Could not update the global install index; install record was preserved.")
        return False
    rec_file = rec.record_path()
    try:
        with resource_lock(rec_file):
            rec_file.unlink(missing_ok=True)
    except Exception as error:
        restore_install_snapshot(snapshot)
        log(f"Could not remove install record: {error}")
        return False
    return True


def run_uninstall(game_dir_or_exe: Path | str, log: LogFn = print, lock_operation: bool = True) -> bool:
    target = Path(game_dir_or_exe).resolve()
    game_dir = target if target.is_dir() else target.parent
    if lock_operation:
        with resource_lock(game_dir / ".dlss5-enabler-install-operation"):
            return _run_uninstall_unlocked(game_dir, log)
    return _run_uninstall_unlocked(game_dir, log)


def _run_uninstall_unlocked(game_dir: Path, log: LogFn) -> bool:
    rec = record_load(game_dir)
    if not rec:
        log(f"No DLSS5 Enabler install record found in {game_dir} - nothing to uninstall.")
        return False
    game_exe = Path(rec.game_exe)
    if not game_exe.is_absolute():
        game_exe = game_dir / game_exe
    if get_platform_adapter().is_game_running(game_exe):
        log(f"Cannot uninstall while {game_exe.name} is running. Close the game and try again.")
        return False

    log(f"Uninstalling DLSS5 Enabler files from {game_dir}...")
    try:
        snapshot = capture_install_snapshot(rec)
    except Exception as error:
        log(f"Could not create uninstall recovery snapshot: {error}")
        return False
    if not revert_record_mutations(rec, log):
        restore_install_snapshot(snapshot)
        log("Uninstall incomplete; install record was preserved for recovery.")
        return False

    if not _finalize_uninstall(rec, game_dir, snapshot, log):
        return False

    cleanup_install_snapshot(snapshot)
    log("Uninstall completed successfully.")
    return True
