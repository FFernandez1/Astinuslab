"""Contract for ``docs/architecture/hotspots.md``.

The hotspots page carries soft size budgets and a documented procedure whose
last step is "Update the LOC numbers in this file in the same commit". Nothing
enforced that step, so the page silently went stale: between two refreshes
``reconcile_libvirt_resources`` grew 183 -> 539 lines and
``playground/orchestrator.py`` grew 1175 -> 2214, while ``spawn_vm`` and
``run_vol3_plugin`` crossed the function ceiling without ever being listed. The
only automated check that touched the page verified a table-of-contents anchor.

This module turns the page into a checked artifact. It does **not** enforce the
soft ceilings -- the page is explicit that large is tolerated where the
responsibility is genuinely single, and several entries sit over their ceiling
today by deliberate choice. It enforces the two properties that make the page
worth reading:

* **Recorded numbers are true.** Every listed LOC equals the measured LOC, so
  growth cannot land without someone restating it.
* **The list is complete.** Anything in ``backend/app`` over the discovery
  thresholds is listed, so a hotspot cannot escape by never being added.
* **Shell is budgeted too.** 14,134 lines of shell, 5,151 of them in
  ``ops/deploy/install.sh``, sat outside every size check in the project while
  the Python surface had a ratchet. Two release-blocking defects were found in
  that installer by host acceptance, one of them an unanchored ``rsync
  --exclude``. The shell tables are measured and enforced the same way.
* **The translations agree.** The ``ca``/``es``/``fr`` pages had rotted further
  than the English one -- they still listed ``backend/app/orchestrator.py``,
  ``backend/app/i18n.py`` and ``frontend/static/style.css``, none of which exist
  in that form any more. Prose is free to differ; the rows are not.

Measurement conventions (mirrored in the page itself so a reader can reproduce
them by hand):

* File LOC is the physical line count, as ``wc -l`` reports it.
* Function LOC spans the ``def`` line through the last line of the body.
  Decorators are excluded, so adding a decorator does not change the number.
* The function scan covers module-level functions and methods of module-level
  classes. Closures nested inside a function are already counted within their
  enclosing entry.
"""

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOTSPOTS = ROOT / "docs/architecture/hotspots.md"
# The heredoc discovery helper is defined once, in the module that owns the
# shell lint gates, and loaded by path here. Restating it would let the page's
# "embedded Python" numbers and the lint gates' idea of embedded Python drift
# apart silently; a rename breaks this import loudly instead.
_HOST_SCRIPTS = importlib.util.spec_from_file_location(
    "_host_scripts_helpers", Path(__file__).with_name("test_host_scripts.py")
)
assert _HOST_SCRIPTS is not None and _HOST_SCRIPTS.loader is not None
host_scripts = importlib.util.module_from_spec(_HOST_SCRIPTS)
_HOST_SCRIPTS.loader.exec_module(host_scripts)
TRANSLATIONS = ("ca", "es", "fr")
SCAN_ROOT = "src/astinuslab"
# Alembic revisions are generated, append-only, and executed in migration
# subprocesses rather than maintained as application code. They sat outside the
# scanned tree until the src layout packaged them, so excluding them keeps this
# ratchet measuring the same thing it measured before -- the same reason
# pyproject omits them from coverage.
SCAN_EXCLUDE = ("src/astinuslab/migrations/",)

pytestmark = pytest.mark.unit


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", SCAN_ROOT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / p for p in out if p.endswith(".py") and not p.startswith(SCAN_EXCLUDE)]


def _file_loc(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    # end_lineno is Optional on the ast stubs but is always populated by
    # ast.parse; a None here would mean the span is unknowable, so fail loudly
    # rather than silently score the function as zero lines and let it hide.
    assert node.end_lineno is not None, f"no end position for {node.name}"
    return node.end_lineno - node.lineno + 1


def _function_spans(path: Path) -> dict[str, int]:
    """Map ``name`` / ``Class.name`` to its LOC span for one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            spans[node.name] = _span(node)
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    spans[f"{node.name}.{member.name}"] = _span(member)
    return spans


_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|")


def _parse_table(section: str) -> dict[str, tuple[int, int]]:
    """Rows of one markdown table as ``{subject: (recorded_loc, ceiling)}``.

    The page is the source of truth for which hotspots exist, so an unparseable
    section is a failure rather than an empty result -- otherwise reformatting
    the table into a shape this regex misses would silently disable every check
    below.
    """
    body = _section_body(section)
    rows: dict[str, tuple[int, int]] = {}
    for line in body.splitlines():
        # A data row names its subject in backticks in the first cell; that is
        # what separates it from the header and the `|---|` divider. Anything
        # backticked that then fails to parse is a malformed entry, not a row to
        # skip over -- otherwise a typo'd LOC column would quietly go unchecked.
        if not line.startswith("|") or "`" not in line.split("|")[1]:
            continue
        match = _ROW.match(line)
        assert match is not None, f"unparseable row in '{section}' of {HOTSPOTS.name}: {line!r}"
        subject, recorded, ceiling = match.group(1), int(match.group(2)), int(match.group(3))
        assert subject not in rows, f"duplicate entry for {subject} in '{section}'"
        rows[subject] = (recorded, ceiling)
    assert rows, f"no rows parsed from '{section}' in {HOTSPOTS.name}"
    return rows


_THRESHOLD = re.compile(
    r"^-\s*(Files|Functions|Shell files|Shell embedded Python):\s*\*\*(\d+)\*\*\s*lines\.", re.MULTILINE
)
_EXPECTED_THRESHOLDS = frozenset({"Files", "Functions", "Shell files", "Shell embedded Python"})


def _discovery_thresholds() -> dict[str, int]:
    """The sizes at or over which a hotspot must be listed, read from the page.

    The page declares these rather than duplicating them here, so a reader and
    this suite cannot disagree about where the line sits: editing the stated
    number changes what is enforced, in the same commit and in one place.
    """
    found = dict(_THRESHOLD.findall(_section_body("Discovery thresholds")))
    assert set(found) == _EXPECTED_THRESHOLDS, (
        f"'## Discovery thresholds' in {HOTSPOTS.name} must state a threshold for each of "
        f"{sorted(_EXPECTED_THRESHOLDS)} as '- Files: **N** lines.'; parsed {sorted(found)}"
    )
    return {name: int(value) for name, value in found.items()}


def _section_body(heading: str) -> str:
    text = HOTSPOTS.read_text(encoding="utf-8")
    marker = f"\n## {heading}\n"
    start = text.find(marker)
    assert start != -1, f"missing '## {heading}' section in {HOTSPOTS.name}"
    start += len(marker)
    end = text.find("\n## ", start)
    return text[start:] if end == -1 else text[start:end]


def test_listed_files_exist_and_record_their_true_line_count():
    stale = []
    for rel, (recorded, _ceiling) in _parse_table("Long files").items():
        path = ROOT / rel
        assert path.is_file(), f"{HOTSPOTS.name} lists a file that no longer exists: {rel}"
        actual = _file_loc(path)
        if actual != recorded:
            stale.append(f"  {rel}: page says {recorded}, actual {actual}")
    assert not stale, "Long files table is out of date. Update the LOC column in the same commit:\n" + "\n".join(stale)


def test_listed_functions_exist_and_record_their_true_span():
    stale = []
    for subject, (recorded, _ceiling) in _parse_table("Long functions").items():
        rel, _, name = subject.partition("::")
        assert name, f"{HOTSPOTS.name} function entry must use 'path::name' form: {subject}"
        path = ROOT / rel
        assert path.is_file(), f"{HOTSPOTS.name} lists a file that no longer exists: {rel}"
        spans = _function_spans(path)
        assert name in spans, (
            f"{HOTSPOTS.name} lists {subject}, but no module-level function or method by that name exists"
        )
        if spans[name] != recorded:
            stale.append(f"  {subject}: page says {recorded}, actual {spans[name]}")
    assert not stale, "Long functions table is out of date. Update the LOC column in the same commit:\n" + "\n".join(
        stale
    )


def test_every_large_file_is_listed():
    file_threshold = _discovery_thresholds()["Files"]
    listed = set(_parse_table("Long files"))
    missing = []
    for path in _tracked_python_files():
        loc = _file_loc(path)
        if loc >= file_threshold and path.relative_to(ROOT).as_posix() not in listed:
            missing.append(f"  {path.relative_to(ROOT).as_posix()}: {loc} lines")
    assert not missing, (
        f"files at or over {file_threshold} lines are missing from the 'Long files' table in "
        f"{HOTSPOTS.name}. Add a row with its LOC, a soft ceiling, and a note, or extract it:\n" + "\n".join(missing)
    )


def test_every_long_function_is_listed():
    function_threshold = _discovery_thresholds()["Functions"]
    listed = set(_parse_table("Long functions"))
    missing = []
    for path in _tracked_python_files():
        rel = path.relative_to(ROOT).as_posix()
        for name, loc in _function_spans(path).items():
            if loc > function_threshold and f"{rel}::{name}" not in listed:
                missing.append(f"  {rel}::{name}: {loc} lines")
    assert not missing, (
        f"functions over {function_threshold} lines are missing from the 'Long functions' table in "
        f"{HOTSPOTS.name}. Add a row with its LOC, a soft ceiling, and a note, or extract it:\n" + "\n".join(missing)
    )


def _tracked_shell_scripts() -> list[Path]:
    return host_scripts._tracked_shell_scripts()


def _embedded_python_loc(path: Path) -> int:
    """Lines inside this script's ``python3 - ... <<'PY'`` heredoc bodies."""
    return sum(len(body.splitlines()) for _, body in host_scripts._embedded_python_heredocs(path))


# Subjects in the embedded-Python table carry this suffix so a script can appear
# in both shell tables without the two rows colliding in `_rows_anywhere`, which
# keys every row in a file by subject alone.
_EMBEDDED = "::embedded-python"


def test_listed_shell_scripts_record_their_true_line_count():
    stale = []
    for rel, (recorded, _ceiling) in _parse_table("Long shell scripts").items():
        path = ROOT / rel
        assert path.is_file(), f"{HOTSPOTS.name} lists a script that no longer exists: {rel}"
        actual = _file_loc(path)
        if actual != recorded:
            stale.append(f"  {rel}: page says {recorded}, actual {actual}")
    assert not stale, (
        "Long shell scripts table is out of date. Update the LOC column in the same commit:\n" + "\n".join(stale)
    )


def test_listed_shell_scripts_record_their_true_embedded_python_count():
    stale = []
    for subject, (recorded, _ceiling) in _parse_table("Embedded Python in shell").items():
        rel, separator, suffix = subject.partition("::")
        assert separator and f"::{suffix}" == _EMBEDDED, (
            f"{HOTSPOTS.name} embedded-Python entry must use 'path{_EMBEDDED}' form: {subject}"
        )
        path = ROOT / rel
        assert path.is_file(), f"{HOTSPOTS.name} lists a script that no longer exists: {rel}"
        actual = _embedded_python_loc(path)
        if actual != recorded:
            stale.append(f"  {subject}: page says {recorded}, actual {actual}")
    assert not stale, (
        "Embedded Python in shell table is out of date. Update the LOC column in the same commit:\n" + "\n".join(stale)
    )


def test_every_large_shell_script_is_listed():
    threshold = _discovery_thresholds()["Shell files"]
    listed = set(_parse_table("Long shell scripts"))
    missing = []
    for path in _tracked_shell_scripts():
        loc = _file_loc(path)
        if loc >= threshold and path.relative_to(ROOT).as_posix() not in listed:
            missing.append(f"  {path.relative_to(ROOT).as_posix()}: {loc} lines")
    assert not missing, (
        f"shell scripts at or over {threshold} lines are missing from the 'Long shell scripts' table in "
        f"{HOTSPOTS.name}. Add a row with its LOC, a soft ceiling, and a note, or split it:\n" + "\n".join(missing)
    )


def test_every_large_embedded_python_body_is_listed():
    """A script can stay under the file threshold while its heredocs grow.

    That is the direction that hurts: heredoc bodies are the code Ruff, Mypy and
    coverage never read, so growth there buys less checking per line than
    growth anywhere else in the repository.
    """
    threshold = _discovery_thresholds()["Shell embedded Python"]
    listed = set(_parse_table("Embedded Python in shell"))
    missing = []
    for path in _tracked_shell_scripts():
        loc = _embedded_python_loc(path)
        rel = path.relative_to(ROOT).as_posix()
        if loc >= threshold and f"{rel}{_EMBEDDED}" not in listed:
            missing.append(f"  {rel}: {loc} embedded python lines")
    assert not missing, (
        f"scripts with {threshold} or more embedded Python lines are missing from the "
        f"'Embedded Python in shell' table in {HOTSPOTS.name}. Add a row, or move the body into a "
        f"module under ops/ where the Python gates can reach it:\n" + "\n".join(missing)
    )


# Every other check here asks whether one entry is honestly recorded. None of
# them asks whether the page as a whole is getting better or worse, and the
# answer to that was free to drift: an entry could grow by 200 lines and stay
# compliant by restating its LOC, or step over its ceiling and stay compliant by
# raising the ceiling. Both are one quiet edit.
#
# These totals close that. They are restated exactly, like every other number on
# the page, and the rule for restating them is that they may only go **down**.
# Raising one is not impossible -- nothing here can stop a deliberate edit -- but
# it is now a visible line in the diff that says the codebase got worse, next to
# the entry that did it.
#
# The ceiling sum is what stops the obvious dodge. Overage alone can be reduced
# to zero by raising every ceiling to meet its entry; holding the sum of the
# ceilings fixed means that trade has to be made in the open, as two numbers
# moving instead of one.
_RATCHET: dict[str, tuple[int, int, int]] = {
    # section: (entries over ceiling, total overage, sum of ceilings)
    #
    # "Long files": 5261 -> 5482 -> 5734 -> 5295. The two rises were the same
    # deliberate trade -- splitting a task body into named phase functions pays
    # file lines in signatures and docstrings to buy function clarity, which
    # cost 221 lines for `reconcile_libvirt_resources` and 252 for `spawn_vm`
    # while returning 691 lines to the function table and retiring the page's
    # two worst entries. The fall gave back more than the first rise: moving
    # those phases into `playground/reconciliation_phases.py` took
    # `reconciliation.py` from 1439 lines to 715, under the discovery
    # threshold, so it left this table entirely. 5295 -> 4869: the same move
    # applied to `orchestrator.py`, whose capture and acquisition group is now
    # `playground/acquisition.py` (2615 -> 2186, -429), against +3 in
    # `verification_service.py` for the reformatted call to the moved helper.
    # Unlike `reconciliation.py` this does not clear the table -- `orchestrator.py`
    # stays over its ceiling until the remaining domains move too. 4869 -> 4694:
    # network create/destroy is now `playground/network_lifecycle.py`
    # (2186 -> 2011). `_wait_for_active_networks` deliberately stayed behind;
    # its only caller is `spawn_vm`, so it is shared infrastructure rather than
    # network-domain code, and moving it would have pushed `spawn_vm` two lines
    # further over its own ceiling to no purpose. 4694 -> 4533: workload
    # provisioning is now `playground/workload_provisioning.py` (2011 -> 1850).
    # `_dispatch_workload_provisioning` stayed for the same reason -- it is named
    # for the workload domain but publishes one message and then tears the VM
    # down, so it is spawn-path cleanup and goes with the VM lifecycle slice.
    # 4533 -> 4366: NIC attach/detach is now `playground/nic_lifecycle.py`
    # (1850 -> 1683). `_reserve_nic_rows` and `_resolve_nic_addresses` stayed
    # for the same reason as `_dispatch_workload_provisioning` -- NIC-named,
    # spawn-path code. Three of the four slices so far have left something
    # behind whose name pointed at the wrong domain.
    # 4366 -> 3742. `orchestrator.py` left this table -- 1683 to 810, under its
    # ceiling and under the 900-line discovery threshold, the way
    # `reconciliation.py` did, and it is the file the original review named. But
    # the code it shed did not leave the repository: `playground/vm_lifecycle.py`
    # arrives at 959 lines and is listed here at the discovery threshold rather
    # than given a comfortable ceiling, so the entry count goes 10 -> 11 and the
    # overage carries its 59.
    #
    # The first version of this restatement claimed (10, 3683, 16500) -- it
    # dropped the old rows without adding the new module's, and the discovery
    # gate caught it. An extraction moves an entry between rows; it only removes
    # one when the destination is under threshold too.
    #
    # 3742 -> 3683, and it is (10, 3683, 16500) now: the same three numbers that
    # were wrong a moment ago, correct this time for the reason they were wrong
    # then. The cloud-init seed group left `vm_lifecycle.py` for
    # `playground/cloud_init_seed.py`, 959 lines to 890, under both its ceiling
    # and the 900-line discovery threshold, and the destination is 111 lines. This
    # time the destination really is under threshold, so the row is removed rather
    # than moved and the 59 it carried goes with it. The way to tell the two cases
    # apart is to measure the destination, not to reason about the source: the
    # earlier restatement asserted `vm_lifecycle.py` had left the table when it
    # was 959 lines.
    #
    # `spawn_vm` keeps its long-functions row at 189 against 180. The group came
    # out of `_boot_playground_vm`, not out of it, so nothing about that entry
    # changed and the long-functions ratchet does not move.
    # 3683 -> 3798: reviewed correctness/scalability growth for the quality-review
    # fixes. Package-service Prepare custody/deletion guards add 90 lines and
    # bounded Jobs history/filter queries add 25 repository lines. Entry count
    # and every soft ceiling remain unchanged; this restates the accepted debt
    # rather than hiding it by raising a ceiling or moving unrelated code.
    # 3798 -> 3813: reviewed full-project custody repairs add four GC transaction
    # lines, six busy-read response lines and five reset-source validation lines.
    # Keep every ceiling and entry count; record this bounded correctness debt.
    # +6 lines make completed shutdown requests destroyable without stealing
    # active power-operation claims. Ceilings and entry count are unchanged.
    # R32 adds 57 lines for immutable import adoption under digest locks. R33
    # brings Prepare to 963 lines with explicit retention/mutation authority and
    # non-destructive recovery. List it at the 900-line discovery threshold, not
    # a comfortable ceiling: +63 debt and one entry. Preserve every old ceiling.
    "Long files": (11, 3939, 17400),
    # Back to (19, 1155, 3810), exactly the pre-split numbers, and that is the
    # honest reading: `spawn_vm` is 189 lines against a 180 ceiling in
    # `vm_lifecycle.py` now instead of in `orchestrator.py`. It moved modules, it
    # was not cut. A 33-line wrapper took its old place, which is under
    # threshold and therefore unlisted -- that is the only thing that changed.
    # 1155 -> 1159. A deliberate rise: `run_command` gained 4 lines redacting
    # secrets out of the command it logs. The alternative was logging the
    # unredacted command, so the overage buys the redaction rather than paying
    # for growth. Entry count and ceiling sum are unchanged.
    # 1159 -> 1152, with one fewer over-budget function: Prepare authority moves
    # run_prepare from 201 to 41 lines (-21 overage), against +5 in reconciliation
    # and +9 in the launch deletion guard. All soft ceilings stay unchanged.
    # Baseline 12b574b already has two additional boot_and_prepare lines. This
    # recount corrects stale documentation; the function and ceiling are unchanged.
    # 1154 -> 1160: five capture lines require the current disk and real output;
    # one spawn argument carries the source into its locked admission check.
    # New helpers remain outside the existing large bodies; ceilings unchanged.
    # 1160 -> 1163: JobRunDeps.default passes the guest address to the workload
    # cache save, which flushes the guest before freezing its disk; an unflushed
    # cache served zero-length files. Entry count and ceilings are unchanged.
    "Long functions": (18, 1163, 3810),
    # "Long shell scripts": 1735 -> 1407, and five entries over ceiling to four.
    # `ops/deploy/uninstall.sh` left the table: 819 lines to 349, under both its
    # ceiling and the discovery threshold, once its six heredoc bodies became
    # `ops/deploy/host_access.py revoke-all` and `ops/deploy/privileged_state.py`.
    # The same two modules took 88 lines out of `install.sh` and 21 out of
    # `configure-privileged-roots.sh`, which had their own copies of two of them.
    # 1407 -> 1450. A deliberate rise: `astinuslab.sh` gained 43 lines giving
    # each service its own log file under storage/logs, so the launcher and an
    # installed deployment present one set of logs rather than two, plus a
    # readiness probe. The launcher was already the separable half of this
    # script; this makes it larger, not more tangled. Entry count and ceiling
    # sum are unchanged.
    # +2 explanatory lines for immutable read/traverse normalization (R19).
    # Executable behavior remains one chmod; ceilings and entry count unchanged.
    # +2 lines require a nonempty packaged migration head in native acceptance.
    # +13 lines drain compressed provenance input while retaining all failure statuses.
    # +1 line declares the custom-storage disk probe read-only, matching its ACL.
    # R29/R31 deployment hooks and R34 rollback-state verification add 87
    # installer lines; role-log routing adds 14 launcher lines. The canonical
    # matrix adds 47 for configured DB paths, custody and read-only log proof.
    # Accept this correctness debt explicitly; keep all four ceilings unchanged.
    # +22 matrix lines explicitly distinguish first-release applicability from
    # historical upgrade proof, reject conflicting inputs and retain provenance.
    # The existing row bodies stay intact; ceilings and entry count are unchanged.
    # +3 assert the exact evidence traversal ACL as well as its custody mode.
    # +1 reapplies evidence-read ACLs after split-upgrade normalization.
    # +11 retain exclusive private custody metadata receipts for native diagnosis.
    # +2 explain why custody probes precede migration and avoid image discovery.
    # +2 provision the absent dumps directory before those probes are seeded.
    "Long shell scripts": (4, 1657, 5300),
    # "Embedded Python in shell": the whole table is within budget for the first
    # time. The single entry over ceiling was `uninstall.sh` at 480 against 300;
    # it is now 0, and the row is gone. Nothing here may rise without the sum of
    # the ceilings rising too, so the next heredoc body to grow past 250 lines
    # has to be added as a row rather than absorbed.
    "Embedded Python in shell": (0, 0, 1100),
}


def _overage(section: str) -> tuple[int, int, int]:
    """How far one table is over its own budgets, in entries and in lines."""
    rows = _parse_table(section)
    over = [(loc, ceiling) for loc, ceiling in rows.values() if loc > ceiling]
    return (
        len(over),
        sum(loc - ceiling for loc, ceiling in over),
        sum(ceiling for _, ceiling in rows.values()),
    )


@pytest.mark.parametrize("section", sorted(_RATCHET))
def test_the_overage_ratchet_is_restated_exactly(section):
    recorded = _RATCHET[section]
    actual = _overage(section)
    if actual == recorded:
        return

    labels = ("entries over ceiling", "total overage", "sum of ceilings")
    moved = [
        f"  {label}: ratchet says {was}, actual {now} ({'worse' if now > was else 'better'})"
        for label, was, now in zip(labels, recorded, actual, strict=True)
        if was != now
    ]
    direction = "worse" if any(now > was for was, now in zip(recorded, actual, strict=True)) else "better"
    guidance = (
        "Restate the numbers in _RATCHET in the same commit."
        if direction == "better"
        else (
            "This table got worse. Restating _RATCHET upward is allowed only as a deliberate, "
            "reviewed decision -- prefer bringing the entry back under instead."
        )
    )
    raise AssertionError(f"'{section}' has moved:\n" + "\n".join(moved) + f"\n{guidance}")


def _rows_anywhere(path: Path) -> dict[str, tuple[int, int]]:
    """Every table row in a file, found without relying on the heading text.

    Translated pages localise their headings, so section lookup does not carry
    across languages; the rows themselves are language-independent because the
    subject is a code path and the two numbers are measurements.
    """
    rows: dict[str, tuple[int, int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match is not None:
            rows[match.group(1)] = (int(match.group(2)), int(match.group(3)))
    return rows


@pytest.mark.parametrize("language", TRANSLATIONS)
def test_translation_tables_match_the_english_page(language):
    translated = HOTSPOTS.with_suffix(f".{language}.md")
    assert translated.is_file(), f"missing translation: {translated.name}"
    english = _rows_anywhere(HOTSPOTS)
    other = _rows_anywhere(translated)

    only_english = sorted(set(english) - set(other))
    only_translated = sorted(set(other) - set(english))
    differing = sorted(subject for subject in set(english) & set(other) if english[subject] != other[subject])

    problems = []
    problems += [f"  missing from {translated.name}: {subject}" for subject in only_english]
    problems += [f"  listed only in {translated.name}: {subject}" for subject in only_translated]
    problems += [f"  {subject}: en={english[subject]} {language}={other[subject]}" for subject in differing]
    assert not problems, (
        f"{translated.name} must carry the same rows and numbers as {HOTSPOTS.name}; only the prose and "
        f"the Notes column are translated:\n" + "\n".join(problems)
    )


def test_discovery_thresholds_are_stated_and_usable():
    """A threshold nobody can read, or one loose enough to catch nothing, is not a budget."""
    thresholds = _discovery_thresholds()
    file_threshold, function_threshold = thresholds["Files"], thresholds["Functions"]
    listed_files = _parse_table("Long files")
    listed_functions = _parse_table("Long functions")
    assert any(loc >= file_threshold for loc, _ in listed_files.values()), (
        f"no listed file reaches the stated {file_threshold}-line file threshold, so it enforces nothing"
    )
    assert any(loc > function_threshold for loc, _ in listed_functions.values()), (
        f"no listed function passes the stated {function_threshold}-line function threshold, so it enforces nothing"
    )
    shell_threshold = thresholds["Shell files"]
    assert any(loc >= shell_threshold for loc, _ in _parse_table("Long shell scripts").values()), (
        f"no listed script reaches the stated {shell_threshold}-line shell threshold, so it enforces nothing"
    )
    embedded_threshold = thresholds["Shell embedded Python"]
    assert any(loc >= embedded_threshold for loc, _ in _parse_table("Embedded Python in shell").values()), (
        f"no listed script reaches the stated {embedded_threshold}-line embedded-Python threshold, "
        f"so it enforces nothing"
    )
