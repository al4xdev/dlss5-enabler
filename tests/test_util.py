import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from dlss5_enabler.core.util import (
    create_hardlink_or_copy,
    file_is_writable,
    get_cache_dir,
    get_global_index_path,
    local_appdata,
    remove_dir_if_empty,
    sha256_file,
    unblock_file,
)
from dlss5_enabler.platform import WindowsAdapter


def test_local_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dlss5_enabler.core.util.get_platform_adapter", WindowsAdapter)
    custom_appdata = tmp_path / "custom_appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(custom_appdata))
    assert local_appdata() == custom_appdata / "DLSS5 Enabler"

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    assert local_appdata() == tmp_path / "user" / "AppData" / "Local" / "DLSS5 Enabler"


def test_get_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dlss5_enabler.core.util.get_platform_adapter", WindowsAdapter)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cache = get_cache_dir()
    assert cache == tmp_path / "DLSS5 Enabler" / "downloads"
    assert cache.is_dir()


def test_get_global_index_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("dlss5_enabler.core.util.get_platform_adapter", WindowsAdapter)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    index = get_global_index_path()
    assert index == tmp_path / "DLSS5 Enabler" / "installs.json"
    assert index.parent.is_dir()


def test_sha256_file(tmp_path: Path) -> None:
    assert sha256_file(tmp_path / "missing.file") == ""

    empty_file = tmp_path / "empty.txt"
    empty_file.write_bytes(b"")
    assert sha256_file(empty_file) == hashlib.sha256(b"").hexdigest()

    test_file = tmp_path / "data.bin"
    data = b"Hello, DLSS5 Enabler Neural Rendering!" * 1000
    test_file.write_bytes(data)
    expected_hash = hashlib.sha256(data).hexdigest()
    assert sha256_file(test_file) == expected_hash


def test_unblock_file_non_existent(tmp_path: Path) -> None:
    unblock_file(tmp_path / "missing.dll")


def test_unblock_file_ads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dlss5_enabler.core.util.get_platform_adapter", WindowsAdapter)
    target = tmp_path / "blocked.dll"
    target.write_bytes(b"MZ")

    removed_paths: list[str] = []

    def mock_exists(p: Any) -> bool:
        return ":Zone.Identifier" in str(p)

    def mock_remove(p: Any) -> None:
        removed_paths.append(str(p))

    monkeypatch.setattr(os.path, "exists", mock_exists)
    monkeypatch.setattr(os, "remove", mock_remove)

    unblock_file(target)
    assert any(":Zone.Identifier" in p for p in removed_paths)


def test_file_is_writable(tmp_path: Path) -> None:
    assert file_is_writable(tmp_path / "missing.txt")

    f = tmp_path / "writable.txt"
    f.write_text("content", encoding="utf-8")
    assert file_is_writable(f)


def test_file_is_writable_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "locked.txt"
    f.write_text("content", encoding="utf-8")

    def mock_open(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("File in use")

    monkeypatch.setattr(Path, "open", mock_open)
    assert not file_is_writable(f)


def test_create_hardlink_or_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.dll"
    src.write_bytes(b"SOURCE_DATA")
    dst = tmp_path / "dst_dir" / "dst.dll"

    assert create_hardlink_or_copy(src, dst)
    assert dst.is_file()
    assert dst.read_bytes() == b"SOURCE_DATA"

    # Overwriting existing destination
    src.write_bytes(b"SOURCE_DATA_UPDATED")
    assert create_hardlink_or_copy(src, dst)
    assert dst.read_bytes() == b"SOURCE_DATA_UPDATED"


def test_create_hardlink_or_copy_fallback_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src.dll"
    src.write_bytes(b"DATA")
    dst = tmp_path / "dst.dll"

    def mock_link(s: Any, d: Any) -> None:
        raise OSError("Cross-device link not permitted")

    monkeypatch.setattr(os, "link", mock_link)
    assert create_hardlink_or_copy(src, dst)
    assert dst.is_file()

    def mock_copy2(s: Any, d: Any) -> None:
        raise OSError("Copy failed")

    monkeypatch.setattr(shutil, "copy2", mock_copy2)
    assert not create_hardlink_or_copy(src, tmp_path / "dst2.dll")


def test_create_hardlink_or_copy_failure_preserves_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src.dll"
    src.write_bytes(b"NEW")
    dst = tmp_path / "dst.dll"
    dst.write_bytes(b"ORIGINAL")

    def fail_link(_src: Any, _dst: Any) -> None:
        raise OSError("no hardlink")

    def fail_copy(_src: Any, _dst: Any) -> None:
        raise OSError("no copy")

    monkeypatch.setattr(os, "link", fail_link)
    monkeypatch.setattr(shutil, "copy2", fail_copy)

    assert not create_hardlink_or_copy(src, dst)
    assert dst.read_bytes() == b"ORIGINAL"


def test_remove_dir_if_empty(tmp_path: Path) -> None:
    empty_d = tmp_path / "empty_dir"
    empty_d.mkdir()
    remove_dir_if_empty(empty_d)
    assert not empty_d.exists()

    non_empty_d = tmp_path / "non_empty"
    non_empty_d.mkdir()
    (non_empty_d / "file.txt").write_text("stay", encoding="utf-8")
    remove_dir_if_empty(non_empty_d)
    assert non_empty_d.exists()

    remove_dir_if_empty(tmp_path / "does_not_exist")
