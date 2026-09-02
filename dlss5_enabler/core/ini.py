import re
from pathlib import Path

from dlss5_enabler.core.fileio import atomic_write_text, resource_lock


def ini_get_exact(ini_path: Path | str, section: str, key: str) -> tuple[bool, str]:
    path = Path(ini_path)
    if not path.is_file():
        return False, ""

    try:
        with resource_lock(path):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False, ""

    in_section = False
    section_pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_pattern = re.compile(r"^\s*\[.*\]\s*$")

    for line in lines:
        stripped = line.strip()
        if any_section_pattern.match(stripped):
            in_section = bool(section_pattern.match(stripped))
            continue

        if in_section:
            if stripped.startswith((";", "#")) or not stripped:
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return True, v.strip()

    return False, ""


def ini_set_exact(ini_path: Path | str, section: str, key: str, value: str) -> bool:
    path = Path(ini_path)
    try:
        with resource_lock(path):
            lines: list[str] = []
            if path.is_file():
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    return False

            section_pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
            any_section_pattern = re.compile(r"^\s*\[.*\]\s*$")

            in_target_section = False
            section_found = False
            key_found = False
            new_lines: list[str] = []
            inserted = False

            for line in lines:
                stripped = line.strip()
                if any_section_pattern.match(stripped):
                    if in_target_section and not key_found and value:
                        new_lines.append(f"{key}={value}")
                        inserted = True
                        key_found = True
                    in_target_section = bool(section_pattern.match(stripped))
                    if in_target_section:
                        section_found = True
                    new_lines.append(line)
                    continue

                if in_target_section and "=" in line and not stripped.startswith((";", "#")):
                    k, _ = line.split("=", 1)
                    if k.strip() == key:
                        key_found = True
                        if value:
                            new_lines.append(f"{key}={value}")
                        continue

                new_lines.append(line)

            if in_target_section and not key_found and value and not inserted:
                new_lines.append(f"{key}={value}")
                key_found = True

            if not section_found and value:
                if new_lines and new_lines[-1].strip():
                    new_lines.append("")
                new_lines.append(f"[{section}]")
                new_lines.append(f"{key}={value}")

            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, "\n".join(new_lines) + "\n")
        return True
    except Exception:
        return False
