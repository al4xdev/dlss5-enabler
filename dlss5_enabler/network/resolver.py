from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dlss5_enabler.core.archive import safe_archive_destination
from dlss5_enabler.core.fileio import atomic_write_bytes, atomic_write_text, resource_lock
from dlss5_enabler.core.util import sha256_file
from dlss5_enabler.network.http import http_download_file
from dlss5_enabler.network.manifest import ArtifactKind, ComponentPolicy, PinnedArtifact, RhiManifestPayload

ProgressFn = Callable[[int, int], None]
DownloadFn = Callable[[str, Path | str, ProgressFn | None], Path]
WarningFn = Callable[[str], None]


class ResolutionWarningCode(str, Enum):
    DISCOVERY_FAILED = "UPSTREAM_DISCOVERY_FAILED"
    ASSET_MISSING = "UPSTREAM_ASSET_MISSING"
    AMBIGUOUS_ASSETS = "UPSTREAM_AMBIGUOUS_ASSETS"
    DOWNLOAD_TIMEOUT = "UPSTREAM_DOWNLOAD_TIMEOUT"
    HTTP_REJECTED = "UPSTREAM_HTTP_REJECTED"
    DIGEST_MISMATCH = "UPSTREAM_DIGEST_MISMATCH"
    ARCHIVE_UNSAFE = "UPSTREAM_ARCHIVE_UNSAFE"
    CONTENT_MISSING = "UPSTREAM_CONTENT_MISSING"
    FORMAT_UNSUPPORTED = "UPSTREAM_FORMAT_UNSUPPORTED"
    STABLE_FALLBACK_USED = "UPSTREAM_STABLE_FALLBACK_USED"
    FALLBACK_FAILED = "UPSTREAM_FALLBACK_FAILED"


class ArtifactOrigin(str, Enum):
    LATEST = "latest"
    STABLE_FALLBACK = "stable_fallback"


@dataclass(frozen=True)
class ArtifactCandidate:
    provider: str
    revision: str
    name: str
    url: str
    sha256: str | None = None
    size_bytes: int | None = None
    asset_id: int | None = None
    format_version: int | None = None


@dataclass(frozen=True)
class ResolutionWarning:
    code: ResolutionWarningCode
    component: str
    provider: str
    reason: str
    latest_revision: str | None = None
    fallback_revision: str | None = None
    log_path: Path | None = None

    def render(self) -> str:
        fields = [f"[{self.code.value}]", self.component, f"provider={self.provider}", self.reason]
        if self.latest_revision:
            fields.append(f"latest={self.latest_revision}")
        if self.fallback_revision:
            fields.append(f"fallback={self.fallback_revision}")
        if self.log_path:
            fields.append(f"log={self.log_path}")
        return " | ".join(fields)


@dataclass(frozen=True)
class ResolvedArtifact:
    component: str
    provider: str
    revision: str
    name: str
    url: str
    path: Path
    sha256: str
    size_bytes: int
    asset_id: int | None
    format_version: int | None
    origin: ArtifactOrigin
    warnings: tuple[ResolutionWarning, ...]


class CacheMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    provider: str
    revision: str
    asset_id: int | None = Field(default=None, ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    format_version: int | None = Field(default=None, ge=1)
    origin: ArtifactOrigin


class ArtifactResolutionError(RuntimeError):
    def __init__(self, code: ResolutionWarningCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class UpstreamResolutionError(RuntimeError):
    def __init__(
        self,
        component: str,
        latest: Exception,
        fallback: Exception,
        warnings: tuple[ResolutionWarning, ...],
    ) -> None:
        super().__init__(f"{component} latest failed ({latest}); stable fallback failed ({fallback})")
        self.component = component
        self.latest = latest
        self.fallback = fallback
        self.warnings = warnings


class ArtifactValidator:
    def validate(
        self,
        path: Path,
        policy: ComponentPolicy,
        candidate: ArtifactCandidate,
        architecture: str | None = None,
    ) -> tuple[str, int, int | None]:
        self.validate_url(candidate.url)
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtifactResolutionError(ResolutionWarningCode.CONTENT_MISSING, "downloaded artifact is empty")
        size = path.stat().st_size
        if candidate.size_bytes is not None and size != candidate.size_bytes:
            raise ArtifactResolutionError(
                ResolutionWarningCode.DIGEST_MISMATCH,
                f"expected {candidate.size_bytes} bytes but received {size}",
            )
        digest = sha256_file(path)
        if candidate.sha256 is not None and digest != candidate.sha256:
            raise ArtifactResolutionError(
                ResolutionWarningCode.DIGEST_MISMATCH,
                f"expected SHA-256 {candidate.sha256} but received {digest}",
            )
        if candidate.format_version is not None and not (
            policy.min_supported_format <= candidate.format_version <= policy.max_supported_format
        ):
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                f"format {candidate.format_version} is outside supported range "
                f"{policy.min_supported_format}-{policy.max_supported_format}",
            )
        recognized_format: int | None = None
        if policy.artifact_kind is ArtifactKind.ZIP:
            recognized_format = self._validate_zip(path, policy, architecture)
        elif policy.artifact_kind is ArtifactKind.JSON:
            self._validate_json(path)
            if policy.repository == "RankFTW/RHI":
                self._validate_rhi_manifest(path)
        return digest, size, recognized_format

    @staticmethod
    def validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise ArtifactResolutionError(
                ResolutionWarningCode.HTTP_REJECTED,
                "artifact URL must be unauthenticated HTTPS",
            )

    @staticmethod
    def _validate_json(path: Path) -> None:
        try:
            value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                f"artifact is not valid UTF-8 JSON: {error}",
            ) from error
        if not isinstance(value, dict):
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                "JSON artifact root must be an object",
            )

    @staticmethod
    def _validate_rhi_manifest(path: Path) -> None:
        try:
            RhiManifestPayload.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                f"RHI manifest does not contain valid NR and SR entries: {error}",
            ) from error

    @staticmethod
    def _validate_zip(path: Path, policy: ComponentPolicy, architecture: str | None) -> int:
        if architecture is not None and architecture not in {"x86", "x64"}:
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                f"unsupported archive architecture: {architecture}",
            )
        try:
            with zipfile.ZipFile(path, "r") as archive:
                members = tuple(info.filename.replace("\\", "/") for info in archive.infolist() if not info.is_dir())
                invalid_member = archive.testzip()
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                f"artifact is not a readable ZIP: {error}",
            ) from error
        if invalid_member is not None:
            raise ArtifactResolutionError(
                ResolutionWarningCode.FORMAT_UNSUPPORTED,
                f"archive member failed its CRC check: {invalid_member}",
            )
        validation_root = path.parent / f".{path.name}.validation"
        normalized_members: set[str] = set()
        for member in members:
            try:
                safe_archive_destination(validation_root, member)
            except ValueError as error:
                raise ArtifactResolutionError(ResolutionWarningCode.ARCHIVE_UNSAFE, str(error)) from error
            normalized = member.casefold()
            if normalized in normalized_members:
                raise ArtifactResolutionError(
                    ResolutionWarningCode.ARCHIVE_UNSAFE,
                    f"archive contains colliding member paths: {member}",
                )
            normalized_members.add(normalized)
        for archive_format in sorted(policy.formats, key=lambda item: item.version, reverse=True):
            required = archive_format.required_members
            if architecture is not None:
                required += archive_format.architecture_members.get(architecture, ())
            if all(any(fnmatch(member.lower(), pattern.lower()) for member in members) for pattern in required):
                matched_members = {
                    member
                    for member in members
                    if any(fnmatch(member.lower(), pattern.lower()) for pattern in required)
                }
                flattened: set[str] = set()
                for member in matched_members:
                    name = Path(member).name.casefold()
                    if name in flattened:
                        raise ArtifactResolutionError(
                            ResolutionWarningCode.ARCHIVE_UNSAFE,
                            f"archive members collide when flattened: {Path(member).name}",
                        )
                    flattened.add(name)
                return archive_format.version
        expected = sorted(
            {
                pattern
                for archive_format in policy.formats
                for pattern in (
                    archive_format.required_members
                    + (() if architecture is None else archive_format.architecture_members.get(architecture, ()))
                )
            }
        )
        raise ArtifactResolutionError(
            ResolutionWarningCode.CONTENT_MISSING,
            f"archive does not match a supported layout; expected: {', '.join(expected)}",
        )


class UpstreamResolver:
    def __init__(
        self,
        downloader: DownloadFn = http_download_file,
        validator: ArtifactValidator | None = None,
        warning_fn: WarningFn | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._downloader = downloader
        self._validator = validator or ArtifactValidator()
        self._warning_fn = warning_fn
        self._log_path = log_path

    def resolve(
        self,
        component: str,
        policy: ComponentPolicy,
        destination: Path,
        latest: Callable[[], Sequence[ArtifactCandidate]],
        stable_name: str | None = None,
        architecture: str | None = None,
        progress: ProgressFn | None = None,
        force: bool = False,
    ) -> ResolvedArtifact:
        warnings: list[ResolutionWarning] = []
        latest_revision: str | None = None
        try:
            candidates = tuple(latest())
            revisions = {candidate.revision for candidate in candidates}
            if len(revisions) == 1:
                latest_revision = next(iter(revisions))
            candidate = self.select_candidate(component, policy, candidates, stable_name)
            latest_revision = candidate.revision
            return self._materialize(
                component,
                policy,
                candidate,
                destination,
                ArtifactOrigin.LATEST,
                architecture,
                progress,
                force,
                tuple(warnings),
            )
        except Exception as latest_error:
            latest_failure = latest_error
            warning = self._warning_for_error(
                component,
                policy.provider,
                latest_error,
                latest_revision=latest_revision,
            )
            warnings.append(warning)
            self._emit(warning)
        try:
            stable = self._select_stable(policy, stable_name)
            candidate = self._stable_candidate(policy, stable)
            fallback = self._materialize(
                component,
                policy,
                candidate,
                destination,
                ArtifactOrigin.STABLE_FALLBACK,
                architecture,
                progress,
                force,
                (),
            )
        except Exception as fallback_error:
            warning = ResolutionWarning(
                code=ResolutionWarningCode.FALLBACK_FAILED,
                component=component,
                provider=policy.provider,
                reason=self._safe_reason(fallback_error),
                latest_revision=latest_revision,
                fallback_revision=self._fallback_revision(policy, stable_name),
                log_path=self._log_path,
            )
            warnings.append(warning)
            self._emit(warning)
            raise UpstreamResolutionError(
                component, latest_failure, fallback_error, tuple(warnings)
            ) from fallback_error
        warning = ResolutionWarning(
            code=ResolutionWarningCode.STABLE_FALLBACK_USED,
            component=component,
            provider=policy.provider,
            reason="latest artifact was rejected; using validated stable fallback",
            latest_revision=latest_revision,
            fallback_revision=fallback.revision,
            log_path=self._log_path,
        )
        warnings.append(warning)
        self._emit(warning)
        return ResolvedArtifact(
            component=fallback.component,
            provider=fallback.provider,
            revision=fallback.revision,
            name=fallback.name,
            url=fallback.url,
            path=fallback.path,
            sha256=fallback.sha256,
            size_bytes=fallback.size_bytes,
            asset_id=fallback.asset_id,
            format_version=fallback.format_version,
            origin=fallback.origin,
            warnings=tuple(warnings),
        )

    @staticmethod
    def select_candidate(
        component: str,
        policy: ComponentPolicy,
        candidates: Sequence[ArtifactCandidate],
        stable_name: str | None = None,
    ) -> ArtifactCandidate:
        patterns = policy.discovery.asset_patterns
        if stable_name is not None and not patterns:
            patterns = (stable_name,)
        matched = tuple(
            candidate
            for candidate in candidates
            if not patterns or any(fnmatch(candidate.name.lower(), pattern.lower()) for pattern in patterns)
        )
        if len(matched) == 1:
            return matched[0]
        if (
            not matched
            and len(candidates) == 1
            and policy.artifact_kind is ArtifactKind.ZIP
            and candidates[0].name.lower().endswith(".zip")
        ):
            return candidates[0]
        published = ", ".join(candidate.name for candidate in candidates) or "none"
        if not matched:
            raise ArtifactResolutionError(
                ResolutionWarningCode.ASSET_MISSING,
                f"{component} has no matching asset; published: {published}",
            )
        raise ArtifactResolutionError(
            ResolutionWarningCode.AMBIGUOUS_ASSETS,
            f"{component} has multiple matching assets: {', '.join(candidate.name for candidate in matched)}",
        )

    def _materialize(
        self,
        component: str,
        policy: ComponentPolicy,
        candidate: ArtifactCandidate,
        destination: Path,
        origin: ArtifactOrigin,
        architecture: str | None,
        progress: ProgressFn | None,
        force: bool,
        warnings: tuple[ResolutionWarning, ...],
    ) -> ResolvedArtifact:
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = Path(f"{destination}.dlss5-enabler-cache.json")
        with resource_lock(Path(f"{destination}.cache-state")):
            cached = self._cached(
                component,
                policy,
                candidate,
                destination,
                metadata_path,
                architecture,
                warnings,
                force,
            )
            if cached is not None:
                return cached
            with tempfile.TemporaryDirectory(prefix=f".{destination.name}.", dir=destination.parent) as raw_temp:
                temporary = Path(raw_temp) / candidate.name
                self._validator.validate_url(candidate.url)
                try:
                    downloaded = self._downloader(candidate.url, temporary, progress)
                except Exception as error:
                    code = self._download_error_code(error)
                    raise ArtifactResolutionError(code, self._safe_reason(error)) from error
                digest, size, format_version = self._validator.validate(
                    downloaded,
                    policy,
                    candidate,
                    architecture,
                )
                metadata = CacheMetadata(
                    url=candidate.url,
                    provider=candidate.provider,
                    revision=candidate.revision,
                    asset_id=candidate.asset_id,
                    sha256=digest,
                    size_bytes=size,
                    format_version=format_version,
                    origin=origin,
                )
                self._promote(downloaded, destination, metadata_path, metadata)
        return ResolvedArtifact(
            component=component,
            provider=candidate.provider,
            revision=candidate.revision,
            name=candidate.name,
            url=candidate.url,
            path=destination,
            sha256=digest,
            size_bytes=size,
            asset_id=candidate.asset_id,
            format_version=format_version,
            origin=origin,
            warnings=warnings,
        )

    def _cached(
        self,
        component: str,
        policy: ComponentPolicy,
        candidate: ArtifactCandidate,
        destination: Path,
        metadata_path: Path,
        architecture: str | None,
        warnings: tuple[ResolutionWarning, ...],
        force: bool,
    ) -> ResolvedArtifact | None:
        if force or not destination.is_file() or not metadata_path.is_file():
            return None
        try:
            metadata = CacheMetadata.model_validate_json(metadata_path.read_bytes())
        except (OSError, ValidationError):
            return None
        if metadata.url != candidate.url or metadata.revision != candidate.revision:
            return None
        cached_candidate = ArtifactCandidate(
            provider=candidate.provider,
            revision=candidate.revision,
            name=candidate.name,
            url=candidate.url,
            sha256=candidate.sha256 or metadata.sha256,
            size_bytes=candidate.size_bytes or metadata.size_bytes,
            asset_id=candidate.asset_id,
            format_version=candidate.format_version,
        )
        try:
            digest, size, format_version = self._validator.validate(
                destination,
                policy,
                cached_candidate,
                architecture,
            )
        except ArtifactResolutionError:
            return None
        if metadata.sha256 != digest or metadata.size_bytes != size or metadata.format_version != format_version:
            return None
        return ResolvedArtifact(
            component=component,
            provider=candidate.provider,
            revision=candidate.revision,
            name=candidate.name,
            url=candidate.url,
            path=destination,
            sha256=digest,
            size_bytes=size,
            asset_id=candidate.asset_id,
            format_version=format_version,
            origin=metadata.origin,
            warnings=warnings,
        )

    @staticmethod
    def _promote(source: Path, destination: Path, metadata_path: Path, metadata: CacheMetadata) -> None:
        fd, backup_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".backup", dir=destination.parent)
        os.close(fd)
        backup = Path(backup_name)
        backup.unlink()
        previous_metadata: bytes | None = None
        had_destination = destination.is_file()
        had_metadata = metadata_path.is_file()
        if had_metadata:
            previous_metadata = metadata_path.read_bytes()
        try:
            if had_destination:
                destination.replace(backup)
            source.replace(destination)
            atomic_write_text(metadata_path, metadata.model_dump_json())
        except Exception:
            destination.unlink(missing_ok=True)
            if had_destination and backup.exists():
                backup.replace(destination)
            if previous_metadata is not None:
                atomic_write_bytes(metadata_path, previous_metadata)
            elif not had_metadata:
                metadata_path.unlink(missing_ok=True)
            raise
        finally:
            backup.unlink(missing_ok=True)

    @staticmethod
    def _select_stable(policy: ComponentPolicy, stable_name: str | None) -> PinnedArtifact:
        if stable_name is None and len(policy.stable_artifacts) == 1:
            return policy.stable_artifacts[0]
        matches = tuple(item for item in policy.stable_artifacts if item.name.lower() == (stable_name or "").lower())
        if len(matches) != 1:
            raise ArtifactResolutionError(
                ResolutionWarningCode.ASSET_MISSING,
                f"stable fallback {stable_name or '<unspecified>'} is not uniquely defined",
            )
        return matches[0]

    @staticmethod
    def _stable_candidate(policy: ComponentPolicy, artifact: PinnedArtifact) -> ArtifactCandidate:
        return ArtifactCandidate(
            provider=policy.provider,
            revision=artifact.revision,
            name=artifact.name,
            url=artifact.url,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            asset_id=artifact.asset_id,
        )

    @staticmethod
    def _download_error_code(error: Exception) -> ResolutionWarningCode:
        text = str(error).lower()
        if isinstance(error, TimeoutError) or "timeout" in text or "timed out" in text or "exceeded" in text:
            return ResolutionWarningCode.DOWNLOAD_TIMEOUT
        return ResolutionWarningCode.HTTP_REJECTED

    def _warning_for_error(
        self,
        component: str,
        provider: str,
        error: Exception,
        latest_revision: str | None,
    ) -> ResolutionWarning:
        code = error.code if isinstance(error, ArtifactResolutionError) else ResolutionWarningCode.DISCOVERY_FAILED
        return ResolutionWarning(
            code=code,
            component=component,
            provider=provider,
            reason=self._safe_reason(error),
            latest_revision=latest_revision,
            log_path=self._log_path,
        )

    def _emit(self, warning: ResolutionWarning) -> None:
        if self._warning_fn is not None:
            self._warning_fn(warning.render())

    @staticmethod
    def _safe_reason(error: Exception) -> str:
        reason = str(error)
        for part in reason.split():
            parsed = urlparse(part.rstrip(",);"))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            host = parsed.hostname or "<redacted>"
            replacement = f"{parsed.scheme}://{host}{parsed.path}"
            if parsed.username is not None or parsed.password is not None:
                replacement = f"{parsed.scheme}://{host}/<redacted>"
            reason = reason.replace(parsed.geturl(), replacement)
        return reason

    @staticmethod
    def _fallback_revision(policy: ComponentPolicy, stable_name: str | None) -> str | None:
        try:
            return UpstreamResolver._select_stable(policy, stable_name).revision
        except ArtifactResolutionError:
            return None
