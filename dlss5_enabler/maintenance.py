from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from dlss5_enabler.core.fileio import atomic_write_text
from dlss5_enabler.network.http import http_download_file
from dlss5_enabler.network.manifest import (
    ComponentPolicy,
    EmbeddedUpstreamManifest,
    PinnedArtifact,
    load_upstream_manifest,
    load_upstream_manifest_path,
)
from dlss5_enabler.network.resolver import ArtifactCandidate, ArtifactValidator, ProgressFn

DownloadFn = Callable[[str, Path | str, ProgressFn | None], Path]


class UpstreamPinCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str
    revision: str
    name: str
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    asset_id: int | None = Field(default=None, ge=1)
    format_version: int | None = Field(default=None, ge=1)


def _download(url: str, destination: Path | str, progress: ProgressFn | None) -> Path:
    return http_download_file(url, destination, progress_fn=progress)


def generate_upstream_pin(
    component: str,
    revision: str,
    name: str,
    url: str,
    architecture: str | None = None,
    asset_id: int | None = None,
    manifest: EmbeddedUpstreamManifest | None = None,
    downloader: DownloadFn = _download,
) -> UpstreamPinCandidate:
    selected_manifest = manifest or load_upstream_manifest()
    if component not in selected_manifest.components:
        raise ValueError(f"Unknown upstream component: {component}")
    policy = selected_manifest.components[component]
    mutable_revisions = {"latest", "head"}
    if policy.discovery.branch:
        mutable_revisions.add(policy.discovery.branch.casefold())
    if not revision.strip() or revision.casefold() in mutable_revisions:
        raise ValueError("An explicit immutable tag, version, or commit is required")
    candidate = ArtifactCandidate(
        provider=policy.provider,
        revision=revision,
        name=name,
        url=url,
        asset_id=asset_id,
    )
    with tempfile.TemporaryDirectory(prefix=f"dlss5-enabler-pin-{component}-") as temporary:
        destination = Path(temporary) / name
        downloaded = downloader(url, destination, None)
        digest, size, format_version = ArtifactValidator().validate(
            downloaded,
            policy,
            candidate,
            architecture,
        )
    return UpstreamPinCandidate(
        component=component,
        revision=revision,
        name=name,
        url=url,
        sha256=digest,
        size_bytes=size,
        asset_id=asset_id,
        format_version=format_version,
    )


def update_manifest_pin(path: Path, candidate: UpstreamPinCandidate) -> EmbeddedUpstreamManifest:
    manifest = load_upstream_manifest_path(path)
    policy = manifest.components[candidate.component]
    updated_artifact = PinnedArtifact(
        revision=candidate.revision,
        name=candidate.name,
        url=candidate.url,
        sha256=candidate.sha256,
        size_bytes=candidate.size_bytes,
        asset_id=candidate.asset_id,
    )
    stable_artifacts = _replace_stable_artifact(policy, updated_artifact)
    components = dict(manifest.components)
    components[candidate.component] = policy.model_copy(update={"stable_artifacts": stable_artifacts})
    updated = EmbeddedUpstreamManifest(schema_version=manifest.schema_version, components=components)
    serialized = updated.model_dump_json(indent=2, exclude_none=True, exclude_defaults=True) + "\n"
    atomic_write_text(path, serialized)
    return updated


def _replace_stable_artifact(
    policy: ComponentPolicy,
    artifact: PinnedArtifact,
) -> tuple[PinnedArtifact, ...]:
    if len(policy.stable_artifacts) == 1:
        return (artifact,)
    matches = tuple(
        index for index, item in enumerate(policy.stable_artifacts) if item.name.casefold() == artifact.name.casefold()
    )
    if len(matches) != 1:
        raise ValueError(f"Stable artifact {artifact.name} is not uniquely defined")
    updated = list(policy.stable_artifacts)
    updated[matches[0]] = artifact
    return tuple(updated)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dlss5-enabler-update-upstream")
    parser.add_argument("component")
    parser.add_argument("revision")
    parser.add_argument("name")
    parser.add_argument("url")
    parser.add_argument("--architecture", choices=("x86", "x64"))
    parser.add_argument("--asset-id", type=int)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("upstreams.json"))
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    candidate = generate_upstream_pin(
        component=args.component,
        revision=args.revision,
        name=args.name,
        url=args.url,
        architecture=args.architecture,
        asset_id=args.asset_id,
    )
    sys.stdout.write(json.dumps(candidate.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
    if args.write:
        update_manifest_pin(args.manifest, candidate)
        sys.stdout.write(f"Updated {args.manifest}\n")


if __name__ == "__main__":
    main()
