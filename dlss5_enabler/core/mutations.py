from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from dlss5_enabler.core.fileio import atomic_copy_file, resource_lock, unique_backup_path
from dlss5_enabler.core.record import InstallRecord, RecordedFile, RuntimeArtifacts


def track_created_directories(rec: InstallRecord, directory: Path) -> None:
    for candidate in [directory, *directory.parents]:
        if candidate.exists():
            if not candidate.is_dir():
                raise ValueError(f"Expected a directory: {candidate}")
            break
        canonical = candidate.resolve().as_posix()
        if canonical not in rec.created_directories:
            rec.created_directories.append(canonical)


def _prepare_managed_path_unlocked(rec: InstallRecord, dst: Path) -> RecordedFile:
    existing = next((item for item in rec.files if Path(item.path).resolve() == dst.resolve()), None)
    if existing is not None:
        return existing
    if dst.is_symlink() or (dst.exists() and not dst.is_file()):
        raise ValueError(f"Managed destination must be a regular file: {dst}")
    track_created_directories(rec, dst.parent)
    backup_str = ""
    if dst.exists():
        backup = unique_backup_path(dst)
        atomic_copy_file(dst, backup)
        backup_str = backup.as_posix()
    item = RecordedFile(path=dst.resolve().as_posix(), backup=backup_str)
    rec.files.append(item)
    return item


def prepare_managed_path(rec: InstallRecord, dst: Path) -> RecordedFile:
    with resource_lock(dst):
        return _prepare_managed_path_unlocked(rec, dst)


@contextmanager
def managed_file_lock(rec: InstallRecord, dst: Path) -> Generator[RecordedFile, None, None]:
    with resource_lock(dst):
        yield _prepare_managed_path_unlocked(rec, dst)


def prepare_runtime_artifacts(rec: InstallRecord, directory: Path, pattern: str) -> None:
    if not pattern or pattern in {".", ".."} or any(token in pattern for token in ("/", "\\", "**")):
        raise ValueError(f"Runtime artifact pattern must match files in one directory: {pattern}")
    if any(
        Path(rule.directory).resolve() == directory.resolve() and rule.pattern == pattern
        for rule in rec.runtime_artifacts
    ):
        return
    rec.runtime_artifacts.append(
        RuntimeArtifacts(
            directory=directory.resolve().as_posix(),
            pattern=pattern,
            preexisting=sorted(path.resolve().as_posix() for path in directory.glob(pattern)),
        )
    )
