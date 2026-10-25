from __future__ import annotations

from pathlib import Path

from astinuslab.core.config import Settings, settings


def test_libvirt_image_base_dir_default() -> None:
    # The declaration has a stable default; the loaded runtime may correctly
    # point at an operator override or this test run's disposable directory.
    assert Settings().libvirt_image_base_dir == Path("/var/lib/libvirt/images/astinuslab")


def test_libvirt_image_base_dir_is_path() -> None:
    assert isinstance(settings.libvirt_image_base_dir, Path)


def test_no_hardcoded_libvirt_image_path_in_production_code() -> None:
    """Guard against drift: production code should not embed the literal.

    Searches the four files refactored in this phase plus a broader sweep of
    src/astinuslab. Allowed: src/astinuslab/core/config.py (defines the default).
    """
    import subprocess

    app_dir = str(Path(__file__).resolve().parents[3] / "src" / "astinuslab")
    result = subprocess.run(
        [
            "grep",
            "-rn",
            "--include=*.py",
            "/var/lib/libvirt/images/astinuslab",
            app_dir,
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode in (0, 1), f"grep failed: {result.stderr}"
    offenders = [
        line for line in result.stdout.strip().splitlines() if line and "src/astinuslab/core/config.py" not in line
    ]
    assert offenders == [], f"Hardcoded path remains in production code: {offenders}"
