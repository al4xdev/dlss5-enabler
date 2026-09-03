import re
from dataclasses import dataclass
from pathlib import Path

from dlss5_enabler.core.fileio import atomic_write_bytes, resource_lock

_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass
class _IniDocument:
    lines: list[str]
    newline: str
    trailing_newline: bool
    has_bom: bool


def _load_ini_document(path: Path) -> _IniDocument | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    has_bom = data.startswith(_UTF8_BOM)
    text = data.removeprefix(_UTF8_BOM).decode("utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in text else ("\r" if "\r" in text else "\n")
    return _IniDocument(
        lines=text.splitlines(),
        newline=newline,
        trailing_newline=text.endswith(("\r", "\n")),
        has_bom=has_bom,
    )


def _save_ini_document(path: Path, document: _IniDocument) -> None:
    text = document.newline.join(document.lines)
    if document.trailing_newline:
        text += document.newline
    prefix = _UTF8_BOM if document.has_bom else b""
    atomic_write_bytes(path, prefix + text.encode("utf-8"))


def ini_get_exact(ini_path: Path | str, section: str, key: str) -> tuple[bool, str]:
    path = Path(ini_path)
    if not path.is_file():
        return False, ""

    try:
        with resource_lock(path):
            document = _load_ini_document(path)
    except Exception:
        return False, ""
    if document is None:
        return False, ""

    in_section = False
    section_pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
    any_section_pattern = re.compile(r"^\s*\[.*\]\s*$")

    for line in document.lines:
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
            if path.is_file():
                document = _load_ini_document(path)
                if document is None:
                    return False
            else:
                document = _IniDocument(lines=[], newline="\n", trailing_newline=True, has_bom=False)

            section_pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$", re.IGNORECASE)
            any_section_pattern = re.compile(r"^\s*\[.*\]\s*$")

            in_target_section = False
            section_found = False
            key_found = False
            new_lines: list[str] = []
            inserted = False

            for line in document.lines:
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

            document.lines = new_lines
            path.parent.mkdir(parents=True, exist_ok=True)
            _save_ini_document(path, document)
        return True
    except Exception:
        return False
