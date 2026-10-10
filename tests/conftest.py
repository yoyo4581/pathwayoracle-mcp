"""Every test gets its own long-term memory directory: memory persists across
calls by design, so a shared one would leak episodes between tests and runs."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("PATHWAYORACLE_MEMORY_DIR", str(tmp_path / "memory"))
