"""Reject missing/skipped native-tool coverage in ordinary, unprivileged CI.

Real-host, reference-image, systemd-version and Windows acceptance have separate
owners. Their skips are counted visibly, never interpreted as passing evidence.
Native-tool means utilities such as setfacl and qemu-img on disposable files;
the backend suite uses fake libvirt and does not certify a live hypervisor.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat

REQUIRED_MODULES = {
    "backend": frozenset(
        f"tests.python.host.{name}"
        for name in (
            "test_host_acl_helper",
            "test_libvirt_storage_admin_helper",
            "test_network_isolation_helper",
            "test_offline_disk_capture",
        )
    ),
    "tooling": frozenset(
        f"tests.python.tooling.{name}"
        for name in (
            "test_documented_api_examples",
            "test_host_scripts",
            "test_install_script",
            "test_third_party_boundary",
            "test_uninstall_acl_reversal",
        )
    ),
}

# This one mutation path needs root; ordinary CI deliberately runs unprivileged.
# Match the exact case AND reason so another missing tool cannot hide behind it.
ALLOWED_SKIP = (
    "tests.python.tooling.test_install_script",
    "test_privileged_root_command_renders_custom_storage_and_home_protection",
    "mutation path is root-only; --check is covered without root",
)


def read_cases(report: Path) -> list[ET.Element]:
    # JUnit needs no DTD or entity definitions. Reject them before expansion,
    # using only the standard library already available in the locked runner.
    def reject_doctype(*_args: object) -> None:
        raise ValueError("DTD declarations are not permitted in test reports")

    builder = ET.TreeBuilder()
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    with report.open("rb") as stream:
        parser.ParseFile(stream)
    return list(builder.close().iter("testcase"))


def check_report(tier: str, report: Path) -> tuple[list[str], str]:
    try:
        cases = read_cases(report)
    except (OSError, expat.ExpatError, ValueError) as exc:
        return [f"Cannot read {tier} report: {exc}"], ""
    required = REQUIRED_MODULES[tier]
    seen: set[str] = set()
    errors: list[str] = []
    outside_skips = 0
    safety_skips = 0
    for case in cases:
        module, name = case.get("classname", ""), case.get("name", "")
        if module in required:
            seen.add(module)
        for tag in ("error", "failure", "skipped"):
            outcome = case.find(tag)
            if outcome is None:
                continue
            reason = outcome.get("message", "")
            if tag == "skipped":
                if module not in required:
                    outside_skips += 1
                    continue
                if (module, name, reason) == ALLOWED_SKIP:
                    safety_skips += 1
                    continue
            errors.append(f"{module}::{name}: {tag}: {reason}")
    errors.extend(f"Required module absent from report: {module}" for module in sorted(required - seen))
    summary = (
        f"{tier}: {len(cases)} cases reported; {len(seen)}/{len(required)} required modules; "
        f"root-safety exceptions: {safety_skips}; "
        f"outside required native-tool scope: {outside_skips} skipped (not acceptance evidence)"
    )
    return errors, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=sorted(REQUIRED_MODULES))
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    errors, summary = check_report(args.tier, args.report)
    if summary:
        print(summary)
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
