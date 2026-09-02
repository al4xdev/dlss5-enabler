import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from dlss5_enabler.platform.proton import ProtonManager, SteamPrefixInfo, WineRegParser


def test_wine_reg_parser_read_empty(tmp_path: Path) -> None:
    non_existent = tmp_path / "user.reg"
    assert WineRegParser.read_overrides(non_existent) == {}


def test_wine_reg_parser_read_existing(tmp_path: Path) -> None:
    reg_file = tmp_path / "user.reg"
    content = (
        "WINE REGISTRY Version 2\n"
        ";; All keys relative to \\User\\S-1-5-21-0-0-0-1000\n\n"
        "[Software\\\\Wine\\\\DllOverrides] 1718000000 0\n"
        "#time=1db89b251234567\n"
        '"d3d9"="native,builtin"\n'
        '"dxgi.dll"="native,builtin"\n'
        '"winegstreamer"=""\n\n'
        "[Software\\\\Wine\\\\Drivers] 1718000000 0\n"
        '"Audio"="alsa"\n'
    )
    reg_file.write_text(content, encoding="utf-8")

    overrides = WineRegParser.read_overrides(reg_file)
    assert overrides["d3d9"] == "native,builtin"
    assert overrides["dxgi"] == "native,builtin"
    assert overrides["winegstreamer"] == ""
    assert "audio" not in overrides


def test_wine_reg_parser_set_overrides_new_file(tmp_path: Path) -> None:
    reg_file = tmp_path / "pfx" / "user.reg"
    ok = WineRegParser.set_overrides(reg_file, {"dxgi": "native,builtin", "d3d9": "native,builtin"})
    assert ok
    assert reg_file.is_file()

    read = WineRegParser.read_overrides(reg_file)
    assert read["dxgi"] == "native,builtin"
    assert read["d3d9"] == "native,builtin"


def test_wine_reg_parser_set_overrides_existing(tmp_path: Path) -> None:
    reg_file = tmp_path / "user.reg"
    content = (
        "WINE REGISTRY Version 2\n\n"
        "[Software\\\\Wine\\\\DllOverrides] 1700000000 0\n"
        '"old_dll"="builtin"\n\n'
        "[Software\\\\Wine\\\\OtherSection] 1700000000 0\n"
        '"key"="value"\n'
    )
    reg_file.write_text(content, encoding="utf-8")

    ok = WineRegParser.set_overrides(reg_file, {"dxgi": "native,builtin"})
    assert ok

    read = WineRegParser.read_overrides(reg_file)
    assert read["old_dll"] == "builtin"
    assert read["dxgi"] == "native,builtin"

    updated_text = reg_file.read_text(encoding="utf-8")
    assert r"[Software\\Wine\\OtherSection]" in updated_text


def test_wine_reg_parser_remove_overrides(tmp_path: Path) -> None:
    reg_file = tmp_path / "user.reg"
    content = (
        "WINE REGISTRY Version 2\n\n"
        "[Software\\\\Wine\\\\DllOverrides] 1700000000 0\n"
        '"dxgi"="native,builtin"\n'
        '"d3d9"="native,builtin"\n'
        '"other"="builtin"\n'
    )
    reg_file.write_text(content, encoding="utf-8")

    ok = WineRegParser.remove_overrides(reg_file, ["dxgi", "d3d9"])
    assert ok

    read = WineRegParser.read_overrides(reg_file)
    assert "dxgi" not in read
    assert "d3d9" not in read
    assert read.get("other") == "builtin"


def test_wine_reg_parser_restores_original_override(tmp_path: Path) -> None:
    reg_file = tmp_path / "user.reg"
    reg_file.write_text(
        'WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\DllOverrides] 1 0\n"dxgi"="builtin"\n',
        encoding="utf-8",
    )

    ok, originals = WineRegParser.set_overrides_with_originals(reg_file, {"dxgi": "native,builtin"})
    assert ok
    assert originals["dxgi"] == (True, "builtin")
    assert WineRegParser.restore_overrides(reg_file, {"dxgi": originals["dxgi"][1]})
    assert WineRegParser.read_overrides(reg_file)["dxgi"] == "builtin"


def test_wine_reg_parser_concurrent_updates_preserve_both(tmp_path: Path) -> None:
    reg_file = tmp_path / "user.reg"
    reg_file.write_text("WINE REGISTRY Version 2\n", encoding="utf-8")

    def set_override(item: str) -> bool:
        return WineRegParser.set_overrides(reg_file, {item: "native,builtin"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(set_override, ["dxgi", "d3d9"]))

    assert all(results)
    assert WineRegParser.read_overrides(reg_file) == {"d3d9": "native,builtin", "dxgi": "native,builtin"}


def test_steam_prefix_info_paths(tmp_path: Path) -> None:
    pfx_dir = tmp_path / "compatdata" / "12345" / "pfx"
    info = SteamPrefixInfo(appid="12345", prefix_path=pfx_dir, game_dir=tmp_path / "game")
    assert info.user_reg_path == pfx_dir / "user.reg"
    assert info.system_reg_path == pfx_dir / "system.reg"


def test_proton_manager_env_steam_compat_data_path(tmp_path: Path) -> None:
    pfx_dir = tmp_path / "compat" / "9999" / "pfx"
    pfx_dir.mkdir(parents=True, exist_ok=True)
    (pfx_dir / "user.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)

    with patch.dict(os.environ, {"STEAM_COMPAT_DATA_PATH": str(tmp_path / "compat" / "9999")}):
        info = ProtonManager.find_prefix_for_game(game_dir / "game.exe")
        assert info is not None
        assert info.appid == "9999"
        assert info.prefix_path == pfx_dir


def test_proton_manager_rejects_invalid_compat_path(tmp_path: Path) -> None:
    invalid = tmp_path / "compat" / "9999"
    invalid.mkdir(parents=True)
    with (
        patch.dict(os.environ, {"STEAM_COMPAT_DATA_PATH": str(invalid)}, clear=True),
        patch.object(ProtonManager, "get_steam_libraries", return_value=[]),
    ):
        assert ProtonManager.find_prefix_for_game(tmp_path / "game.exe") is None


def test_proton_manager_env_wineprefix(tmp_path: Path) -> None:
    wine_pfx = tmp_path / "winepfx"
    wine_pfx.mkdir(parents=True, exist_ok=True)
    (wine_pfx / "user.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
    game_dir = tmp_path / "game"
    game_dir.mkdir(parents=True, exist_ok=True)

    with patch.dict(os.environ, {"WINEPREFIX": str(wine_pfx)}, clear=True):
        info = ProtonManager.find_prefix_for_game(game_dir / "game.exe")
        assert info is not None
        assert info.appid == "wine"
        assert info.prefix_path == wine_pfx


def test_proton_manager_parent_traversal(tmp_path: Path) -> None:
    steamapps = tmp_path / "SteamLibrary" / "steamapps"
    game_dir = steamapps / "common" / "Cyberpunk 2077" / "bin" / "x64"
    game_dir.mkdir(parents=True, exist_ok=True)
    game_exe = game_dir / "Cyberpunk2077.exe"
    game_exe.write_bytes(b"dummy")

    manifest = steamapps / "appmanifest_1091500.acf"
    manifest.write_text(
        '"AppState"\n{\n\t"appid"\t\t"1091500"\n\t"installdir"\t\t"Cyberpunk 2077"\n}\n',
        encoding="utf-8",
    )

    pfx_dir = steamapps / "compatdata" / "1091500" / "pfx"
    pfx_dir.mkdir(parents=True, exist_ok=True)
    (pfx_dir / "user.reg").write_text("WINE REGISTRY Version 2\n", encoding="utf-8")

    with patch.dict(os.environ, {}, clear=True):
        info = ProtonManager.find_prefix_for_game(game_exe)
        assert info is not None
        assert info.appid == "1091500"
        assert info.prefix_path == pfx_dir


def test_proton_manager_inject_and_revert(tmp_path: Path) -> None:
    pfx_dir = tmp_path / "pfx"
    pfx_dir.mkdir(parents=True, exist_ok=True)
    user_reg = pfx_dir / "user.reg"
    user_reg.write_text("WINE REGISTRY Version 2\n", encoding="utf-8")

    info = SteamPrefixInfo(appid="123", prefix_path=pfx_dir, game_dir=tmp_path)

    injected = ProtonManager.inject_overrides(info, {"dxgi": "native,builtin", "d3d9": "native,builtin"})
    assert "dxgi" in injected
    assert "d3d9" in injected

    overrides = WineRegParser.read_overrides(user_reg)
    assert overrides["dxgi"] == "native,builtin"

    ok = ProtonManager.revert_overrides(info, ["dxgi"])
    assert ok
    overrides_after = WineRegParser.read_overrides(user_reg)
    assert "dxgi" not in overrides_after
    assert overrides_after["d3d9"] == "native,builtin"


def test_proton_manager_launch_options() -> None:
    assert ProtonManager.get_launch_options() == 'WINEDLLOVERRIDES="dxgi=n,b" %command%'
    assert ProtonManager.get_launch_options(["dxgi", "d3d9"]) == 'WINEDLLOVERRIDES="dxgi,d3d9=n,b" %command%'
    assert (
        ProtonManager.get_launch_options({"opengl32": "native,builtin"}) == 'WINEDLLOVERRIDES="opengl32=n,b" %command%'
    )
