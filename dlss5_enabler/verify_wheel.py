from __future__ import annotations

import sys
import tarfile
import zipfile
from collections.abc import Sequence
from email.parser import BytesParser
from pathlib import Path

from packaging.utils import parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

MANIFEST_MEMBER = "dlss5_enabler/upstreams.json"


def verify_wheels(directory: Path) -> tuple[Path, ...]:
    wheels = tuple(sorted(directory.glob("*.whl")))
    if not wheels:
        raise RuntimeError(f"No wheel found in {directory}")
    for wheel in wheels:
        with zipfile.ZipFile(wheel, "r") as archive:
            if MANIFEST_MEMBER not in archive.namelist():
                raise RuntimeError(f"{wheel.name} does not contain {MANIFEST_MEMBER}")
            if not archive.read(MANIFEST_MEMBER):
                raise RuntimeError(f"{wheel.name} contains an empty {MANIFEST_MEMBER}")
    return wheels


def _metadata_version(payload: bytes, artifact: Path) -> Version:
    value = BytesParser().parsebytes(payload).get("Version")
    if value is None:
        raise RuntimeError(f"{artifact.name} package metadata has no Version field")
    return Version(value)


def verify_distribution_versions(directory: Path) -> Version:
    wheels = tuple(sorted(directory.glob("*.whl")))
    source_distributions = tuple(sorted(directory.glob("*.tar.gz")))
    if len(wheels) != 1 or len(source_distributions) != 1:
        raise RuntimeError("Expected exactly one wheel and one source distribution")
    wheel = wheels[0]
    source_distribution = source_distributions[0]
    wheel_name, wheel_version, _build, _tags = parse_wheel_filename(wheel.name)
    source_name, source_version = parse_sdist_filename(source_distribution.name)
    if wheel_name != source_name or wheel_version != source_version:
        raise RuntimeError("Wheel and source distribution names or versions do not match")
    with zipfile.ZipFile(wheel, "r") as archive:
        wheel_metadata_members = tuple(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        if len(wheel_metadata_members) != 1:
            raise RuntimeError(f"{wheel.name} does not contain exactly one METADATA file")
        if _metadata_version(archive.read(wheel_metadata_members[0]), wheel) != wheel_version:
            raise RuntimeError(f"{wheel.name} metadata version does not match its filename")
    with tarfile.open(source_distribution, "r:gz") as archive:
        source_metadata_members = tuple(member for member in archive.getmembers() if member.name.endswith("/PKG-INFO"))
        if len(source_metadata_members) != 1:
            raise RuntimeError(f"{source_distribution.name} does not contain exactly one PKG-INFO file")
        stream = archive.extractfile(source_metadata_members[0])
        if stream is None or _metadata_version(stream.read(), source_distribution) != source_version:
            raise RuntimeError(f"{source_distribution.name} metadata version does not match its filename")
    return wheel_version


def main(argv: Sequence[str] | None = None) -> None:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    directory = Path(arguments[0]) if arguments else Path("dist")
    wheels = verify_wheels(directory)
    distribution_version = verify_distribution_versions(directory)
    sys.stdout.write(
        "Verified embedded upstream manifest and distribution version "
        f"{distribution_version} in " + ", ".join(path.name for path in wheels) + "\n"
    )


if __name__ == "__main__":
    main()
