from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUN_WHEEL_CONTRACT = os.environ.get("ASTINUSLAB_RUN_WHEEL_CONTRACT") == "1"

pytestmark = [pytest.mark.integration]


def _copy_tracked_source(destination: Path) -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)


@pytest.mark.skipif(not RUN_WHEEL_CONTRACT, reason="set ASTINUSLAB_RUN_WHEEL_CONTRACT=1 for the wheel-only gate")
def test_wheel_only_runtime_contract(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _copy_tracked_source(source)
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist)],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("astinuslab-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
    assert "astinuslab/migrations/alembic.ini" in members
    assert "astinuslab/migrations/versions/a043_host_action_source_artifact.py" in members
    assert "astinuslab/migrations/versions/a044_prepare_execution_token.py" in members
    assert "astinuslab/lab_packages/prepare_authority.py" in members
    assert any(member.endswith("/share/astinuslab/docs/user/VOLATILITY3.md") for member in members)
    assert any(member.endswith("/share/astinuslab/docs/usage/getting-started.md") for member in members)
    assert "astinuslab/web/static/packages/creator.js" in members
    assert "astinuslab/web/static/packages/index.js" in members
    assert "astinuslab/web/templates/help.html" not in members
    assert "astinuslab/web/static/css/pages/help.css" not in members
    assert "astinuslab/web/templates/base.html" in members
    assert "astinuslab/web/static/htmx.min.js" in members
    # The self-hosted brand faces: a wheel without them silently falls back to
    # system fonts in installed releases while every source-tree check stays green.
    assert "astinuslab/web/static/fonts/InterVariable.woff2" in members
    assert "astinuslab/web/static/fonts/JetBrainsMono-Regular.woff2" in members
    assert not any(member.startswith(("backend/", "frontend/", "tests/")) for member in members)
    license_names = {Path(member).name for member in members if ".dist-info/licenses/" in member}
    assert {
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "VSL-1.0.txt",
        "go-BSD-3-Clause.txt",
        "htmx-0BSD.txt",
        "pflag-BSD-3-Clause.txt",
    } <= license_names

    isolated = tmp_path / "wheel-venv"
    venv.EnvBuilder(with_pip=False).create(isolated)
    python = isolated / "bin" / "python"
    subprocess.run(
        [sys.executable, "-m", "pip", "--python", str(python), "install", "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    probe = textwrap.dedent(
        """
        import importlib.util
        import json
        import sqlite3
        import sys
        from pathlib import Path

        dependency_site = Path(sys.argv[1]).resolve()
        repository = Path(sys.argv[2]).resolve()
        sys.path.append(str(dependency_site))

        assert all(Path(entry or ".").resolve() != repository for entry in sys.path)
        # find_spec raises rather than returning None when the *parent* package is
        # absent, which is the healthy case here: a clean wheel environment has no
        # `backend` at all. Either outcome means the legacy module is not importable.
        try:
            legacy_spec = importlib.util.find_spec("backend.app")
        except ModuleNotFoundError:
            legacy_spec = None
        assert legacy_spec is None

        import astinuslab
        from astinuslab.jobs.celery_main import app as celery_app
        from astinuslab.core.config import settings
        from astinuslab.db import initialize_database
        from astinuslab.main import app
        from astinuslab.ui.job_context import templates

        package_file = Path(astinuslab.__file__).resolve()
        assert package_file.is_relative_to(Path(sys.prefix).resolve())
        expected_tasks = {
            "acquire_memory_dump",
            "playground.spawn_vm",
            "reconcile_libvirt_resources",
            "run_vol3_plugin",
        }
        assert expected_tasks <= set(celery_app.tasks)
        initialize_database()
        with sqlite3.connect(settings.db_path) as connection:
            revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        assert revisions == [("a044_prepare_execution_token",)]
        revision = revisions[0][0]
        with sqlite3.connect(settings.db_path) as connection:
            reservation_columns = {row[1] for row in connection.execute("PRAGMA table_info(capacity_reservations)")}
            action_columns = {row[1] for row in connection.execute("PRAGMA table_info(host_actions)")}
            nic_columns = {row[1] for row in connection.execute("PRAGMA table_info(job_vm_nics)")}
            prepare_columns = {row[1] for row in connection.execute("PRAGMA table_info(lab_prepare_attempts)")}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"job_id", "vm_count", "memory_mb", "storage_bytes", "state", "boot_started"} <= reservation_columns
        assert {"owner_token", "lease_expires_at", "celery_task_id", "dispatched_at", "dispatch_attempts"} <= action_columns
        assert {"ordinal", "detach_token", "detach_claimed_at"} <= nic_columns
        assert "execution_token" in prepare_columns
        assert {"lab_packages", "lab_instances", "lab_operation_outbox"} <= tables
        assert templates.get_template("base.html").name == "base.html"
        static_route = next(route for route in app.routes if getattr(route, "name", None) == "static")
        static_root = Path(static_route.app.directory).resolve()
        assert static_root.is_relative_to(package_file.parent)
        assert (static_root / "css/tokens.css").is_file()
        assert (static_root / "fonts/InterVariable.woff2").is_file()
        from astinuslab.ui.help_routes import docs_root
        help_root = docs_root()
        assert help_root.is_relative_to(Path(sys.prefix))
        guide = help_root / "user/VOLATILITY3.md"
        assert guide.is_file()
        assert (help_root / "usage/getting-started.md").is_file()
        assert not (help_root / "audits").exists()
        print(
            json.dumps(
                {
                    "package": str(package_file),
                    "revision": revision,
                    "static": str(static_root),
                    "tasks": sorted(expected_tasks),
                }
            )
        )
        """
    )
    env = {
        **os.environ,
        "ASTINUSLAB_DATA_DIR": str(tmp_path / "data"),
        "ASTINUSLAB_DB_PATH": str(tmp_path / "data" / "wheel.db"),
        "ASTINUSLAB_STORAGE_DIR": str(tmp_path / "data" / "storage"),
        "ASTINUSLAB_FAKE_LIBVIRT": "1",
        "ASTINUSLAB_FAKE_PLAYGROUND": "1",
    }
    result = subprocess.run(
        [str(python), "-I", "-c", probe, sysconfig.get_paths()["purelib"], str(ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
