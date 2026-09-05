import base64
import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dlss5_enabler.core.fileio import atomic_copy_file, atomic_write_text, resource_lock
from dlss5_enabler.core.ini import ini_set_exact
from dlss5_enabler.core.mutations import (
    managed_created_directories,
    reject_reparse_ancestors,
    validate_runtime_directory,
)
from dlss5_enabler.core.record import (
    IndexEntrySnapshot,
    InstallRecord,
    RecordedFile,
    capture_index_entry,
    index_remove,
    record_load,
    restore_index_entry,
)
from dlss5_enabler.platform import get_platform_adapter
from dlss5_enabler.platform.proton import ProtonManager, WineRegParser

LogFn = Callable[[str], object]


@dataclass
class InstallSnapshot:
    root: Path
    record: InstallRecord
    files: dict[str, Path]
    missing_files: list[str] = field(default_factory=list[str])
    directories: list[str] = field(default_factory=list[str])
    index_entry: IndexEntrySnapshot | None = None
    recovery_errors: list[str] = field(default_factory=list[str])


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


def _runtime_artifacts(rec: InstallRecord) -> list[Path]:
    recorded = {Path(item.path).resolve() for item in rec.files}
    found: set[Path] = set()
    for rule in rec.runtime_artifacts:
        directory = validate_runtime_directory(rec, Path(rule.directory), rule.pattern)
        preexisting = {Path(path).resolve() for path in rule.preexisting}
        for path in directory.glob(rule.pattern):
            resolved = path.resolve()
            if not path.is_symlink() and path.is_file() and resolved not in preexisting | recorded:
                found.add(path)
    return sorted(found)


def _capture_file(path: Path, saved: Path) -> bool:
    with resource_lock(path):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Recovery snapshot requires a regular file: {path}")
        if not path.exists():
            return False
        shutil.copy2(path, saved)
        return True


def capture_install_snapshot(rec: InstallRecord) -> InstallSnapshot:
    root = Path(tempfile.mkdtemp(prefix="dlss5-enabler-install-snapshot-"))
    try:
        paths: list[Path] = [Path(item.path) for item in rec.files]
        paths.extend(Path(item.backup) for item in rec.files if item.backup)
        paths.extend(Path(item.path) for item in rec.ini_touched)
        paths.extend(Path(item.reg_path) for item in rec.registry_touched)
        paths.append(rec.record_path())
        paths.extend(_runtime_artifacts(rec))
        files: dict[str, Path] = {}
        missing_files: list[str] = []
        for path in paths:
            resolved = path.resolve().as_posix()
            if resolved in files or resolved in missing_files:
                continue
            saved = root / str(len(files))
            if _capture_file(path, saved):
                files[resolved] = saved
            else:
                missing_files.append(resolved)
        directories = [path.as_posix() for path in managed_created_directories(rec) if path.is_dir()]
        index_entry = capture_index_entry(rec.game_dir)
        snapshot = InstallSnapshot(
            root=root,
            record=rec.model_copy(deep=True),
            files=files,
            missing_files=missing_files,
            directories=directories,
            index_entry=index_entry,
        )
        atomic_write_text(
            root / "recovery.json",
            json.dumps(
                {
                    "snapshot_version": 1,
                    "record": rec.model_dump(mode="json"),
                    "files": {original: saved.name for original, saved in files.items()},
                    "missing_files": missing_files,
                    "directories": directories,
                    "index_entry": {
                        "game_dir": index_entry.game_dir,
                        "entry": index_entry.entry.model_dump(mode="json") if index_entry.entry else None,
                        "original_bytes": (
                            base64.b64encode(index_entry.original_bytes).decode("ascii")
                            if index_entry.original_bytes is not None
                            else None
                        ),
                        "position": index_entry.position,
                    },
                },
                indent=2,
            ),
        )
        return snapshot
    except Exception:
        cleanup_install_snapshot(InstallSnapshot(root=root, record=rec, files={}))
        raise


def cleanup_install_snapshot(snapshot: InstallSnapshot) -> None:
    root = snapshot.root.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if root.parent != temp_root or not root.name.startswith("dlss5-enabler-install-snapshot-"):
        raise ValueError(f"Refusing to remove an invalid recovery snapshot directory: {root}")
    if root.exists():
        shutil.rmtree(root)


def restore_install_snapshot(snapshot: InstallSnapshot, *, cleanup: bool = True) -> bool:
    snapshot.recovery_errors.clear()
    for original in snapshot.missing_files:
        try:
            with resource_lock(original):
                Path(original).unlink(missing_ok=True)
        except Exception as error:
            snapshot.recovery_errors.append(f"Could not restore absence of {original}: {error}")
    for directory in snapshot.directories:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except Exception as error:
            snapshot.recovery_errors.append(f"Could not restore directory {directory}: {error}")
    for original, saved in snapshot.files.items():
        try:
            atomic_copy_file(saved, Path(original))
        except Exception as error:
            snapshot.recovery_errors.append(f"Could not restore {original}: {error}")
    if snapshot.index_entry is None or not restore_index_entry(snapshot.record.game_dir, snapshot.index_entry):
        snapshot.recovery_errors.append("Could not restore the previous global index entry.")
    if snapshot.recovery_errors:
        return False
    if not cleanup:
        return True
    try:
        cleanup_install_snapshot(snapshot)
    except Exception as error:
        snapshot.recovery_errors.append(f"Could not remove the completed recovery snapshot: {error}")
        return False
    return True


def _validate_backups(rec: InstallRecord, log: LogFn) -> bool:
    for item in _recorded_files(rec):
        path = Path(item.path)
        try:
            reject_reparse_ancestors(path)
            if item.backup:
                reject_reparse_ancestors(Path(item.backup))
        except ValueError as error:
            log(str(error))
            return False
        if path.is_symlink() or (path.exists() and not path.is_file()):
            log(f"Cannot restore a destination that is not a regular file: {path}")
            return False
        if item.backup and (not Path(item.backup).is_file() or Path(item.backup).is_symlink()):
            log(f"Backup missing or not a regular file for {path.name}: {item.backup}")
            return False
    return True


def _restore_recorded_file(item: RecordedFile, log: LogFn) -> bool:
    path = Path(item.path)
    try:
        if item.backup:
            backup = Path(item.backup)
            atomic_copy_file(backup, path)
            backup.unlink()
            log(f"Restored backup -> {path.name}")
        else:
            with resource_lock(path):
                path.unlink(missing_ok=True)
            log(f"Removed {path.name}")
        return True
    except Exception as error:
        log(f"Could not restore {path.name}: {error}")
        return False


def _cleanup_created_directories(directories: list[Path]) -> None:
    for directory in sorted(set(directories), key=lambda path: len(path.parts), reverse=True):
        reject_reparse_ancestors(directory)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def revert_record_mutations(rec: InstallRecord, log: LogFn = print) -> bool:
    try:
        artifacts = _runtime_artifacts(rec)
        directories = managed_created_directories(rec)
    except Exception as error:
        log(f"Could not validate managed runtime artifacts or directories: {error}")
        return False
    if not _validate_backups(rec, log):
        return False
    success = True
    recorded_paths = {Path(item.path).resolve() for item in rec.files}
    for ini_touch in reversed(rec.ini_touched):
        ini_path = Path(ini_touch.path)
        if ini_path.resolve() in recorded_paths:
            continue
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
        if not _restore_recorded_file(item, log):
            success = False
    if success:
        try:
            for artifact in artifacts:
                with resource_lock(artifact):
                    reject_reparse_ancestors(artifact)
                    artifact.unlink(missing_ok=True)
            _cleanup_created_directories(directories)
        except Exception as error:
            log(f"Could not remove managed runtime artifacts or directories: {error}")
            success = False
    return success


def _recover_failed_uninstall(snapshot: InstallSnapshot, log: LogFn) -> None:
    if restore_install_snapshot(snapshot):
        log("The previous installation state was restored.")
    else:
        for error in snapshot.recovery_errors:
            log(error)
        log(f"Automatic recovery is incomplete. Recovery files and recovery.json were retained at {snapshot.root}.")


def _finalize_uninstall(rec: InstallRecord, game_dir: Path, snapshot: InstallSnapshot, log: LogFn) -> bool:
    if not index_remove(game_dir):
        _recover_failed_uninstall(snapshot, log)
        log("Could not update the global install index.")
        return False
    rec_file = rec.record_path()
    try:
        with resource_lock(rec_file):
            rec_file.unlink(missing_ok=True)
    except Exception as error:
        _recover_failed_uninstall(snapshot, log)
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


def _handle_missing_install_record(game_dir: Path, log: LogFn) -> bool:
    if (game_dir / "dlss5-enabler.install.json").exists():
        log(f"The install record in {game_dir} could not be read or validated; installation and index were preserved.")
        return False
    if not index_remove(game_dir):
        log(f"No install record found in {game_dir}, and the stale index entry could not be removed.")
        return False
    log(f"No DLSS5 Enabler install record found in {game_dir}; it is already uninstalled.")
    return True


def _can_uninstall(rec: InstallRecord, game_dir: Path, log: LogFn) -> bool:
    if Path(rec.game_dir).resolve() != game_dir:
        log("The install record belongs to another directory; no files were changed.")
        return False
    game_exe = Path(rec.game_exe)
    if not game_exe.is_absolute():
        game_exe = game_dir / game_exe
    if get_platform_adapter().is_game_running(game_exe):
        log(f"Cannot uninstall while {game_exe.name} is running. Close the game and try again.")
        return False
    return True


def _revert_for_uninstall(rec: InstallRecord, log: LogFn) -> bool:
    try:
        return revert_record_mutations(rec, log)
    except Exception as error:
        log(f"Unexpected failure while reverting installation mutations: {error}")
        return False


def _run_uninstall_unlocked(game_dir: Path, log: LogFn) -> bool:
    rec = record_load(game_dir)
    if not rec:
        return _handle_missing_install_record(game_dir, log)
    if not _can_uninstall(rec, game_dir, log):
        return False

    log(f"Uninstalling DLSS5 Enabler files from {game_dir}...")
    try:
        snapshot = capture_install_snapshot(rec)
    except Exception as error:
        log(f"Could not create uninstall recovery snapshot: {error}")
        return False
    if not _revert_for_uninstall(rec, log):
        _recover_failed_uninstall(snapshot, log)
        log("Uninstall incomplete.")
        return False

    if not _finalize_uninstall(rec, game_dir, snapshot, log):
        return False

    try:
        cleanup_install_snapshot(snapshot)
    except Exception as error:
        log(f"Uninstall completed, but recovery snapshot cleanup failed at {snapshot.root}: {error}")
    else:
        log("Uninstall completed successfully.")
    return True
