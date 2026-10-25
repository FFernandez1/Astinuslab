"""Read-only operator guidance for external Volatility 3 tooling."""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .tool_metadata import DWARF2JSON_AMD64

if TYPE_CHECKING:
    from .vol3_tools import ToolStatus, Vol3Readiness

# A recovery guide must render even when config import fails. This operator
# example is checked against preferred_tested_volatility_version in guide tests.
_EXAMPLE_VOLATILITY_VERSION = "2.28.0"


def load_settings():
    from ..core.config import load_settings as load

    return load()


def get_vol3_readiness(configured):
    from .vol3_tools import get_vol3_readiness as probe

    return probe(configured)


@dataclass(frozen=True)
class GuideContext:
    base_dir: Path
    data_dir: Path
    service_user: str
    split_users: bool
    api_url: str
    auth_enabled: bool


_STATUS_LABELS = {
    "feature_disabled": "UNCONFIGURED",
    "volatility_not_configured": "UNCONFIGURED",
    "dwarf2json_not_configured": "UNCONFIGURED",
    "volatility_not_found": "MISSING",
    "dwarf2json_not_found": "MISSING",
    "volatility_not_executable": "NOT EXECUTABLE",
    "dwarf2json_not_executable": "NOT EXECUTABLE",
    "volatility_version_unsupported": "UNSUPPORTED",
    "dwarf2json_hash_not_configured": "HASH NOT CONFIGURED",
    "dwarf2json_hash_mismatch": "HASH MISMATCH",
    "volatility_probe_failed": "PROBE FAILED",
}


def status_label(status: ToolStatus) -> str:
    if status.ready:
        return "READY (UNTESTED)" if status.untested else "READY"
    return _STATUS_LABELS.get(status.code, "UNAVAILABLE")


def _restart_command(context: GuideContext) -> str:
    if context.base_dir == context.data_dir:
        return "./astinuslab.sh restart"
    if context.split_users:
        return "sudo systemctl restart astinuslab-api astinuslab-runner astinuslab-analysis"
    suffix = shlex.quote(context.service_user)
    return f"sudo systemctl restart astinuslab-api@{suffix} astinuslab-runner@{suffix} astinuslab-analysis@{suffix}"


def _readiness_command(context: GuideContext) -> str:
    endpoint = f"{context.api_url.rstrip('/')}/api/v1/volatility/readiness"
    if not context.auth_enabled:
        return f"curl -fsS {shlex.quote(endpoint)} | python3 -m json.tool"
    return "\n".join(
        (
            "export ASTINUSLAB_ADMIN_TOKEN='<saved-install-token>'",
            'curl -fsS -H "Authorization: Bearer $ASTINUSLAB_ADMIN_TOKEN" \\\n'
            f"  {shlex.quote(endpoint)} | python3 -m json.tool",
        )
    )


def _status_section(readiness: Vol3Readiness | None) -> str:
    if readiness is None:
        return "Current status: configuration unavailable"
    return "\n".join(
        (
            f"Analysis: {status_label(readiness.analysis)} — {readiness.analysis.detail}",
            f"ISF build: {status_label(readiness.isf_build)} — {readiness.isf_build.detail}",
        )
    )


def render_guide(context: GuideContext, readiness: Vol3Readiness | None) -> str:
    version = _EXAMPLE_VOLATILITY_VERSION
    volatility_root = Path(f"/opt/volatility3-{version}")
    volatility_bin = volatility_root / "bin" / "vol"
    dwarf_root = Path(f"/opt/dwarf2json/{DWARF2JSON_AMD64.version}")
    dwarf_bin = dwarf_root / DWARF2JSON_AMD64.name
    config_path = context.data_dir / "astinuslab.toml"
    validation = shlex.quote(
        f"import pathlib,tomllib; tomllib.loads(pathlib.Path({str(config_path)!r}).read_text()); print('TOML valid')"
    )
    python = shlex.quote(str(context.base_dir / ".venv/bin/python"))
    return f"""Volatility 3 operator setup guide
=================================

This command is read-only. It never downloads, installs, upgrades, or licenses
Volatility 3 or dwarf2json. Review THIRD_PARTY_NOTICES.md and
docs/user/VOLATILITY3.md before choosing and obtaining either tool.

1. CURRENT STATUS
{_status_section(readiness)}

2. INSTALL VOLATILITY 3 {version}
Choose a trusted package index and review the upstream VSL first, then run:

  sudo install -d -m 0755 {volatility_root}
  sudo chown "$(id -un):$(id -gn)" {volatility_root}
  python3.12 -m venv {volatility_root}
  {volatility_root}/bin/python -m pip install 'volatility3=={version}'
  {volatility_bin} -q frameworkinfo.FrameworkInfo

Expected banner for this tested example: Volatility 3 Framework {version}
This release requires at least {version}; {version} is tested. Later versions
are accepted as READY (UNTESTED) and require your own regression checks.

3. OBTAIN DWARF2JSON
Audited upstream artifact (information only; AstinusLab does not fetch it):
  {DWARF2JSON_AMD64.download_url}
Pinned source commit:
  {DWARF2JSON_AMD64.commit}

After obtaining the artifact yourself, place and verify it:

  sudo install -d -m 0755 {dwarf_root}
  sudo install -m 0755 /path/to/downloaded/{DWARF2JSON_AMD64.name} {dwarf_bin}
  echo '{DWARF2JSON_AMD64.sha256}  {dwarf_bin}' | sha256sum --check -
  {dwarf_bin} --help

4. CONFIGURE ASTINUSLAB
Edit the existing [vol3] section in {config_path}, replacing these keys
and preserving other keys. Do not append a second [vol3] table. Create the
section only if it is absent:

  [vol3]
  enabled = true
  volatility_bin = "{volatility_bin}"
  dwarf2json_path = "{dwarf_bin}"
  dwarf2json_sha256 = "{DWARF2JSON_AMD64.sha256}"

Validate TOML syntax before restarting (read-only; fix errors before proceeding):
  {python} -c {validation}

5. RESTART ASTINUSLAB
  {_restart_command(context)}

6. VERIFY APPLICATION READINESS
{_readiness_command(context)}

UI alternative: open Volatility → Readiness.

7. FIRST USE
Build or upload a matching Linux ISF, run a memory-capture job, then launch
plugin analysis. See docs/user/VOLATILITY3.md for upgrades, rollback, manual
ISF upload, and stable diagnostic codes.
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print read-only Volatility 3 installation and readiness guidance.")
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--service-user", required=True)
    parser.add_argument("--split-users", action="store_true")
    parser.add_argument("--api-url", required=True)
    args = parser.parse_args(argv)

    try:
        configured = load_settings()
        readiness = get_vol3_readiness(configured)
        auth_enabled = configured.auth_enabled
    except (OSError, ValueError):
        readiness = None
        auth_enabled = True

    context = GuideContext(
        base_dir=args.base_dir,
        data_dir=args.data_dir,
        service_user=args.service_user,
        split_users=args.split_users,
        api_url=args.api_url,
        auth_enabled=auth_enabled,
    )
    print(render_guide(context, readiness))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
