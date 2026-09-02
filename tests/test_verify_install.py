from collections.abc import Mapping, Sequence
from pathlib import Path

from dlss5_enabler.verify_install import verify_isolated_installs


def test_isolated_install_verifier_checks_uv_and_pip_entrypoints(tmp_path: Path) -> None:
    wheel = tmp_path / "dlss5_enabler-1.1.0-py3-none-any.whl"
    wheel.touch()
    commands: list[tuple[str, ...]] = []

    def record(command: Sequence[str], _environment: Mapping[str, str] | None) -> None:
        commands.append(tuple(command))

    verify_isolated_installs(wheel, runner=record)

    assert commands[0][:4] == ("uv", "tool", "install", "--force")
    assert commands[0][-1] == str(wheel.resolve())
    assert any(command[-1] == "--help" and "uv-bin" in command[0] for command in commands)
    assert any(command[-1] == "version" and "uv-bin" in command[0] for command in commands)
    assert any(command[1:5] == ("-m", "pip", "install", "--upgrade") for command in commands)
    assert any(command[-1] == "--help" and "pip-venv" in command[0] for command in commands)
    assert any(command[-1] == "version" and "pip-venv" in command[0] for command in commands)
