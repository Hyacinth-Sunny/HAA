"""Tests for the ``haa project`` CLI sub-app.

Uses typer's CliRunner with a monkeypatched StateStore so no real database
or API keys are needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from haa.cli.main import app
from haa.state import StateStore

runner = CliRunner()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def patched_store(db_path, monkeypatch):
    """Monkeypatch haa.cli.project._store to use a temp database.

    Each call returns a fresh StateStore pointing to the same file, so data
    persists across CLI invocations (the ``with _store() as store:`` pattern
    closes each handle but the file survives).
    """
    import haa.cli.project as proj_mod
    monkeypatch.setattr(proj_mod, "_store", lambda: StateStore(db_path))


@pytest.fixture
def brief_file(tmp_path):
    p = tmp_path / "brief.yaml"
    p.write_text(
        "title: Test Brief\n"
        "problem_area: Generalization bounds\n"
        "track: theory\n",
        encoding="utf-8",
    )
    return p


# --------------------------------------------------------------------------- #
#  create + list
# --------------------------------------------------------------------------- #

class TestCreateList:

    def test_create_success(self, patched_store, brief_file):
        result = runner.invoke(app, [
            "project", "create", str(brief_file), "--seek", "5", "--output", "3",
        ])
        assert result.exit_code == 0, result.output
        assert "Project created" in result.output
        assert "Test Brief" in result.output

    def test_list_shows_created_project(self, patched_store, brief_file):
        runner.invoke(app, ["project", "create", str(brief_file)])
        result = runner.invoke(app, ["project", "list"])
        assert result.exit_code == 0
        assert "Test Brief" in result.output
        assert "not_started" in result.output

    def test_list_empty(self, patched_store):
        result = runner.invoke(app, ["project", "list"])
        assert result.exit_code == 0
        assert "No projects" in result.output


# --------------------------------------------------------------------------- #
#  status
# --------------------------------------------------------------------------- #

class TestStatus:

    def test_status_shows_detail(self, patched_store, brief_file):
        create_result = runner.invoke(app, ["project", "create", str(brief_file)])
        # Extract project ID from output.
        project_id = _extract_project_id(create_result.output)
        result = runner.invoke(app, ["project", "status", project_id])
        assert result.exit_code == 0
        assert "Test Brief" in result.output
        assert "not_started" in result.output

    def test_status_missing_project(self, patched_store):
        result = runner.invoke(app, ["project", "status", "nonexistent"])
        assert result.exit_code == 1


# --------------------------------------------------------------------------- #
#  abandon
# --------------------------------------------------------------------------- #

class TestAbandon:

    def test_abandon_sets_aborted(self, patched_store, brief_file):
        create_result = runner.invoke(app, ["project", "create", str(brief_file)])
        project_id = _extract_project_id(create_result.output)
        result = runner.invoke(app, [
            "project", "abandon", project_id, "--reason", "testing",
        ])
        assert result.exit_code == 0
        assert "ABORTED" in result.output
        # Verify via list.
        list_result = runner.invoke(app, ["project", "list"])
        assert "aborted" in list_result.output


# --------------------------------------------------------------------------- #
#  recover (without --yes should fail)
# --------------------------------------------------------------------------- #

class TestRecover:

    def test_recover_without_yes_exits(self, patched_store, brief_file):
        create_result = runner.invoke(app, ["project", "create", str(brief_file)])
        project_id = _extract_project_id(create_result.output)
        result = runner.invoke(app, ["project", "recover", project_id])
        # Not MORIBUND → error.
        assert result.exit_code != 0


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _extract_project_id(output: str) -> str:
    """Extract the project ID from `project create` output."""
    for line in output.splitlines():
        if "Project created:" in line:
            # Line looks like: "✓ Project created: abc123..."
            parts = line.split()
            for p in parts:
                if len(p) >= 8 and all(c in "0123456789abcdef" for c in p):
                    return p
    # Fallback: read from the store.
    raise ValueError(f"could not extract project ID from output:\n{output}")
