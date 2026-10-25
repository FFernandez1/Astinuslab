"""Per-module coverage floors for the modules whose bugs are silent.

The suite has one global gate, `--cov-fail-under`. A single percentage over
30,000 statements cannot notice that one file went from well covered to barely
covered: a few hundred new well-tested lines elsewhere pay for it, and the
total moves by a tenth of a point.

That matters unevenly. Most of this codebase fails loudly -- a route returns
500, a test goes red. The modules listed here fail quietly: they reconcile
libvirt against the database, tear resources down, move evidence into custody,
and publish artifacts. A defect in one of them leaks a VM, a disk or a network,
or loses an acquisition, and nothing reports an error at the time. Those are
the files where "is it still tested?" needs its own answer.

Floors are integers and the check is `>=`, not equality. Coverage moves by
fractions whenever an unrelated test touches a shared helper, and a gate that
fails on a tenth of a point trains people to edit the number without reading
it. A floor that has been cleared by SLACK points or more is reported too, so
the ratchet tightens instead of drifting away from reality.

Usage, against the JSON the coverage run already knows how to emit:

    pytest tests/python --cov=src/astinuslab --cov-report=json:coverage.json
    python tools/quality/check_coverage_floors.py coverage.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# How far above its floor a module may sit before the floor is considered
# stale. Wide enough to absorb ordinary movement, narrow enough that a module
# cannot quietly drift 10 points clear and then fall back unnoticed.
SLACK = 3

# path relative to the repository root -> minimum percent covered
#
# Every entry is here because its failure mode is silent. Add a module when
# that is true of it, not because it happens to be well covered today.
FLOORS: dict[str, int] = {
    # Reconciles libvirt against the database and tears down what no row
    # claims. A bug in either half destroys a live VM or leaves a dead one
    # running. The floor was 92 when this was one 1,439-line module; splitting
    # the phases out left the entry point and the primitives at 91.99, so it
    # is 91 here and the phases carry their own. Both halves are floored on
    # purpose: a floor on only one of them would have let the other drift, and
    # this very split is what would have caused that.
    "src/astinuslab/playground/reconciliation.py": 91,
    "src/astinuslab/playground/reconciliation_phases.py": 93,
    # No longer creates disks, defines domains or touches the filesystem -- those
    # moved to `vm_lifecycle.py` and `filesystem_safety.py` with their domains,
    # and this module no longer imports `os`, `stat` or `errno` at all. What is
    # floored here now is the shared surface every sibling reaches through: the
    # libvirt connection seam, the fake-mode double the whole hermetic tier
    # depends on, the Celery task wrappers, and the delegating wrappers the
    # siblings and the suite reach by name. The floor held at 81 through the VM
    # split only because the first two were covered in the same change; it dipped
    # to 76.6 in between. 81 -> 84: removing the dead `_image_os_slug` and
    # `_slug_segment` took 14 uncovered statements out of the denominator and
    # moved the module to 85.02% without testing anything new. 84 -> 91: the
    # no-follow slice covered the primitives before moving them and then took
    # them out of this file, which is 92.03% now. Both rises are the same kind of
    # tightening -- less unproven code here, not a number talked up -- so the
    # floor follows them rather than banking the headroom.
    "src/astinuslab/playground/orchestrator.py": 91,
    # The no-follow filesystem primitives split out of `orchestrator.py`, and the
    # last domain to leave it. Its defect is the worst of the set because nothing
    # reports it: a followed symlink or an inode swapped between the check and the
    # act deletes a file the playground never created, or writes a capture
    # somewhere other than the path recorded for the evidence, and every row
    # involved still looks correct afterwards. Every member was at 100% when it
    # moved -- the group went 78.1% to fully covered in the commit before the
    # extraction -- and it is floored at 99 for the reason `network_lifecycle.py`
    # is: a fractional dip is not a failure, a new uncovered refusal is. Floored
    # beside `orchestrator.py`, both halves, for the reason the reconciliation
    # pair is.
    "src/astinuslab/playground/filesystem_safety.py": 99,
    # The VM lifecycle split out of `orchestrator.py`: it creates the disk and
    # defines the domain, so its defect is a VM whose disk exists and whose
    # domain may be running, with a row that no longer says so. This module
    # inherited the rationale that used to sit on `orchestrator.py`'s own floor.
    # Held at 99 through the cloud-init split (99.56% to 99.54%), which took it
    # from 959 lines to 890 and off the hotspot page.
    "src/astinuslab/playground/vm_lifecycle.py": 99,
    # The cloud-init seed writer split out of `vm_lifecycle.py`. It is here for
    # what the seed grants rather than for what it costs to lose: `user-data`
    # installs the guest's authorized key and gives that account passwordless
    # sudo, so the staging directory is created 0700 explicitly and through the
    # no-follow helper. A wrong key or a group-writable staging directory is a
    # security defect that reports nothing at the time -- the VM boots, the row
    # says running, and only the operator's failed SSH says otherwise. Fully
    # covered when it moved, floored at 99 for the reason `network_lifecycle.py`
    # is. Floored beside `vm_lifecycle.py`, both halves, for the reason the
    # reconciliation pair is.
    "src/astinuslab/playground/cloud_init_seed.py": 99,
    # NIC attach and detach split out of `orchestrator.py`. Its defect is the
    # one an operator cannot see from the database: an interface on a running
    # domain that no row claims, or a row claiming an interface that is gone.
    # Floored beside `orchestrator.py`, both halves, for the reason the
    # reconciliation pair is.
    "src/astinuslab/playground/nic_lifecycle.py": 96,
    # Workload provisioning split out of `orchestrator.py`. By the time it runs
    # the VM is booted and its disk written, so its defect is a VM no operator
    # can account for: the work happened and the row never recorded what became
    # of it. Floored beside `orchestrator.py`, both halves, for the reason the
    # reconciliation pair is.
    "src/astinuslab/playground/workload_provisioning.py": 99,
    # The network lifecycle split out of `orchestrator.py`. Its defect is a
    # leaked subnet: a libvirt network left defined with no row claiming it,
    # which blocks the next create with "Network is already in use". Fully
    # covered when it moved, floored at 99 so a fractional dip is not a failure
    # but a new uncovered custody path is. Floored beside `orchestrator.py` for
    # the reason the reconciliation pair is: a floor on only one half lets the
    # other drift, and the split is what would cause it.
    "src/astinuslab/playground/network_lifecycle.py": 99,
    # The capture and acquisition pipeline split out of `orchestrator.py`: it
    # writes the evidence, hashes it, and seals the Acquisition row. A defect
    # here loses evidence or leaves an acquisition unresolvable. Floored beside
    # `orchestrator.py` for the reason the reconciliation pair is: a floor on
    # only one half lets the other drift, and the split is what would cause it.
    "src/astinuslab/playground/acquisition.py": 92,
    # The claim leases every destructive playground path checks before acting.
    "src/astinuslab/playground/resource_custody.py": 81,
    # The startup sweep, which decides what a restart is allowed to remove.
    "src/astinuslab/jobs/startup_recovery.py": 80,
    # Package import, launch, reset, teardown and reconciliation.
    "src/astinuslab/lab_packages/package_service.py": 82,
    # Acquisition capture under lock; a defect here loses evidence.
    "src/astinuslab/forensics/capture_support.py": 85,
    # Artifact publication, hashing and manifest writing.
    "src/astinuslab/storage/publication.py": 88,
    # Deletes retained evidence on a schedule. 93 -> 96: the serial-log
    # retention cases the observability work added took this to 96.7%, and the
    # floors gate reports anything 3 or more points clear as a floor to raise.
    "src/astinuslab/storage/retention.py": 96,
    # The shared execution slot; over-admission oversubscribes the host.
    "src/astinuslab/jobs/capacity.py": 93,
    # Lower than the rest, and the number understates it: this module is both
    # imported by the package services *and* run as a subprocess by
    # ops/deploy/install.sh (`python3 -I ... storage_custody.py`). The suite
    # here measures only the imported half; the installer half is exercised by
    # tests/python/tooling/test_package_custody.py, through subprocesses this
    # report cannot see. The floor still catches the module falling out of use
    # entirely, which is what it is for until that half is measurable.
    "src/astinuslab/lab_packages/storage_custody.py": 39,
}


def _report_files(report: Path) -> dict[str, float]:
    data = json.loads(report.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("report contains no per-file coverage")
    measured: dict[str, float] = {}
    for path, entry in files.items():
        percent = entry.get("summary", {}).get("percent_covered")
        if percent is None:
            raise ValueError(f"no percent_covered for {path}")
        measured[path.replace("\\", "/")] = float(percent)
    return measured


def check_floors(report: Path) -> tuple[list[str], list[str]]:
    """Returns (failures, notes)."""
    try:
        measured = _report_files(report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"Cannot read coverage report {report}: {exc}"], []

    failures: list[str] = []
    notes: list[str] = []
    for path in sorted(FLOORS):
        floor = FLOORS[path]
        if path not in measured:
            # A floor naming a file the report does not contain is not a pass.
            # The module was renamed, deleted, or dropped from --cov scope, and
            # each of those needs the list updated rather than ignored.
            failures.append(f"{path}: named in FLOORS but absent from the coverage report")
            continue
        actual = measured[path]
        if actual < floor:
            failures.append(f"{path}: {actual:.1f}% covered, floor is {floor}%")
        elif actual >= floor + SLACK:
            notes.append(f"{path}: {actual:.1f}% covered, floor is {floor}% -- raise it")
    return failures, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="coverage.py JSON report")
    parser.add_argument(
        "--raise-stale-floors",
        action="store_true",
        help=f"fail when a module sits {SLACK} or more points above its floor, instead of only reporting it",
    )
    args = parser.parse_args(argv)

    failures, notes = check_floors(args.report)
    print(
        f"coverage floors: {len(FLOORS)} modules checked, {len(failures)} below floor, {len(notes)} clear by {SLACK}+"
    )
    for note in notes:
        print(f"  {note}")
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        return 1
    return 1 if (notes and args.raise_stale_floors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
