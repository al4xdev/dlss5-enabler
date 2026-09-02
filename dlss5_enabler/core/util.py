import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from dlss5_enabler.core.fileio import resource_lock
from dlss5_enabler.platform import get_platform_adapter


def local_appdata() -> Path:
    return get_platform_adapter().get_data_dir()


def get_cache_dir() -> Path:
    return get_platform_adapter().get_cache_dir()


def get_global_index_path() -> Path:
    return get_platform_adapter().get_data_dir() / "installs.json"


def sha256_file(path: Path | str) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    h = hashlib.sha256()
    try:
        with p.open("rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def unblock_file(path: Path | str) -> None:
    get_platform_adapter().unblock_file(path)


def make_executable(path: Path | str) -> None:
    get_platform_adapter().make_executable(path)


def file_is_writable(path: Path | str) -> bool:
    p = Path(path)
    if not p.exists():
        return True
    try:
        with p.open("r+b"):
            return True
    except (PermissionError, OSError):
        return False


def is_directory_writable(directory: Path | str) -> bool:
    return get_platform_adapter().is_directory_writable(directory)


def get_permission_guidance(directory: Path | str) -> str:
    return get_platform_adapter().get_permission_guidance(directory)


def create_hardlink_or_copy(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with resource_lock(dst):
        fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
        os.close(fd)
        temp = Path(temp_name)
        temp.unlink(missing_ok=True)
        try:
            try:
                os.link(src, temp)
            except OSError:
                shutil.copy2(src, temp)
            temp.replace(dst)
            return True
        except Exception:
            temp.unlink(missing_ok=True)
            return False


def remove_dir_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except Exception:
        pass
