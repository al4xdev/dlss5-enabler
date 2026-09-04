import json
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from dlss5_enabler.network.manifest import EXPECTED_COMPONENTS, load_upstream_manifest, load_upstream_manifest_path

ManifestMutation = Callable[[dict[str, Any]], object]
INVALID_MANIFEST_CASES: list[tuple[ManifestMutation, str]] = [
    (lambda raw: raw.update(schema_version=2), "unsupported upstream manifest schema"),
    (lambda raw: raw.update(components={}), "missing components"),
    (lambda raw: raw["components"].pop("feeder"), "missing components: feeder"),
    (lambda raw: raw["components"].update(unknown=raw["components"]["feeder"]), "unknown components: unknown"),
    (lambda raw: raw.update(unexpected=True), "Extra inputs are not permitted"),
    (
        lambda raw: raw["components"]["feeder"]["stable_artifacts"][0].update(sha256="bad"),
        "String should match pattern",
    ),
    (
        lambda raw: raw["components"]["feeder"]["stable_artifacts"][0].update(sha256="1" * 64),
        "cannot be a placeholder",
    ),
    (
        lambda raw: raw["components"]["feeder"]["stable_artifacts"][0].update(url="http://example.com/a.zip"),
        "upstream URL must be unauthenticated HTTPS",
    ),
    (
        lambda raw: raw["components"]["lumenite"]["stable_artifacts"][0].update(revision="mainline"),
        "repository archive fallback requires an immutable",
    ),
]


def _raw_manifest() -> dict[str, Any]:
    resource = resources.files("dlss5_enabler").joinpath("upstreams.json")
    return cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))


def test_embedded_manifest_loads_every_component() -> None:
    manifest = load_upstream_manifest()

    assert manifest.schema_version == 1
    assert manifest.components.keys() == EXPECTED_COMPONENTS
    assert all(policy.stable_artifacts for policy in manifest.components.values())
    assert all(
        len(set(artifact.sha256)) > 1 for policy in manifest.components.values() for artifact in policy.stable_artifacts
    )


def test_reshade_header_fallback_pins_match_the_verified_snapshot() -> None:
    headers = load_upstream_manifest().components["reshade_headers"].stable_artifacts

    assert {artifact.name: artifact.sha256 for artifact in headers} == {
        "ReShade.fxh": "6dabfbbaf968c3871905d2ea17f96572ff7b1cec01310b5d0e5252b66b30174f",
        "ReShadeUI.fxh": "78adf672df47460297eb9fe6dd238d2aafa24510b52b84feb1a745dff70eb901",
        "DrawText.fxh": "b79cc4dfb3e98bcf4c06193d00ea7631d74f467f73a4deeeee13e71336d3e680",
    }


def test_manifest_resource_is_available_outside_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    manifest = load_upstream_manifest()

    assert manifest.components["feeder"].repository == "jlrouzies-fr/DLSS5-Feeder"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    INVALID_MANIFEST_CASES,
)
def test_manifest_rejects_invalid_data(tmp_path: Path, mutation: ManifestMutation, expected: str) -> None:
    raw = _raw_manifest()
    mutation(raw)
    path = tmp_path / "upstreams.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match=expected):
        load_upstream_manifest_path(path)


def test_manifest_rejects_unknown_archive_architecture(tmp_path: Path) -> None:
    raw = _raw_manifest()
    raw["components"]["dgvoodoo2"]["formats"][0]["architecture_members"]["arm64"] = ["MS/arm64/D3D9.dll"]
    path = tmp_path / "upstreams.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValidationError, match="archive architecture must be x86 or x64"):
        load_upstream_manifest_path(path)
