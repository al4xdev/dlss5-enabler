import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from dlss5_enabler.verify_wheel import MANIFEST_MEMBER, verify_distribution_versions, verify_wheels


def test_verify_built_wheel_manifest_resource(tmp_path: Path) -> None:
    wheel = tmp_path / "dlss5_enabler-1.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, b'{"schema_version":1}')

    assert verify_wheels(tmp_path) == (wheel,)


def test_verify_built_wheel_rejects_missing_resource(tmp_path: Path) -> None:
    wheel = tmp_path / "dlss5_enabler-1.0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dlss5_enabler/__init__.py", b"")

    with pytest.raises(RuntimeError, match=r"upstreams\.json"):
        verify_wheels(tmp_path)


def test_verify_distribution_versions_match(tmp_path: Path) -> None:
    wheel = tmp_path / "dlss5_enabler-1.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dlss5_enabler-1.1.0.dist-info/METADATA", b"Name: dlss5-enabler\nVersion: 1.1.0\n")
    source_distribution = tmp_path / "dlss5_enabler-1.1.0.tar.gz"
    payload = b"Name: dlss5-enabler\nVersion: 1.1.0\n"
    member = tarfile.TarInfo("dlss5_enabler-1.1.0/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(source_distribution, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))

    assert str(verify_distribution_versions(tmp_path)) == "1.1.0"


def test_verify_distribution_versions_reject_metadata_mismatch(tmp_path: Path) -> None:
    wheel = tmp_path / "dlss5_enabler-1.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dlss5_enabler-1.1.0.dist-info/METADATA", b"Name: dlss5-enabler\nVersion: 1.0.1\n")
    source_distribution = tmp_path / "dlss5_enabler-1.1.0.tar.gz"
    payload = b"Name: dlss5-enabler\nVersion: 1.1.0\n"
    member = tarfile.TarInfo("dlss5_enabler-1.1.0/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(source_distribution, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="metadata version"):
        verify_distribution_versions(tmp_path)
