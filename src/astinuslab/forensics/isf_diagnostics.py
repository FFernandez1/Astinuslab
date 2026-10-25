"""Collect diagnostic artifacts on terminal ISF extraction failures."""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from pathlib import Path

_SERIAL_TAIL_LINES = 200


def write_diagnostic_bundle(
    *,
    run_dir: Path,
    target: str,
    domain_name: str | None,
    error: BaseException,
    serial_log_path: Path,
    preflight_info: dict[str, str],
    dumpxml: Callable[[str], str],
) -> Path:
    """Write diagnostics under run_dir/target/diagnostics/. Return the directory.

    Best-effort: never raises on internal collection failures.
    """
    bundle_dir = run_dir / target / "diagnostics"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    if serial_log_path.exists():
        try:
            lines = serial_log_path.read_text(errors="replace").splitlines()
            tail = lines[-_SERIAL_TAIL_LINES:]
            (bundle_dir / "serial.log").write_text("\n".join(tail) + "\n")
        except Exception as exc:  # noqa: BLE001
            (bundle_dir / "serial.log").write_text(f"(failed to read serial log: {exc})\n")
    else:
        (bundle_dir / "serial.log").write_text("(no serial log captured)\n")

    if domain_name:
        try:
            xml = dumpxml(domain_name)
            (bundle_dir / "libvirt-domain.xml").write_text(xml or "(empty dumpxml)\n")
        except Exception as exc:  # noqa: BLE001
            (bundle_dir / "libvirt-domain.xml").write_text(f"(dumpxml failed: {exc})\n")
    else:
        (bundle_dir / "libvirt-domain.xml").write_text("(no domain available)\n")

    try:
        (bundle_dir / "preflight.json").write_text(json.dumps(preflight_info, indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001
        (bundle_dir / "preflight.json").write_text(f"{{}}  // serialization failed: {exc}\n")

    error_text = f"{type(error).__name__}: {error}\n\n"
    error_text += "".join(traceback.format_exception(type(error), error, error.__traceback__))
    (bundle_dir / "error.txt").write_text(error_text)

    return bundle_dir
