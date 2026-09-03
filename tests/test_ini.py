from pathlib import Path
from unittest.mock import patch

from dlss5_enabler.core.ini import ini_get_exact, ini_set_exact


def test_ini_get_exact_non_existent(tmp_path: Path) -> None:
    non_existent = tmp_path / "missing.ini"
    found, val = ini_get_exact(non_existent, "GENERAL", "Key")
    assert not found
    assert val == ""


def test_ini_get_exact_exception(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[GENERAL]\nKey=Value", encoding="utf-8")
    with patch("dlss5_enabler.core.ini._load_ini_document", return_value=None):
        found, val = ini_get_exact(target, "GENERAL", "Key")
        assert not found
        assert val == ""


def test_ini_get_exact_basic_and_whitespace(tmp_path: Path) -> None:
    ini_content = """
    [GENERAL]
    EffectSearchPaths = .\\reshade-shaders\\Shaders\\**
    TextureSearchPaths=.\\reshade-shaders\\Textures\\**
    """
    target = tmp_path / "ReShade.ini"
    target.write_text(ini_content, encoding="utf-8")

    found, val = ini_get_exact(target, "GENERAL", "EffectSearchPaths")
    assert found
    assert val == ".\\reshade-shaders\\Shaders\\**"

    found, val = ini_get_exact(target, "GENERAL", "TextureSearchPaths")
    assert found
    assert val == ".\\reshade-shaders\\Textures\\**"


def test_ini_get_exact_section_case_insensitivity(tmp_path: Path) -> None:
    ini_content = """
    [general]
    Key = Value1
    [DirectX]
    OutputAPI = d3d11
    """
    target = tmp_path / "test.ini"
    target.write_text(ini_content, encoding="utf-8")

    found, val = ini_get_exact(target, "GENERAL", "Key")
    assert found
    assert val == "Value1"

    found, val = ini_get_exact(target, "directx", "OutputAPI")
    assert found
    assert val == "d3d11"


def test_ini_get_exact_key_case_sensitivity(tmp_path: Path) -> None:
    ini_content = """
    [GENERAL]
    PreprocessorDefinitions = DEFINED_1
    PreProcessorDefinitions = DEFINED_WRONG
    """
    target = tmp_path / "test.ini"
    target.write_text(ini_content, encoding="utf-8")

    found, val = ini_get_exact(target, "GENERAL", "PreprocessorDefinitions")
    assert found
    assert val == "DEFINED_1"

    found, val = ini_get_exact(target, "GENERAL", "PreProcessorDefinitions")
    assert found
    assert val == "DEFINED_WRONG"

    found, val = ini_get_exact(target, "GENERAL", "preprocessordefinitions")
    assert not found
    assert val == ""


def test_ini_get_exact_comments_and_empty_lines(tmp_path: Path) -> None:
    ini_content = """
    ; This is a comment
    # Another comment

    [GENERAL]
    ; Comment inside section
    # Comment with hash
    ; Key = CommentedValue
    Key = RealValue
    """
    target = tmp_path / "test.ini"
    target.write_text(ini_content, encoding="utf-8")

    found, val = ini_get_exact(target, "GENERAL", "Key")
    assert found
    assert val == "RealValue"


def test_ini_get_exact_multiple_sections(tmp_path: Path) -> None:
    ini_content = """
    [Section1]
    SharedKey = Val1
    [Section2]
    SharedKey = Val2
    """
    target = tmp_path / "test.ini"
    target.write_text(ini_content, encoding="utf-8")

    found, val1 = ini_get_exact(target, "Section1", "SharedKey")
    assert found
    assert val1 == "Val1"

    found, val2 = ini_get_exact(target, "Section2", "SharedKey")
    assert found
    assert val2 == "Val2"


def test_ini_get_exact_missing_key_and_section(tmp_path: Path) -> None:
    ini_content = """
    [GENERAL]
    Key1 = Val1
    """
    target = tmp_path / "test.ini"
    target.write_text(ini_content, encoding="utf-8")

    found, val = ini_get_exact(target, "GENERAL", "NonExistentKey")
    assert not found
    assert val == ""

    found, val = ini_get_exact(target, "NonExistentSection", "Key1")
    assert not found
    assert val == ""


def test_ini_set_exact_create_new_file(tmp_path: Path) -> None:
    target = tmp_path / "new.ini"
    assert ini_set_exact(target, "GENERAL", "Key", "Val")
    assert target.is_file()

    found, val = ini_get_exact(target, "GENERAL", "Key")
    assert found
    assert val == "Val"


def test_ini_set_exact_update_existing_key(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[GENERAL]\nKey=OldVal\nOther=1\n", encoding="utf-8")

    assert ini_set_exact(target, "GENERAL", "Key", "NewVal")
    found, val = ini_get_exact(target, "GENERAL", "Key")
    assert found
    assert val == "NewVal"

    found, other_val = ini_get_exact(target, "GENERAL", "Other")
    assert found
    assert other_val == "1"


def test_ini_set_exact_preserves_reshade_bom_and_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "ReShade.ini"
    original = b"\xef\xbb\xbf[GENERAL]\r\nEffectSearchPaths=.\\reshade-shaders\\Shaders\\**\\**\r\n"
    target.write_bytes(original)

    assert ini_set_exact(target, "GENERAL", "EffectSearchPaths", ".\\reshade-shaders\\Shaders\\**")

    assert target.read_bytes() == b"\xef\xbb\xbf[GENERAL]\r\nEffectSearchPaths=.\\reshade-shaders\\Shaders\\**\r\n"


def test_ini_set_exact_delete_key_when_empty_value(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[GENERAL]\nKey1=Val1\nKey2=Val2\n", encoding="utf-8")

    assert ini_set_exact(target, "GENERAL", "Key1", "")
    found, _ = ini_get_exact(target, "GENERAL", "Key1")
    assert not found

    found, val2 = ini_get_exact(target, "GENERAL", "Key2")
    assert found
    assert val2 == "Val2"


def test_ini_set_exact_insert_key_in_existing_section_at_end(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[GENERAL]\nExistingKey=Val\n", encoding="utf-8")

    assert ini_set_exact(target, "GENERAL", "NewKey", "NewVal")
    found, val = ini_get_exact(target, "GENERAL", "NewKey")
    assert found
    assert val == "NewVal"


def test_ini_set_exact_insert_key_before_next_section(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[GENERAL]\nExisting=Val\n\n[OTHER]\nOtherKey=1\n", encoding="utf-8")

    assert ini_set_exact(target, "GENERAL", "InsertedKey", "InsertedVal")
    found, val = ini_get_exact(target, "GENERAL", "InsertedKey")
    assert found
    assert val == "InsertedVal"

    found, other_val = ini_get_exact(target, "OTHER", "OtherKey")
    assert found
    assert other_val == "1"


def test_ini_set_exact_add_new_section_to_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[GENERAL]\nKey=Val\n", encoding="utf-8")

    assert ini_set_exact(target, "NEW_SECTION", "NewKey", "NewVal")
    found, val = ini_get_exact(target, "NEW_SECTION", "NewKey")
    assert found
    assert val == "NewVal"


def test_ini_set_exact_case_insensitive_section_update(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    target.write_text("[general]\nKey=Val1\n", encoding="utf-8")

    assert ini_set_exact(target, "GENERAL", "Key", "Val2")
    found, val = ini_get_exact(target, "general", "Key")
    assert found
    assert val == "Val2"


def test_ini_set_exact_error_handling(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    with patch("dlss5_enabler.core.ini.atomic_write_bytes", side_effect=OSError("Disk write error")):
        assert not ini_set_exact(target, "GENERAL", "Key", "Val")


def test_ini_set_exact_read_failure_preserves_file(tmp_path: Path) -> None:
    target = tmp_path / "test.ini"
    original = "[GENERAL]\nExisting=Value\n"
    target.write_text(original, encoding="utf-8")
    with patch("dlss5_enabler.core.ini._load_ini_document", return_value=None):
        assert not ini_set_exact(target, "GENERAL", "NewKey", "NewValue")
    assert target.read_text(encoding="utf-8") == original
