from pathlib import Path, PurePosixPath


def safe_archive_destination(root: Path | str, member: str, flatten: bool = False) -> Path:
    base = Path(root).resolve()
    normalized = member.replace("\\", "/")
    archive_path = PurePosixPath(normalized)
    unsafe_part = any(part in {"", ".", ".."} for part in archive_path.parts)
    if archive_path.is_absolute() or not archive_path.parts or unsafe_part:
        raise ValueError(f"Unsafe archive member path: {member}")
    relative = Path(archive_path.name) if flatten else Path(*archive_path.parts)
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"Archive member escapes destination: {member}")
    return target
