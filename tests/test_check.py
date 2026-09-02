import sys
from pathlib import Path

import pytest

from dlss5_enabler.check import PROJECT_ROOT, run_tool


def test_run_tool_uses_project_root_from_any_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    passed, _duration, output = run_tool([sys.executable, "-c", "import os; print(os.getcwd())"])

    assert passed
    assert Path(output).resolve() == PROJECT_ROOT
