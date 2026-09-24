"""MCP SDK dependency contract tests."""

import tomllib
from pathlib import Path


def test_mcp_dependency_matches_server_api():
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mcp>=2.0,<3" in pyproject["project"]["dependencies"]
