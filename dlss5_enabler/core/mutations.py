from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from stat import FILE_ATTRIBUTE_REPARSE_POINT

from dlss5_enabler.core.fileio import atomic_copy_file, resource_lock, unique_backup_path
from dlss5_enabler.core.record import InstallRecord, RecordedFile, RuntimeArtifacts


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(int(getattr(path.lstat(), "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


def reject_reparse_ancestors(path: Path) -> None:
    for candidate in [path, *path.parents]:
        if _is_reparse_point(candidate):
            raise ValueError(f"Managed path must not traverse a symbolic link or reparse point: {candidate}")


def _installation_roots(rec: InstallRecord) -> set[Path]:
    return {Path(rec.game_dir).resolve(), rec.effective_reshade_dir().resolve()}


def _validate_owned_directory(rec: InstallRecord, directory: Path) -> Path:
    roots = _installation_roots(rec)
    if (
        not directory.is_absolute()
        or ".." in directory.parts
        or not any(directory != root and directory.is_relative_to(root) for root in roots)
    ):
        raise ValueError(f"Created directory is outside the managed installation: {directory}")
    reject_reparse_ancestors(directory)
    return directory.resolve()


def managed_created_directories(rec: InstallRecord) -> list[Path]:
    return [_validate_owned_directory(rec, Path(path)) for path in rec.created_directories]


def validate_runtime_directory(rec: InstallRecord, directory: Path, pattern: str) -> Path:
    if not pattern or pattern in {".", ".."} or any(token in pattern for token in ("/", "\\", "**", ":", "\x00")):
        raise ValueError(f"Runtime artifact pattern must match files in one directory: {pattern}")
    roots = _installation_roots(rec)
    if rec.d3d9_translate:
        roots.add((Path(rec.game_dir) / "bin").resolve())
    allowed = roots | {root / "host64" for root in roots} | set(managed_created_directories(rec))
    if not directory.is_absolute() or ".." in directory.parts or directory not in allowed:
        raise ValueError(f"Runtime artifact directory is not owned by this installation: {directory}")
    reject_reparse_ancestors(directory)
    return directory.resolve()


def track_created_directories(rec: InstallRecord, directory: Path) -> None:
    reject_reparse_ancestors(directory)
    for candidate in [directory, *directory.parents]:
        if candidate.exists():
            if not candidate.is_dir():
                raise ValueError(f"Expected a directory: {candidate}")
            break
        canonical = candidate.resolve().as_posix()
        if canonical not in rec.created_directories:
            rec.created_directories.append(canonical)


def _prepare_managed_path_unlocked(rec: InstallRecord, dst: Path) -> RecordedFile:
    reject_reparse_ancestors(dst)
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
    directory = validate_runtime_directory(rec, directory, pattern)
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
