import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, field_validator

from dlss5_enabler.core.fileio import atomic_write_text, resource_lock
from dlss5_enabler.core.util import get_global_index_path


class RecordedFile(BaseModel):
    path: str
    backup: str = ""
    size_bytes: int = 0
    sha256: str = ""

    @field_validator("path", "backup", mode="before")
    @classmethod
    def canonicalize_paths(cls, value: Any) -> str:
        return Path(str(value).replace("\\", "/")).as_posix() if value else ""


class IniTouch(BaseModel):
    path: str
    section: str
    key: str
    original: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def canonicalize_path(cls, value: Any) -> str:
        return Path(str(value).replace("\\", "/")).as_posix()


class RegistryTouch(BaseModel):
    reg_path: str
    key: str
    value_name: str
    original_value: str = ""
    original_exists: bool = False

    @field_validator("reg_path", mode="before")
    @classmethod
    def canonicalize_path(cls, value: Any) -> str:
        return Path(str(value).replace("\\", "/")).as_posix()


class BinaryInfo(BaseModel):
    name: str
    version: str = ""
    sha256: str = ""
    size_bytes: int = 0
    source_url: str = ""


class InstallRecord(BaseModel):
    tool_version: str = "1.0.0"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    game_exe: str
    game_dir: str
    reshade_dir: str = ""
    architecture: str = "x64"
    is_32bit: bool = False
    install_type: str = "D3D11/D3D12"
    d3d9_translate: bool = False
    opengl: bool = False
    reshade_by_us: bool = False
    vulkan_layer: bool = False
    lumenite_installed: bool = True
    platform: str = "windows"
    proton_prefix: str = ""
    binaries: dict[str, BinaryInfo] = Field(default_factory=lambda: cast(dict[str, BinaryInfo], {}))
    files: list[RecordedFile] = Field(default_factory=lambda: cast(list[RecordedFile], []))
    ini_touched: list[IniTouch] = Field(default_factory=lambda: cast(list[IniTouch], []))
    registry_touched: list[RegistryTouch] = Field(default_factory=lambda: cast(list[RegistryTouch], []))

    @field_validator("game_exe", "game_dir", "reshade_dir", "proton_prefix", mode="before")
    @classmethod
    def canonicalize_paths(cls, value: Any) -> str:
        return Path(str(value).replace("\\", "/")).as_posix() if value else ""

    def record_path(self) -> Path:
        return Path(self.game_dir) / "dlss5-enabler.install.json"

    def effective_reshade_dir(self) -> Path:
        return Path(self.reshade_dir) if self.reshade_dir else Path(self.game_dir)


class IndexEntry(BaseModel):
    game_exe: str
    game_dir: str
    timestamp: str
    architecture: str = "x64"
    install_type: str = "D3D11/D3D12"
    tool_version: str = "1.0.0"

    @field_validator("game_exe", "game_dir", mode="before")
    @classmethod
    def canonicalize_paths(cls, value: Any) -> str:
        return Path(str(value).replace("\\", "/")).as_posix()


def record_save(rec: InstallRecord) -> bool:
    p = rec.record_path()
    try:
        with resource_lock(p):
            atomic_write_text(p, rec.model_dump_json(indent=2))
        return True
    except Exception:
        return False


def record_load(game_dir: Path | str) -> InstallRecord | None:
    p = Path(game_dir) / "dlss5-enabler.install.json"
    if not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8")
        return InstallRecord.model_validate_json(content)
    except Exception:
        return None


def record_exists(game_dir: Path | str) -> bool:
    return (Path(game_dir) / "dlss5-enabler.install.json").is_file()


def _index_load_unlocked(p: Path) -> list[IndexEntry]:
    if not p.is_file():
        return []
    content = p.read_text(encoding="utf-8")
    raw: Any = json.loads(content)
    if not isinstance(raw, list):
        raise TypeError("DLSS5 Enabler install index must contain a JSON array")
    data = cast(list[dict[str, Any]], raw)
    return [IndexEntry.model_validate(e) for e in data]


def index_load() -> list[IndexEntry]:
    p = get_global_index_path()
    try:
        with resource_lock(p):
            return _index_load_unlocked(p)
    except Exception:
        return []


def _index_save_unlocked(p: Path, entries: list[IndexEntry]) -> None:
    data = [e.model_dump() for e in entries]
    atomic_write_text(p, json.dumps(data, indent=2))


def index_save(entries: list[IndexEntry]) -> bool:
    p = get_global_index_path()
    try:
        with resource_lock(p):
            _index_save_unlocked(p, entries)
        return True
    except Exception:
        return False


def index_add(rec: InstallRecord) -> bool:
    p = get_global_index_path()
    try:
        with resource_lock(p):
            entries = _index_load_unlocked(p)
            entries = [e for e in entries if Path(e.game_dir).resolve() != Path(rec.game_dir).resolve()]
            entries.append(
                IndexEntry(
                    game_exe=rec.game_exe,
                    game_dir=rec.game_dir,
                    timestamp=rec.timestamp,
                    architecture=rec.architecture,
                    install_type=rec.install_type,
                    tool_version=rec.tool_version,
                )
            )
            _index_save_unlocked(p, entries)
        return True
    except Exception:
        return False


def index_remove(game_dir: Path | str) -> bool:
    p = get_global_index_path()
    try:
        with resource_lock(p):
            entries = _index_load_unlocked(p)
            remaining = [e for e in entries if Path(e.game_dir).resolve() != Path(game_dir).resolve()]
            if len(remaining) != len(entries):
                _index_save_unlocked(p, remaining)
        return True
    except Exception:
        return False
