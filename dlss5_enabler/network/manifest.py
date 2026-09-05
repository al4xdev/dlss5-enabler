from __future__ import annotations

import re
from enum import Enum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXPECTED_COMPONENTS = frozenset(
    {
        "feeder",
        "renodx_dlss5",
        "rhi_manifest",
        "ngx_nr",
        "ngx_sr",
        "ngx_fg",
        "reshade_headers",
        "lumenite",
        "dgvoodoo2",
        "reshade_addon",
    }
)


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactKind(str, Enum):
    FILE = "file"
    JSON = "json"
    ZIP = "zip"


class DiscoveryKind(str, Enum):
    LATEST_RELEASE = "latest_release"
    RELEASE_LIST = "release_list"
    REPOSITORY_FILE = "repository_file"
    REPOSITORY_ARCHIVE = "repository_archive"
    OFFICIAL_PAGE = "official_page"


class DiscoveryPolicy(ManifestModel):
    kind: DiscoveryKind
    asset_patterns: tuple[str, ...] = ()
    release_tag_prefix: str | None = None
    branch: str | None = None
    relative_path: str | None = None
    relative_paths: tuple[str, ...] = ()
    page_url: str | None = None
    download_pattern: str | None = None

    @field_validator("asset_patterns")
    @classmethod
    def validate_asset_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not pattern.strip() for pattern in value):
            raise ValueError("asset patterns cannot be empty")
        return value

    @field_validator("page_url")
    @classmethod
    def validate_page_url(cls, value: str | None) -> str | None:
        if value is not None:
            _require_https(value)
        return value

    @model_validator(mode="after")
    def validate_kind_fields(self) -> DiscoveryPolicy:
        if self.kind in {DiscoveryKind.LATEST_RELEASE, DiscoveryKind.RELEASE_LIST} and not self.asset_patterns:
            raise ValueError(f"{self.kind.value} discovery requires asset_patterns")
        if self.kind is DiscoveryKind.RELEASE_LIST and not self.release_tag_prefix:
            raise ValueError("release_list discovery requires release_tag_prefix")
        if self.kind in {DiscoveryKind.REPOSITORY_FILE, DiscoveryKind.REPOSITORY_ARCHIVE} and not self.branch:
            raise ValueError(f"{self.kind.value} discovery requires branch")
        if self.kind is DiscoveryKind.REPOSITORY_FILE and not self.relative_path and not self.relative_paths:
            raise ValueError("repository_file discovery requires relative_path or relative_paths")
        if self.kind is DiscoveryKind.OFFICIAL_PAGE and (not self.page_url or not self.download_pattern):
            raise ValueError("official_page discovery requires page_url and download_pattern")
        return self


class PinnedArtifact(ManifestModel):
    revision: str = Field(min_length=1)
    name: str = Field(min_length=1)
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=1)
    asset_id: int | None = Field(default=None, ge=1)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _require_https(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(set(value)) == 1:
            raise ValueError("stable artifact SHA-256 cannot be a placeholder")
        return value


class ArchiveFormat(ManifestModel):
    version: int = Field(ge=1)
    required_members: tuple[str, ...] = Field(min_length=1)
    architecture_members: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("required_members")
    @classmethod
    def validate_required_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not member.strip() for member in value):
            raise ValueError("required archive members cannot be empty")
        return value

    @field_validator("architecture_members")
    @classmethod
    def validate_architecture_members(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        if any(architecture not in {"x86", "x64"} for architecture in value):
            raise ValueError("archive architecture must be x86 or x64")
        if any(not members or any(not member.strip() for member in members) for members in value.values()):
            raise ValueError("architecture member patterns cannot be empty")
        return value


class ComponentPolicy(ManifestModel):
    provider: str = Field(min_length=1)
    repository: str | None = None
    artifact_kind: ArtifactKind
    discovery: DiscoveryPolicy
    stable_artifacts: tuple[PinnedArtifact, ...] = Field(min_length=1)
    min_supported_format: int = Field(default=1, ge=1)
    max_supported_format: int = Field(default=1, ge=1)
    formats: tuple[ArchiveFormat, ...] = ()

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise ValueError("repository must use owner/name syntax")
        return value

    @model_validator(mode="after")
    def validate_formats(self) -> ComponentPolicy:
        if self.max_supported_format < self.min_supported_format:
            raise ValueError("max_supported_format cannot be lower than min_supported_format")
        versions = tuple(item.version for item in self.formats)
        if len(versions) != len(set(versions)):
            raise ValueError("archive format versions must be unique")
        if self.artifact_kind is ArtifactKind.ZIP:
            if not versions:
                raise ValueError("zip components require at least one archive format")
            if min(versions) < self.min_supported_format or max(versions) > self.max_supported_format:
                raise ValueError("archive format version is outside the supported range")
        elif self.formats:
            raise ValueError("only zip components can define archive formats")
        if self.discovery.kind is DiscoveryKind.REPOSITORY_ARCHIVE and any(
            re.fullmatch(r"[0-9a-f]{40}", artifact.revision) is None for artifact in self.stable_artifacts
        ):
            raise ValueError("repository archive fallback requires an immutable 40-character commit")
        return self


class EmbeddedUpstreamManifest(ManifestModel):
    schema_version: int
    components: dict[str, ComponentPolicy]

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError(f"unsupported upstream manifest schema: {value}")
        return value

    @field_validator("components")
    @classmethod
    def validate_components(cls, value: dict[str, ComponentPolicy]) -> dict[str, ComponentPolicy]:
        missing = EXPECTED_COMPONENTS.difference(value)
        if missing:
            raise ValueError(f"upstream manifest is missing components: {', '.join(sorted(missing))}")
        unexpected = value.keys() - EXPECTED_COMPONENTS
        if unexpected:
            raise ValueError(f"upstream manifest has unknown components: {', '.join(sorted(unexpected))}")
        return value


class RhiManifestEntry(ManifestModel):
    version: str = Field(min_length=1)
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _require_https(value)


class RhiManifestPayload(ManifestModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    dlssnr: tuple[RhiManifestEntry, ...] = Field(min_length=1)
    dlss: tuple[RhiManifestEntry, ...] = Field(min_length=1)
    dlssg: tuple[RhiManifestEntry, ...] = ()


def _require_https(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("upstream URL must be unauthenticated HTTPS")
    return value


def parse_upstream_manifest(data: bytes | str) -> EmbeddedUpstreamManifest:
    return EmbeddedUpstreamManifest.model_validate_json(data)


@lru_cache(maxsize=1)
def load_upstream_manifest() -> EmbeddedUpstreamManifest:
    data = resources.files("dlss5_enabler").joinpath("upstreams.json").read_bytes()
    return parse_upstream_manifest(data)


def load_upstream_manifest_path(path: Path) -> EmbeddedUpstreamManifest:
    return parse_upstream_manifest(path.read_bytes())
