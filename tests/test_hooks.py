"""Unit tests for hooks module."""

import json
from pathlib import Path

from agy_remote.hooks import install_hooks_config


def test_install_hooks_config(tmp_path: Path):
    hooks_file = install_hooks_config(tmp_path)
    assert hooks_file.exists()

    with open(hooks_file, encoding="utf-8") as f:
        data = json.load(f)

    assert "remote-approval" in data
    assert "PreToolUse" in data["remote-approval"]
    assert data["remote-approval"]["PreToolUse"][0]["matcher"] == "*"
