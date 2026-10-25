"""Convention test: error-field names match the ADR in docs/architecture/adr-error-field-naming.md."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_error_field_naming_audit_passes():
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "quality" / "audit_error_fields.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "Error-field naming convention violated. "
        "See docs/architecture/adr-error-field-naming.md.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Inspected " in result.stdout


def test_error_field_naming_audit_targets_real_source_tree():
    namespace = runpy.run_path(
        str(REPO / "tools" / "quality" / "audit_error_fields.py"),
        run_name="audit_error_fields_test",
    )
    app = namespace["APP"]
    source_files = tuple(app.rglob("*.py"))

    assert app.is_dir()
    assert source_files
    assert namespace["audit"]() == 0


def test_error_field_audit_counts_only_parsed_files(tmp_path, capsys):
    namespace = runpy.run_path(
        str(REPO / "tools" / "quality" / "audit_error_fields.py"),
        run_name="audit_error_fields_test",
    )
    (tmp_path / "valid.py").write_text("value = 1\n")
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n")
    namespace["audit"].__globals__["APP"] = tmp_path

    assert namespace["audit"]() == 0

    output = capsys.readouterr().out
    assert "Inspected 1 Python source files" in output
    assert "Skipped 1 unparseable file(s)." in output
