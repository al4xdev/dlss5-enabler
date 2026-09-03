from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_global_install_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index_path = tmp_path / "global-state" / "installs.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("dlss5_enabler.core.record.get_global_index_path", lambda: index_path)
