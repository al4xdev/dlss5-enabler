import hashlib
import os
import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock


def _lock_path(resource: Path | str) -> Path:
    key = hashlib.sha256(str(Path(resource).resolve()).encode()).hexdigest()
    root = Path(tempfile.gettempdir()) / "dlss5-enabler-locks"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{key}.lock"


@contextmanager
def resource_lock(resource: Path | str, timeout: float = 300.0) -> Generator[None, None, None]:
    with FileLock(_lock_path(resource), timeout=timeout):
        yield


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            temp.chmod(target.stat().st_mode)
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, content.encode(encoding))


def atomic_copy_file(source: Path | str, destination: Path | str) -> None:
    src = Path(source)
    target = Path(destination)
    with resource_lock(target):
        _atomic_copy_file_unlocked(src, target)


def _atomic_copy_file_unlocked(src: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(src, temp)
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def unique_backup_path(path: Path | str) -> Path:
    target = Path(path)
    base = target.with_suffix(target.suffix + ".dlss5-enabler.bak")
    candidate = base
    index = 1
    while candidate.exists():
        candidate = Path(f"{base}.{index}")
        index += 1
    return candidate
