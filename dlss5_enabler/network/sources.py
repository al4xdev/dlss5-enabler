from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from fnmatch import fnmatch
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

from dlss5_enabler.core.archive import safe_archive_destination
from dlss5_enabler.core.fileio import _atomic_copy_file_unlocked, atomic_write_bytes, atomic_write_text, resource_lock
from dlss5_enabler.core.record import BinaryInfo
from dlss5_enabler.core.util import get_cache_dir, sha256_file
from dlss5_enabler.network.adapters import (
    DownloadSourceAdapter,
    RepositorySnapshot,
    SourceAsset,
    get_download_source_adapter,
)
from dlss5_enabler.network.http import http_download_file, http_get_json, http_get_text
from dlss5_enabler.network.manifest import ComponentPolicy, RhiManifestEntry, RhiManifestPayload, load_upstream_manifest
from dlss5_enabler.network.resolver import ArtifactCandidate, ResolutionWarning, ResolvedArtifact, UpstreamResolver

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


def _version_key(value: str) -> tuple[int, ...]:
    numbers = tuple(int(part) for part in re.findall(r"\d+", value))
    return numbers or (0,)


def _github() -> DownloadSourceAdapter:
    return get_download_source_adapter("github", http_get_json)


def _policy(component: str) -> ComponentPolicy:
    return load_upstream_manifest().components[component]


def _download(url: str, destination: Path | str, progress: ProgressFn | None) -> Path:
    return http_download_file(url, destination, progress_fn=progress)


def _resolver(log: LogFn) -> UpstreamResolver:
    return UpstreamResolver(downloader=_download, warning_fn=lambda message: log(f"WARNING: {message}"))


def _artifact_candidate(provider: str, asset: SourceAsset) -> ArtifactCandidate:
    return ArtifactCandidate(
        provider=provider,
        revision=asset.revision,
        name=asset.name,
        url=asset.url,
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        asset_id=asset.asset_id,
    )


def _release_candidates(adapter: DownloadSourceAdapter, repository: str) -> tuple[ArtifactCandidate, ...]:
    release = adapter.latest_release(repository)
    return tuple(_artifact_candidate(adapter.name, asset) for asset in release.assets)


def _repository_file_candidates(
    adapter: DownloadSourceAdapter,
    repository: str,
    revision: str,
    relative_path: str,
) -> tuple[ArtifactCandidate, ...]:
    asset = adapter.repository_file(repository, revision, relative_path)
    return (_artifact_candidate(adapter.name, asset),)


def _binary(path: Path, name: str, resolved: ResolvedArtifact) -> BinaryInfo:
    return BinaryInfo(
        name=name,
        version=resolved.revision,
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        source_url=resolved.url,
        source_revision=resolved.revision,
    )


def zip_extract_matching(
    zip_path: Path | str,
    dest_dir: Path | str,
    patterns: list[str],
    flatten: bool = True,
) -> list[Path]:
    zpath = Path(zip_path)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    with zipfile.ZipFile(zpath, "r") as archive:
        destinations: set[Path] = set()
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if not any(fnmatch(name.lower(), pattern.lower()) for pattern in patterns):
                continue
            out_path = safe_archive_destination(dest, name, flatten=flatten)
            if out_path in destinations:
                raise ValueError(f"Archive members collide at destination: {out_path.name}")
            destinations.add(out_path)
            with resource_lock(out_path):
                atomic_write_bytes(out_path, archive.read(info))
            extracted.append(out_path)

    if not extracted:
        raise ValueError(f"No matching files in {zpath.name} for patterns: {patterns}")
    return extracted


def zip_has_matching(zip_path: Path | str, patterns: list[str]) -> bool:
    with zipfile.ZipFile(zip_path, "r") as archive:
        return any(
            any(fnmatch(info.filename.replace("\\", "/").lower(), pattern.lower()) for pattern in patterns)
            for info in archive.infolist()
            if not info.is_dir()
        )


class FeederBundle:
    def __init__(self) -> None:
        self.release_tag: str = ""
        self.addon64: Path | None = None
        self.addon32: Path | None = None
        self.fx_shader: Path | None = None
        self.host64_exe: Path | None = None
        self.vk_layer_zip: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


class OptiScalerBundle:
    def __init__(self) -> None:
        self.archive_path: Path | None = None
        self.variant: str = ""
        self.source_revision: str = ""
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


class DlssgBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.dll_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def fetch_optiscaler(
    log: LogFn,
    progress: ProgressFn | None = None,
    force: bool = False,
    archive_path: Path | None = None,
    source_revision: str = "",
) -> OptiScalerBundle:
    del log, progress, force
    expected_digest = "f927b5aed15d09b23f559433d6740834f550d79bb2b75c7315602319819a3096"
    out = OptiScalerBundle()
    if archive_path is not None:
        archive = archive_path.expanduser().resolve()
        if not archive.is_file():
            raise FileNotFoundError(f"OptiScaler archive not found: {archive}")
        digest = sha256_file(archive)
        if digest != expected_digest:
            raise ValueError(f"Unsupported OptiScaler archive SHA-256: {digest}")
        cache_path = get_cache_dir() / f"OptiScaler-y4my4my4m-v3-{digest}.zip"
        with resource_lock(cache_path):
            if not cache_path.is_file() or sha256_file(cache_path) != digest:
                _atomic_copy_file_unlocked(archive, cache_path)
        out.archive_path = cache_path
        out.variant = "y4my4my4m-v3"
        out.source_revision = digest
        out.binaries[archive.name] = BinaryInfo(
            name=archive.name,
            version=out.variant,
            sha256=digest,
            size_bytes=archive.stat().st_size,
            source_revision=digest,
        )
        return out
    revision = source_revision or expected_digest
    if revision != expected_digest:
        raise ValueError(f"Unsupported OptiScaler source revision: {revision}")
    cache_path = get_cache_dir() / f"OptiScaler-y4my4my4m-v3-{revision}.zip"
    if not cache_path.is_file() or sha256_file(cache_path) != revision:
        raise FileNotFoundError("The recorded OptiScaler archive is not available in the verified local cache")
    out.archive_path = cache_path
    out.variant = "y4my4my4m-v3"
    out.source_revision = revision
    out.binaries[cache_path.name] = BinaryInfo(
        name=cache_path.name,
        version=out.variant,
        sha256=revision,
        size_bytes=cache_path.stat().st_size,
        source_revision=revision,
    )
    return out


def fetch_dlssg(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> DlssgBundle:
    cache_dir = get_cache_dir()
    manifest_result = _resolve_rhi_manifest(log, cache_dir, progress, force)
    manifest = RhiManifestPayload.model_validate_json(manifest_result.path.read_bytes())
    if not manifest.dlssg:
        raise ValueError("RHI manifest does not publish a DLSS Frame Generation runtime")
    entry = max(manifest.dlssg, key=lambda item: _version_key(item.version))
    result = _resolve_ngx("ngx_fg", entry, cache_dir / "nvngx_dlssg.zip", log, progress, force)
    dll_path = zip_extract_matching(result.path, cache_dir, ["*nvngx_dlssg.dll"], flatten=True)[0]
    out = DlssgBundle()
    out.version = result.revision.removeprefix("dlssg-")
    out.dll_path = dll_path
    out.warnings = manifest_result.warnings + result.warnings
    out.binaries["nvngx_dlssg.dll"] = _binary(dll_path, "nvngx_dlssg.dll", result)
    return out


def fetch_feeder(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> FeederBundle:
    out = FeederBundle()
    cache_dir = get_cache_dir()
    policy = _policy("feeder")
    adapter = _github()
    repository = policy.repository
    if repository is None:
        raise RuntimeError("DLSS5-Feeder policy has no repository")
    resolved = _resolver(log).resolve(
        "feeder",
        policy,
        cache_dir / "DLSS5-Feeder.zip",
        lambda: _release_candidates(adapter, repository),
        progress=progress,
        force=force,
    )
    out.release_tag = resolved.revision
    out.warnings = resolved.warnings
    log(f"DLSS5-Feeder release: {out.release_tag}")
    required_names = (
        "dlss5-feed.addon64",
        "dlss5-feed.addon32",
        "DLSS5_Feed.fx",
        "dlss5-feed-host64.exe",
    )
    extracted = zip_extract_matching(
        resolved.path,
        cache_dir,
        [f"*{name}" for name in (*required_names, "feed-vk-layer.zip")],
        flatten=True,
    )
    paths = {path.name.lower(): path for path in extracted}
    missing = [name for name in required_names if name.lower() not in paths]
    if missing:
        raise RuntimeError(f"DLSS5-Feeder release {resolved.revision} archive is missing: {', '.join(missing)}")
    out.addon64 = paths["dlss5-feed.addon64"]
    out.addon32 = paths["dlss5-feed.addon32"]
    out.fx_shader = paths["dlss5_feed.fx"]
    out.host64_exe = paths["dlss5-feed-host64.exe"]
    out.vk_layer_zip = paths.get("feed-vk-layer.zip")
    if out.vk_layer_zip is None and zip_has_matching(resolved.path, ["*layer-x64/*", "*layer-x86/*"]):
        out.vk_layer_zip = resolved.path
    for name in required_names:
        path = paths[name.lower()]
        out.binaries[name] = _binary(path, name, resolved)
    if out.vk_layer_zip is not None:
        out.binaries["feed-vk-layer.zip"] = _binary(out.vk_layer_zip, "feed-vk-layer.zip", resolved)
    return out


class RenoDxBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.addon64_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def fetch_renodx_dlss5(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> RenoDxBundle:
    out = RenoDxBundle()
    cache_dir = get_cache_dir()
    policy = _policy("renodx_dlss5")
    adapter = _github()
    repository = policy.repository
    prefix = policy.discovery.release_tag_prefix
    if repository is None or prefix is None:
        raise RuntimeError("RenoDX policy is incomplete")

    def latest() -> Sequence[ArtifactCandidate]:
        releases = tuple(
            release for release in adapter.releases(repository) if release.tag.lower().startswith(prefix.lower())
        )
        if not releases:
            return ()
        release = max(releases, key=lambda item: _version_key(item.tag[len(prefix) :]))
        return tuple(_artifact_candidate(adapter.name, asset) for asset in release.assets)

    resolved = _resolver(log).resolve(
        "renodx_dlss5",
        policy,
        cache_dir / "renodx-dlss5.zip",
        latest,
        progress=progress,
        force=force,
    )
    out.version = resolved.revision.removeprefix(prefix)
    out.warnings = resolved.warnings
    log(f"renodx-dlss5 release: {out.version}")
    out.addon64_path = zip_extract_matching(resolved.path, cache_dir, ["*renodx-dlss5.addon64"], flatten=True)[0]
    out.binaries["renodx-dlss5.addon64"] = _binary(out.addon64_path, "renodx-dlss5.addon64", resolved)
    return out


class NgxBundle:
    def __init__(self) -> None:
        self.nr_version: str = ""
        self.nr_dll_path: Path | None = None
        self.sr_version: str = ""
        self.sr_dll_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def _resolve_rhi_manifest(
    log: LogFn,
    cache_dir: Path,
    progress: ProgressFn | None,
    force: bool,
) -> ResolvedArtifact:
    policy = _policy("rhi_manifest")
    adapter = _github()
    repository = policy.repository
    branch = policy.discovery.branch
    relative_path = policy.discovery.relative_path
    if repository is None or branch is None or relative_path is None:
        raise RuntimeError("RHI manifest policy is incomplete")

    def latest() -> Sequence[ArtifactCandidate]:
        snapshot = adapter.repository_snapshot(repository, branch)
        asset = adapter.repository_file(repository, snapshot.revision, relative_path)
        return (_artifact_candidate(adapter.name, asset),)

    return _resolver(log).resolve(
        "rhi_manifest",
        policy,
        cache_dir / "dlss_manifest.json",
        latest,
        stable_name=policy.stable_artifacts[0].name,
        progress=progress,
        force=force,
    )


def _resolve_ngx(
    component: str,
    entry: RhiManifestEntry,
    destination: Path,
    log: LogFn,
    progress: ProgressFn | None,
    force: bool,
) -> ResolvedArtifact:
    policy = _policy(component)
    name = Path(urlparse(entry.url).path).name
    candidate = ArtifactCandidate(
        provider=policy.provider,
        revision=f"{policy.discovery.release_tag_prefix or ''}{entry.version}",
        name=name,
        url=entry.url,
    )
    return _resolver(log).resolve(
        component,
        policy,
        destination,
        lambda: (candidate,),
        progress=progress,
        force=force,
    )


def fetch_ngx_dlls(
    log: LogFn,
    progress: ProgressFn | None = None,
    force: bool = False,
    *,
    include_sr: bool = True,
) -> NgxBundle:
    out = NgxBundle()
    cache_dir = get_cache_dir()
    manifest_result = _resolve_rhi_manifest(log, cache_dir, progress, force)
    manifest = RhiManifestPayload.model_validate_json(manifest_result.path.read_bytes())
    short_fuse = tuple(entry for entry in manifest.dlssnr if "SF" in entry.version)
    nr_entry = max(short_fuse or manifest.dlssnr, key=lambda entry: _version_key(entry.version))
    nr_result = _resolve_ngx("ngx_nr", nr_entry, cache_dir / "nvngx_dlssnr.zip", log, progress, force)
    out.nr_version = nr_result.revision.removeprefix("dlssnr-")
    out.warnings = manifest_result.warnings + nr_result.warnings
    out.nr_dll_path = zip_extract_matching(nr_result.path, cache_dir, ["*nvngx_dlssnr.dll"], flatten=True)[0]
    out.binaries["nvngx_dlssnr.dll"] = _binary(out.nr_dll_path, "nvngx_dlssnr.dll", nr_result)
    if include_sr:
        sr_entry = max(manifest.dlss, key=lambda entry: _version_key(entry.version))
        sr_result = _resolve_ngx("ngx_sr", sr_entry, cache_dir / "nvngx_dlss.zip", log, progress, force)
        out.sr_version = sr_result.revision.removeprefix("dlss-")
        out.warnings += sr_result.warnings
        out.sr_dll_path = zip_extract_matching(sr_result.path, cache_dir, ["*nvngx_dlss.dll"], flatten=True)[0]
        out.binaries["nvngx_dlss.dll"] = _binary(out.sr_dll_path, "nvngx_dlss.dll", sr_result)
    return out


class ReshadeBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.setup_exe_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def fetch_reshade(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> ReshadeBundle:
    out = ReshadeBundle()
    cache_dir = get_cache_dir()
    policy = _policy("reshade_addon")
    page_url = policy.discovery.page_url
    download_pattern = policy.discovery.download_pattern
    if page_url is None or download_pattern is None:
        raise RuntimeError("ReShade policy is incomplete")

    def latest() -> Sequence[ArtifactCandidate]:
        page = http_get_text(page_url)
        matches = re.findall(r"reshade_setup_([0-9.]+)_addon\.exe", page, re.IGNORECASE)
        if not matches:
            return ()
        version = max(matches, key=_version_key)
        url = download_pattern.format(version=version)
        return (
            ArtifactCandidate(
                provider=policy.provider,
                revision=version,
                name=Path(urlparse(url).path).name,
                url=url,
            ),
        )

    resolved = _resolver(log).resolve(
        "reshade_addon",
        policy,
        cache_dir / "ReShade_Setup_Addon.exe",
        latest,
        progress=progress,
        force=force,
    )
    out.version = resolved.revision
    out.setup_exe_path = resolved.path
    out.warnings = resolved.warnings
    out.binaries["ReShade_Setup"] = _binary(resolved.path, resolved.name, resolved)
    return out


class LumeniteBundle:
    def __init__(self) -> None:
        self.branch: str = "mainline"
        self.revision: str = ""
        self.staging_dir: Path | None = None
        self.files: list[Path] = []
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def fetch_lumenite(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> LumeniteBundle:
    out = LumeniteBundle()
    cache_dir = get_cache_dir()
    policy = _policy("lumenite")
    adapter = _github()
    repository = policy.repository
    branch = policy.discovery.branch
    if repository is None or branch is None:
        raise RuntimeError("LumeniteFX policy is incomplete")

    def latest() -> Sequence[ArtifactCandidate]:
        snapshot = adapter.repository_snapshot(repository, branch)
        archive = adapter.repository_archive(snapshot)
        return (_artifact_candidate(adapter.name, archive),)

    resolved = _resolver(log).resolve(
        "lumenite",
        policy,
        cache_dir / "LumeniteFX.zip",
        latest,
        progress=progress,
        force=force,
    )
    out.branch = branch
    out.revision = resolved.revision
    out.warnings = resolved.warnings
    out.binaries["LumeniteFX.zip"] = _binary(resolved.path, "LumeniteFX.zip", resolved)
    safe_revision = re.sub(r"[^A-Za-z0-9_.-]", "_", resolved.revision)[:24]
    staging = cache_dir / f"lumenite_stage_{safe_revision}"
    marker = staging / ".complete"
    with resource_lock(staging):
        if not marker.is_file():
            temporary = Path(tempfile.mkdtemp(prefix="lumenite-stage-", dir=cache_dir))
            try:
                with zipfile.ZipFile(resolved.path, "r") as archive:
                    for info in archive.infolist():
                        if info.is_dir():
                            continue
                        name = info.filename.replace("\\", "/")
                        parts = name.split("/", 1)
                        if len(parts) < 2:
                            continue
                        relative = parts[1]
                        if relative.startswith("Shaders/"):
                            destination = "reshade-shaders/Shaders/" + relative.removeprefix("Shaders/")
                        elif relative.startswith("Textures/"):
                            destination = "reshade-shaders/Textures/" + relative.removeprefix("Textures/")
                        else:
                            continue
                        target = safe_archive_destination(temporary, destination)
                        atomic_write_bytes(target, archive.read(info))
                atomic_write_text(temporary / ".complete", resolved.revision)
                backup = Path(tempfile.mkdtemp(prefix=f".{staging.name}.backup-", dir=cache_dir))
                backup.rmdir()
                try:
                    if staging.exists():
                        staging.replace(backup)
                    temporary.replace(staging)
                except Exception:
                    if not staging.exists() and backup.exists():
                        backup.replace(staging)
                    raise
                finally:
                    if backup.exists():
                        shutil.rmtree(backup, ignore_errors=True)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
    out.staging_dir = staging
    out.files = [path for path in staging.rglob("*") if path.is_file() and path != marker]
    log(f"Staged {len(out.files)} LumeniteFX files from {resolved.revision}.")
    return out


class ReshadeHeaders:
    def __init__(self) -> None:
        self.fxh_path: Path | None = None
        self.ui_fxh_path: Path | None = None
        self.drawtext_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def fetch_reshade_headers(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> ReshadeHeaders:
    out = ReshadeHeaders()
    cache_dir = get_cache_dir()
    policy = _policy("reshade_headers")
    adapter = _github()
    repository = policy.repository
    branch = policy.discovery.branch
    relative_paths = policy.discovery.relative_paths
    if repository is None or branch is None or not relative_paths:
        raise RuntimeError("ReShade headers policy is incomplete")
    snapshot: RepositorySnapshot | None = None
    snapshot_error: Exception | None = None

    def latest(relative_path: str) -> tuple[ArtifactCandidate, ...]:
        nonlocal snapshot, snapshot_error
        if snapshot is None:
            if snapshot_error is not None:
                raise snapshot_error
            try:
                snapshot = adapter.repository_snapshot(repository, branch)
            except Exception as error:
                snapshot_error = error
                raise
        return _repository_file_candidates(adapter, repository, snapshot.revision, relative_path)

    results: dict[str, ResolvedArtifact] = {}
    warnings: list[ResolutionWarning] = []
    for relative_path in relative_paths:
        name = Path(relative_path).name
        resolved = _resolver(log).resolve(
            "reshade_headers",
            policy,
            cache_dir / name,
            partial(latest, relative_path),
            stable_name=name,
            progress=progress,
            force=force,
        )
        results[name] = resolved
        warnings.extend(resolved.warnings)
        out.binaries[name] = _binary(resolved.path, name, resolved)
    out.fxh_path = results["ReShade.fxh"].path
    out.ui_fxh_path = results["ReShadeUI.fxh"].path
    out.drawtext_path = results["DrawText.fxh"].path
    out.warnings = tuple(warnings)
    return out


class DgvoodooBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.d3d9_dll: Path | None = None
        self.conf: Path | None = None
        self.cpl: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}
        self.warnings: tuple[ResolutionWarning, ...] = ()


def fetch_dgvoodoo(
    log: LogFn,
    progress: ProgressFn | None = None,
    force: bool = False,
    architecture: str = "x86",
) -> DgvoodooBundle:
    if architecture not in {"x86", "x64"}:
        raise ValueError(f"Unsupported dgVoodoo architecture: {architecture}")
    out = DgvoodooBundle()
    cache_dir = get_cache_dir()
    policy = _policy("dgvoodoo2")
    adapter = _github()
    repository = policy.repository
    if repository is None:
        raise RuntimeError("dgVoodoo2 policy has no repository")

    def latest() -> Sequence[ArtifactCandidate]:
        candidates = _release_candidates(adapter, repository)
        return tuple(
            candidate
            for candidate in candidates
            if "dbg" not in candidate.name.lower() and "dev" not in candidate.name.lower()
        )

    resolved = _resolver(log).resolve(
        "dgvoodoo2",
        policy,
        cache_dir / "dgVoodoo2.zip",
        latest,
        architecture=architecture,
        progress=progress,
        force=force,
    )
    out.version = resolved.revision
    out.warnings = resolved.warnings
    stage = cache_dir / f"dgvoodoo_{architecture}"
    out.d3d9_dll = zip_extract_matching(
        resolved.path,
        stage,
        [f"MS/{architecture}/D3D9.dll"],
        flatten=True,
    )[0]
    out.conf = zip_extract_matching(resolved.path, stage, ["dgVoodoo.conf"], flatten=True)[0]
    out.cpl = zip_extract_matching(resolved.path, stage, ["dgVoodooCpl.exe"], flatten=True)[0]
    for path in (out.d3d9_dll, out.conf, out.cpl):
        out.binaries[path.name] = _binary(path, path.name, resolved)
    return out
