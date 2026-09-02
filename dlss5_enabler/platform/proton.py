import os
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from dlss5_enabler.core.fileio import atomic_write_text, resource_lock


class SteamPrefixInfo(BaseModel):
    appid: str
    prefix_path: Path
    game_dir: Path
    user_reg_path: Path = Field(default_factory=Path)
    system_reg_path: Path = Field(default_factory=Path)

    def model_post_init(self, __context: Any) -> None:
        if not str(self.user_reg_path) or str(self.user_reg_path) == ".":
            self.user_reg_path = self.prefix_path / "user.reg"
        if not str(self.system_reg_path) or str(self.system_reg_path) == ".":
            self.system_reg_path = self.prefix_path / "system.reg"


class WineRegParser:
    SECTION_HEADER = r"\[Software\\+Wine\\+DllOverrides\](?:\s+(\d+)\s+(\d+))?"

    @staticmethod
    def _normalize_dll_key(name: str) -> str:
        k = name.strip().lower()
        if k.endswith(".dll"):
            k = k[:-4]
        return k

    @classmethod
    def read_overrides(cls, reg_path: Path | str) -> dict[str, str]:
        p = Path(reg_path)
        try:
            with resource_lock(p):
                return cls._read_overrides_unlocked(p)
        except Exception:
            return {}

    @classmethod
    def _read_overrides_unlocked(cls, p: Path) -> dict[str, str]:
        if not p.is_file():
            return {}
        content = p.read_text(encoding="utf-8", errors="strict")
        lines = content.splitlines()

        in_section = False
        overrides: dict[str, str] = {}

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";;")):
                continue

            if stripped.startswith("["):
                in_section = bool(re.match(cls.SECTION_HEADER, stripped, re.IGNORECASE))
                continue

            if in_section:
                m = re.match(r'^"([^"]+)"="([^"]*)"$', stripped)
                if m:
                    key = cls._normalize_dll_key(m.group(1))
                    val = m.group(2)
                    overrides[key] = val

        return overrides

    @classmethod
    def _write_overrides_unlocked(cls, p: Path, updates: dict[str, str | None]) -> None:
        content = p.read_text(encoding="utf-8", errors="strict") if p.is_file() else "WINE REGISTRY Version 2\n"
        lines = content.splitlines()

        section_idx: int = -1
        section_end_idx: int = -1
        ts_now = int(time.time())

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("["):
                if re.match(cls.SECTION_HEADER, stripped, re.IGNORECASE):
                    section_idx = idx
                elif section_idx != -1 and section_end_idx == -1:
                    section_end_idx = idx
                    break

        if section_idx != -1 and section_end_idx == -1:
            section_end_idx = len(lines)

        existing: dict[str, str] = {}
        if section_idx != -1:
            for section_line in lines[section_idx + 1 : section_end_idx]:
                match = re.match(r'^"([^"]+)"="([^"]*)"$', section_line.strip())
                if match:
                    existing[cls._normalize_dll_key(match.group(1))] = match.group(2)

        for key, value in updates.items():
            normalized = cls._normalize_dll_key(key)
            if value is None:
                existing.pop(normalized, None)
            else:
                existing[normalized] = value

        if section_idx == -1 and not existing:
            return

        if section_idx == -1:
            new_lines = list(lines)
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.append(f"[Software\\\\Wine\\\\DllOverrides] {ts_now} 0")
            for k, v in sorted(existing.items()):
                new_lines.append(f'"{k}"="{v}"')
            new_lines.append("")
        else:
            formatted_section = [f"[Software\\\\Wine\\\\DllOverrides] {ts_now} 0"]
            for k, v in sorted(existing.items()):
                formatted_section.append(f'"{k}"="{v}"')
            new_lines = lines[:section_idx] + formatted_section + lines[section_end_idx:]
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, "\n".join(new_lines) + "\n")

    @classmethod
    def set_overrides_with_originals(
        cls, reg_path: Path | str, overrides: dict[str, str]
    ) -> tuple[bool, dict[str, tuple[bool, str]]]:
        if not overrides:
            return True, {}
        p = Path(reg_path)
        try:
            with resource_lock(p):
                existing = cls._read_overrides_unlocked(p)
                originals = {
                    cls._normalize_dll_key(key): (
                        cls._normalize_dll_key(key) in existing,
                        existing.get(cls._normalize_dll_key(key), ""),
                    )
                    for key in overrides
                }
                cls._write_overrides_unlocked(p, dict(overrides))
            return True, originals
        except Exception:
            return False, {}

    @classmethod
    def set_overrides(cls, reg_path: Path | str, overrides: dict[str, str]) -> bool:
        ok, _originals = cls.set_overrides_with_originals(reg_path, overrides)
        return ok

    @classmethod
    def remove_overrides(cls, reg_path: Path | str, dll_names: list[str]) -> bool:
        return cls.restore_overrides(reg_path, dict.fromkeys(dll_names))

    @classmethod
    def restore_overrides(cls, reg_path: Path | str, originals: dict[str, str | None]) -> bool:
        p = Path(reg_path)
        try:
            with resource_lock(p):
                if not p.is_file() and all(value is None for value in originals.values()):
                    return True
                cls._write_overrides_unlocked(p, originals)
            return True
        except Exception:
            return False


class ProtonManager:
    STEAM_ROOT_CANDIDATES: ClassVar[list[Path]] = [
        Path.home() / ".local" / "share" / "Steam",
        Path.home() / ".steam" / "steam",
        Path.home() / ".steam" / "root",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".steam" / "steam",
        Path.home() / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
    ]

    @staticmethod
    def _valid_prefix(path: Path) -> bool:
        return path.is_dir() and (path / "user.reg").is_file()

    @classmethod
    def get_steam_libraries(cls) -> list[Path]:
        libraries: list[Path] = []
        for root in cls.STEAM_ROOT_CANDIDATES:
            if not root.is_dir():
                continue
            if root not in libraries:
                libraries.append(root)

            vdf_paths = [
                root / "steamapps" / "libraryfolders.vdf",
                root / "config" / "libraryfolders.vdf",
            ]
            for vdf in vdf_paths:
                if vdf.is_file():
                    try:
                        content = vdf.read_text(encoding="utf-8", errors="ignore")
                        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
                            lib_path = Path(match.group(1).replace("\\\\", "/"))
                            if lib_path.is_dir() and lib_path not in libraries:
                                libraries.append(lib_path)
                    except Exception:
                        pass

        return libraries

    @classmethod
    def _find_appid_from_manifests(cls, steamapps_dir: Path, target_dir_name: str) -> str:
        if not steamapps_dir.is_dir():
            return ""

        for manifest in steamapps_dir.glob("appmanifest_*.acf"):
            try:
                content = manifest.read_text(encoding="utf-8", errors="ignore")
                m_install = re.search(r'"installdir"\s+"([^"]+)"', content)
                if m_install and m_install.group(1).lower() == target_dir_name.lower():
                    m_appid = re.search(r'"appid"\s+"([^"]+)"', content)
                    if m_appid:
                        return m_appid.group(1)
            except Exception:
                pass
        return ""

    @classmethod
    def find_prefix_for_game(cls, game_exe_or_dir: Path | str) -> SteamPrefixInfo | None:
        target = Path(game_exe_or_dir).resolve()
        game_dir = target if target.is_dir() else target.parent

        env_compat = os.environ.get("STEAM_COMPAT_DATA_PATH")
        if env_compat:
            compat_p = Path(env_compat)
            pfx = compat_p / "pfx"
            if cls._valid_prefix(pfx):
                appid = os.environ.get("STEAM_COMPAT_APP_ID") or compat_p.name
                return SteamPrefixInfo(appid=appid, prefix_path=pfx, game_dir=game_dir)

        env_wineprefix = os.environ.get("WINEPREFIX")
        if env_wineprefix:
            pfx = Path(env_wineprefix)
            if cls._valid_prefix(pfx):
                return SteamPrefixInfo(appid="wine", prefix_path=pfx, game_dir=game_dir)

        target_parts = list(game_dir.parts)
        for i, part in enumerate(target_parts):
            if part.lower() == "common" and i > 0 and target_parts[i - 1].lower() == "steamapps":
                steamapps_dir = Path(*target_parts[:i])
                game_folder_name = target_parts[i + 1] if len(target_parts) > i + 1 else game_dir.name
                appid = cls._find_appid_from_manifests(steamapps_dir, game_folder_name)
                if appid:
                    compat_pfx = steamapps_dir / "compatdata" / appid / "pfx"
                    if cls._valid_prefix(compat_pfx):
                        return SteamPrefixInfo(appid=appid, prefix_path=compat_pfx, game_dir=game_dir)

        libraries = cls.get_steam_libraries()
        for lib in libraries:
            steamapps_dir = lib / "steamapps"
            appid = cls._find_appid_from_manifests(steamapps_dir, game_dir.name)
            if appid:
                compat_pfx = steamapps_dir / "compatdata" / appid / "pfx"
                if cls._valid_prefix(compat_pfx):
                    return SteamPrefixInfo(appid=appid, prefix_path=compat_pfx, game_dir=game_dir)

        return None

    @classmethod
    def inject_overrides(cls, prefix: SteamPrefixInfo | Path | str, overrides: dict[str, str]) -> list[str]:
        pfx_path = prefix.prefix_path if isinstance(prefix, SteamPrefixInfo) else Path(prefix)
        if not cls._valid_prefix(pfx_path):
            return []
        user_reg = pfx_path / "user.reg"

        ok = WineRegParser.set_overrides(user_reg, overrides)
        if ok:
            return list(overrides.keys())
        return []

    @classmethod
    def inject_overrides_with_originals(
        cls, prefix: SteamPrefixInfo | Path | str, overrides: dict[str, str]
    ) -> tuple[list[str], dict[str, tuple[bool, str]]]:
        pfx_path = prefix.prefix_path if isinstance(prefix, SteamPrefixInfo) else Path(prefix)
        if not cls._valid_prefix(pfx_path):
            return [], {}
        user_reg = pfx_path / "user.reg"
        ok, originals = WineRegParser.set_overrides_with_originals(user_reg, overrides)
        return (list(overrides.keys()), originals) if ok else ([], {})

    @classmethod
    def revert_overrides(cls, prefix: SteamPrefixInfo | Path | str, dll_names: list[str]) -> bool:
        pfx_path = prefix.prefix_path if isinstance(prefix, SteamPrefixInfo) else Path(prefix)
        if not cls._valid_prefix(pfx_path):
            return False
        user_reg = pfx_path / "user.reg"

        return WineRegParser.remove_overrides(user_reg, dll_names)

    @classmethod
    def get_launch_options(cls, overrides: list[str] | dict[str, str] | None = None) -> str:
        if isinstance(overrides, dict):
            dll_keys = list(overrides.keys())
        elif isinstance(overrides, list):
            dll_keys = overrides
        else:
            dll_keys = ["dxgi"]

        normalized = [WineRegParser._normalize_dll_key(k) for k in dll_keys if k.strip()]
        if not normalized:
            normalized = ["dxgi"]

        joined = ",".join(normalized)
        return f'WINEDLLOVERRIDES="{joined}=n,b" %command%'
