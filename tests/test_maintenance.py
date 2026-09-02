import json
import shutil
import zipfile
from importlib import resources
from io import BytesIO
from pathlib import Path

import pytest

from dlss5_enabler.maintenance import generate_upstream_pin, update_manifest_pin
from dlss5_enabler.network.manifest import load_upstream_manifest, load_upstream_manifest_path


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_generate_upstream_pin_requires_explicit_immutable_revision() -> None:
    with pytest.raises(ValueError, match="explicit immutable"):
        generate_upstream_pin(
            component="lumenite",
            revision="mainline",
            name="LumeniteFX.zip",
            url="https://example.com/LumeniteFX.zip",
        )


def test_generate_and_explicitly_update_manifest_pin(tmp_path: Path) -> None:
    payload = _zip({"renodx-dlss5.addon64": b"addon"})
    calls: list[str] = []

    def download(url: str, destination: Path | str, _progress: object) -> Path:
        calls.append(url)
        path = Path(destination)
        path.write_bytes(payload)
        return path

    candidate = generate_upstream_pin(
        component="renodx_dlss5",
        revision="renodx-dlss5-test",
        name="renodx-dlss5_test.zip",
        url="https://example.com/renodx-dlss5_test.zip",
        manifest=load_upstream_manifest(),
        downloader=download,
    )
    source = resources.files("dlss5_enabler").joinpath("upstreams.json")
    manifest_path = tmp_path / "upstreams.json"
    with source.open("rb") as input_stream, manifest_path.open("wb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)

    updated = update_manifest_pin(manifest_path, candidate)

    assert calls == [candidate.url]
    assert candidate.format_version == 1
    assert candidate.size_bytes == len(payload)
    assert updated.components["renodx_dlss5"].stable_artifacts[0].revision == "renodx-dlss5-test"
    assert load_upstream_manifest_path(manifest_path) == updated
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw["components"]["renodx_dlss5"]["stable_artifacts"][0]["sha256"] == candidate.sha256
