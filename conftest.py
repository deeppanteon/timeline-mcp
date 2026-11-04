# The Python modules live in backend/; put it on sys.path so tests under tests/ can
# `import panteon_client` without packaging the project.

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))


@pytest.fixture(autouse=True)
def _request_log_dir(tmp_path, monkeypatch):
    # Keep the daily Timeline request log out of the real logs/ folder.
    monkeypatch.setenv("PANTEON_REQUEST_LOG_DIR", str(tmp_path / "request-logs"))


@pytest.fixture(autouse=True)
def _no_request_pacing(monkeypatch):
    monkeypatch.setenv("PANTEON_MIN_REQUEST_GAP_MS", "0")
