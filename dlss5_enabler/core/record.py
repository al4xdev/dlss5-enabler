import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dlss5_enabler.core.fileio import atomic_write_bytes, atomic_write_text, resource_lock
from dlss5_enabler.core.util import get_global_index_path
from dlss5_enabler.core.version import get_tool_version
from dlss5_enabler.schemas import migrations
from dlss5_enabler.schemas.strategy import InstallStrategy

CURRENT_RECORD_SCHEMA_VERSION = migrations.CURRENT_RECORD_SCHEMA_VERSION


def _canonical_record_path(value: object) -> str:
    if isinstance(value, (str, Path)):
        return Path(str(value).replace("\\", "/")).as_posix() if value else ""
    raise ValueError("Recorded paths must be strings or Path objects")


class RecordedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    backup: str = ""
    size_bytes: int = 0
    sha256: str = ""

    @field_validator("path", "backup", mode="before")
    @classmethod
    def canonicalize_paths(cls, value: object) -> str:
        return _canonical_record_path(value)


class IniTouch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    section: str
    key: str
    original: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def canonicalize_path(cls, value: object) -> str:
        return _canonical_record_path(value)


class RegistryTouch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reg_path: str
    key: str
    value_name: str
    original_value: str = ""
    original_exists: bool = False

    @field_validator("reg_path", mode="before")
    @classmethod
    def canonicalize_path(cls, value: object) -> str:
        return _canonical_record_path(value)


class RuntimeArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: str
    pattern: str
    preexisting: list[str] = Field(default_factory=lambda: cast(list[str], []))

    @field_validator("directory", mode="before")
    @classmethod
    def canonicalize_directory(cls, value: object) -> str:
        return _canonical_record_path(value)

    @field_validator("preexisting")
    @classmethod
    def canonicalize_preexisting(cls, value: list[str]) -> list[str]:
        return [Path(path.replace("\\", "/")).as_posix() for path in value]


class BinaryInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = ""
    sha256: str = ""
    size_bytes: int = 0
    source_url: str = ""
    source_revision: str = ""


class InstallOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lumenite: bool = True
    d3d9: bool = False
    opengl: bool = False
    vulkan_layer: bool = False


class RenoDxStrategyOptions(InstallOptions):
    kind: Literal["renodx"] = "renodx"

    @classmethod
    def from_install_options(cls, options: InstallOptions) -> "RenoDxStrategyOptions":
        return cls(
            lumenite=options.lumenite, d3d9=options.d3d9, opengl=options.opengl, vulkan_layer=options.vulkan_layer
        )

    def as_install_options(self) -> InstallOptions:
        return InstallOptions(
            lumenite=self.lumenite, d3d9=self.d3d9, opengl=self.opengl, vulkan_layer=self.vulkan_layer
        )


class OptiScalerStrategyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["optiscaler"] = "optiscaler"
    variant: Literal["y4my4my4m-v3"] = "y4my4my4m-v3"
    proxy_name: str
    nr_passes: int = Field(default=1, ge=1, le=5, strict=True)
    source_revision: str = Field(min_length=1)

    @field_validator("proxy_name")
    @classmethod
    def validate_proxy_name(cls, value: str) -> str:
        if value not in {
            "dxgi.dll",
            "winmm.dll",
            "d3d12.dll",
            "dbghelp.dll",
            "version.dll",
            "wininet.dll",
            "winhttp.dll",
        }:
            raise ValueError("Unsupported OptiScaler proxy filename")
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("OptiScaler source_revision must be nonempty and have no surrounding whitespace")
        return value


StrategyOptions = Annotated[RenoDxStrategyOptions | OptiScalerStrategyOptions, Field(discriminator="kind")]


class InstallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=CURRENT_RECORD_SCHEMA_VERSION,
        ge=CURRENT_RECORD_SCHEMA_VERSION,
        le=CURRENT_RECORD_SCHEMA_VERSION,
        strict=True,
    )
    strategy: InstallStrategy = InstallStrategy.RENODX
    tool_version: str = Field(default_factory=get_tool_version)
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
    native_dlss_detected: bool = False
    install_options: InstallOptions = Field(default_factory=InstallOptions)
    strategy_options: StrategyOptions = Field(default_factory=RenoDxStrategyOptions)
    platform: str = "windows"
    proton_prefix: str = ""
    binaries: dict[str, BinaryInfo] = Field(default_factory=lambda: cast(dict[str, BinaryInfo], {}))
    files: list[RecordedFile] = Field(default_factory=lambda: cast(list[RecordedFile], []))
    ini_touched: list[IniTouch] = Field(default_factory=lambda: cast(list[IniTouch], []))
    registry_touched: list[RegistryTouch] = Field(default_factory=lambda: cast(list[RegistryTouch], []))
    runtime_artifacts: list[RuntimeArtifacts] = Field(default_factory=lambda: cast(list[RuntimeArtifacts], []))
    created_directories: list[str] = Field(default_factory=lambda: cast(list[str], []))

    @model_validator(mode="before")
    @classmethod
    def migrate_schema(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(cast(dict[str, object], value))
        options = data.get("install_options")
        if isinstance(options, InstallOptions):
            data["install_options"] = options.model_dump()
        migrated = migrations.migrate_record(data)
        if "strategy" not in migrated:
            raise ValueError(f"Install record schema {CURRENT_RECORD_SCHEMA_VERSION} requires an explicit strategy")
        if "install_options" not in migrated and migrated["strategy"] != InstallStrategy.OPTISCALER:
            raise ValueError(f"Install record schema {CURRENT_RECORD_SCHEMA_VERSION} requires explicit install_options")
        if "strategy_options" not in migrated:
            raise ValueError(
                f"Install record schema {CURRENT_RECORD_SCHEMA_VERSION} requires explicit strategy_options"
            )
        return migrated

    @model_validator(mode="after")
    def validate_strategy_options(self) -> "InstallRecord":
        if self.strategy.value != self.strategy_options.kind:
            raise ValueError("Install record strategy does not match strategy_options.kind")
        if (
            isinstance(self.strategy_options, RenoDxStrategyOptions)
            and self.install_options != self.strategy_options.as_install_options()
        ):
            raise ValueError("RenoDX strategy_options must match legacy install_options")
        return self

    @field_validator("game_exe", "game_dir", "reshade_dir", "proton_prefix", mode="before")
    @classmethod
    def canonicalize_paths(cls, value: object) -> str:
        return _canonical_record_path(value)

    @field_validator("created_directories")
    @classmethod
    def canonicalize_directories(cls, value: list[str]) -> list[str]:
        return [Path(path.replace("\\", "/")).as_posix() for path in value]

    def record_path(self) -> Path:
        return Path(self.game_dir) / "dlss5-enabler.install.json"

    def effective_reshade_dir(self) -> Path:
        return Path(self.reshade_dir) if self.reshade_dir else Path(self.game_dir)


class IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_exe: str
    game_dir: str
    timestamp: str
    architecture: str = "x64"
    install_type: str = "D3D11/D3D12"
    schema_version: int = 1
    tool_version: str = Field(default_factory=get_tool_version)

    @field_validator("game_exe", "game_dir", mode="before")
    @classmethod
    def canonicalize_paths(cls, value: object) -> str:
        return _canonical_record_path(value)


@dataclass(frozen=True)
class IndexEntrySnapshot:
    game_dir: str
    entry: IndexEntry | None
    original_bytes: bytes | None
    position: int | None


def record_save(rec: InstallRecord) -> bool:
    try:
        validated = InstallRecord.model_validate(rec.model_dump(warnings=False))
        p = validated.record_path()
        with resource_lock(p):
            atomic_write_text(p, validated.model_dump_json(indent=2))
        return True
    except Exception:
        return False


def record_load(game_dir: Path | str) -> InstallRecord | None:
    p = Path(game_dir) / "dlss5-enabler.install.json"
    if not p.is_file():
        return None
    try:
        content = p.read_text(encoding="utf-8")
        raw: Any = json.loads(content)
        if not isinstance(raw, dict):
            return None
        return InstallRecord.model_validate(raw)
    except Exception:
        return None


def record_exists(game_dir: Path | str) -> bool:
    return (Path(game_dir) / "dlss5-enabler.install.json").is_file()


def _parse_index(content: bytes | None) -> list[IndexEntry]:
    if content is None:
        return []
    raw: Any = json.loads(content)
    if not isinstance(raw, list):
        raise TypeError("DLSS5 Enabler install index must contain a JSON array")
    data = cast(list[object], raw)
    return [IndexEntry.model_validate(e) for e in data]


def _index_load_unlocked(p: Path) -> list[IndexEntry]:
    return _parse_index(p.read_bytes() if p.exists() else None)


def index_load() -> list[IndexEntry]:
    p = get_global_index_path()
    try:
        with resource_lock(p):
            return _index_load_unlocked(p)
    except Exception:
        return []


def _index_entry_is_active(entry: IndexEntry) -> bool:
    try:
        game_dir = Path(entry.game_dir)
        game_exe = Path(entry.game_exe)
        if (
            game_dir.resolve().is_relative_to(Path(gettempdir()).resolve())
            or not game_dir.is_dir()
            or not game_exe.is_file()
        ):
            return False
        record = record_load(game_dir)
        if record is None:
            return False
        return (
            Path(record.game_dir).resolve() == game_dir.resolve()
            and Path(record.game_exe).resolve() == game_exe.resolve()
        )
    except OSError:
        return False


def index_load_active() -> list[IndexEntry]:
    p = get_global_index_path()
    try:
        with resource_lock(p):
            entries = _index_load_unlocked(p)
            active = [entry for entry in entries if _index_entry_is_active(entry)]
            if len(active) != len(entries):
                _index_save_unlocked(p, active)
            return active
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
                    schema_version=rec.schema_version,
                    tool_version=rec.tool_version,
                )
            )
            _index_save_unlocked(p, entries)
        return True
    except Exception:
        return False


def capture_index_entry(game_dir: Path | str) -> IndexEntrySnapshot:
    target = Path(game_dir).resolve()
    p = get_global_index_path()
    with resource_lock(p):
        original_bytes = p.read_bytes() if p.exists() else None
        entries = _parse_index(original_bytes)
        matching = [(index, entry) for index, entry in enumerate(entries) if Path(entry.game_dir).resolve() == target]
        if len(matching) > 1:
            raise ValueError(f"Multiple install index entries found for {target}")
        position, entry = matching[0] if matching else (None, None)
        return IndexEntrySnapshot(
            game_dir=target.as_posix(), entry=entry, original_bytes=original_bytes, position=position
        )


def _restored_index_position(original: list[IndexEntry], remaining: list[IndexEntry], position: int | None) -> int:
    if position is None:
        return len(remaining)
    successors = {Path(entry.game_dir).resolve() for entry in original[position + 1 :]}
    for index, entry in enumerate(remaining):
        if Path(entry.game_dir).resolve() in successors:
            return index
    predecessors = {Path(entry.game_dir).resolve() for entry in original[:position]}
    for index in range(len(remaining) - 1, -1, -1):
        if Path(remaining[index].game_dir).resolve() in predecessors:
            return index + 1
    return min(position, len(remaining))


def restore_index_entry(game_dir: Path | str, snapshot: IndexEntrySnapshot) -> bool:
    p = get_global_index_path()
    try:
        target = Path(game_dir).resolve()
        if Path(snapshot.game_dir).resolve() != target:
            return False
        entry = snapshot.entry
        if entry is not None and Path(entry.game_dir).resolve() != target:
            return False
        with resource_lock(p):
            current_bytes = p.read_bytes() if p.exists() else None
            entries = _parse_index(current_bytes)
            remaining = [existing for existing in entries if Path(existing.game_dir).resolve() != target]
            original = _parse_index(snapshot.original_bytes)
            original_remaining = [previous for previous in original if Path(previous.game_dir).resolve() != target]
            if remaining == original_remaining:
                if snapshot.original_bytes is None:
                    p.unlink(missing_ok=True)
                elif current_bytes != snapshot.original_bytes:
                    atomic_write_bytes(p, snapshot.original_bytes)
            else:
                if entry is not None:
                    position = _restored_index_position(original, remaining, snapshot.position)
                    remaining.insert(position, entry)
                if remaining != entries:
                    _index_save_unlocked(p, remaining)
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
