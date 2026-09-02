from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from dlss5_enabler.core.record import (
    BinaryInfo,
    IndexEntry,
    IniTouch,
    InstallRecord,
    RecordedFile,
    index_add,
    index_load,
    index_remove,
    index_save,
    record_exists,
    record_load,
    record_save,
)


def test_models_instantiation() -> None:
    rec_file = RecordedFile(path="C:/game/d3d11.dll", backup="C:/game/d3d11.dll.bak", size_bytes=1024, sha256="abc")
    assert rec_file.path == "C:/game/d3d11.dll"
    assert rec_file.size_bytes == 1024

    touch = IniTouch(path="C:/game/ReShade.ini", section="GENERAL", key="EffectSearchPaths", original=".\\Shaders")
    assert touch.key == "EffectSearchPaths"

    bin_info = BinaryInfo(
        name="ReShade", version="6.8.0", sha256="123", size_bytes=2048, source_url="http://reshade.me"
    )
    assert bin_info.version == "6.8.0"


def test_record_paths_are_canonical_posix() -> None:
    rec = InstallRecord(
        game_exe=r"C:\Games\Title\game.exe",
        game_dir=r"C:\Games\Title",
        files=[RecordedFile(path=r"C:\Games\Title\dxgi.dll", backup=r"C:\Games\Title\dxgi.dll.bak")],
    )

    assert rec.game_exe == "C:/Games/Title/game.exe"
    assert rec.game_dir == "C:/Games/Title"
    assert rec.files[0].path == "C:/Games/Title/dxgi.dll"


def test_install_record_methods(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_exe = game_dir / "game.exe"
    rec = InstallRecord(game_exe=str(game_exe), game_dir=str(game_dir), reshade_dir="")

    assert rec.record_path() == game_dir / "dlss5-enabler.install.json"
    assert rec.effective_reshade_dir() == game_dir

    rec.reshade_dir = str(game_dir / "bin")
    assert rec.effective_reshade_dir() == game_dir / "bin"


def test_record_save_load_exists(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    game_exe = game_dir / "game.exe"

    rec = InstallRecord(
        game_exe=str(game_exe),
        game_dir=str(game_dir),
        architecture="x64",
        install_type="D3D11/D3D12",
        files=[RecordedFile(path=str(game_dir / "dxgi.dll"), size_bytes=100)],
        binaries={"dxgi": BinaryInfo(name="dxgi", version="1.0")},
    )

    assert not record_exists(game_dir)
    assert record_load(game_dir) is None

    assert record_save(rec)
    assert record_exists(game_dir)

    loaded = record_load(game_dir)
    assert loaded is not None
    assert loaded.game_exe == game_exe.as_posix()
    assert loaded.architecture == "x64"
    assert len(loaded.files) == 1
    assert loaded.files[0].path == str(game_dir / "dxgi.dll")
    assert "dxgi" in loaded.binaries


def test_record_load_corrupted_json(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)
    bad_json = game_dir / "dlss5-enabler.install.json"
    bad_json.write_text("{corrupted json", encoding="utf-8")

    assert record_load(game_dir) is None


def test_record_save_failure(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    rec = InstallRecord(game_exe=str(game_dir / "g.exe"), game_dir=str(game_dir))

    with patch("dlss5_enabler.core.record.atomic_write_text", side_effect=OSError("Permission denied")):
        assert not record_save(rec)


def test_global_index_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    assert index_load() == []

    rec1 = InstallRecord(game_exe="C:/g1/g1.exe", game_dir="C:/g1", architecture="x64", install_type="D3D12")
    rec2 = InstallRecord(game_exe="C:/g2/g2.exe", game_dir="C:/g2", architecture="x86", install_type="D3D9")

    assert index_add(rec1)
    entries = index_load()
    assert len(entries) == 1
    assert entries[0].game_exe == "C:/g1/g1.exe"

    assert index_add(rec2)
    entries = index_load()
    assert len(entries) == 2

    # Update existing record
    rec1_updated = InstallRecord(game_exe="C:/g1/g1.exe", game_dir="C:/g1", architecture="x64", install_type="OpenGL")
    assert index_add(rec1_updated)
    entries = index_load()
    assert len(entries) == 2
    g1_entry = next(e for e in entries if e.game_dir == "C:/g1")
    assert g1_entry.install_type == "OpenGL"

    # Remove entry
    assert index_remove("C:/g1")
    entries = index_load()
    assert len(entries) == 1
    assert entries[0].game_dir == "C:/g2"

    # Remove non-existent returns True
    assert index_remove("C:/nonexistent")
    assert len(index_load()) == 1


def test_index_load_corrupted_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    fake_index.write_text("invalid json", encoding="utf-8")
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    assert index_load() == []


def test_index_add_preserves_corrupted_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    fake_index.write_text("invalid json", encoding="utf-8")
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    rec = InstallRecord(game_exe="C:/g.exe", game_dir="C:/g")

    assert not index_add(rec)
    assert fake_index.read_text(encoding="utf-8") == "invalid json"


def test_index_add_is_concurrency_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    fake_index.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)
    records = [InstallRecord(game_exe=f"C:/g{i}/g.exe", game_dir=f"C:/g{i}") for i in range(8)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(index_add, records))

    assert all(results)
    assert {entry.game_dir for entry in index_load()} == {record.game_dir for record in records}


def test_index_save_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_index = tmp_path / "appdata" / "installs.json"
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: fake_index)

    entries = [IndexEntry(game_exe="C:/g.exe", game_dir="C:/g", timestamp="2026-01-01")]
    with patch("dlss5_enabler.core.record.atomic_write_text", side_effect=OSError("Disk full")):
        assert not index_save(entries)
