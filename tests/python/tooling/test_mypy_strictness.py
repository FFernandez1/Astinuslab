"""The type-strictness ratchet for `src/astinuslab`.

`pyproject.toml` turns on every strict flag the tree already satisfies, so
those cannot regress: Mypy fails the build. This file covers the other half --
the flags that are not affordable yet, whose cost was previously recorded only
in a config comment dated 2026-07-09 and had no way of being wrong out loud.

`mypy src --strict` is the full set. The gap between that and the configured
strictness is this project's typing debt, counted per error code and restated
exactly, like every other ratchet here. The rule for restating is that the
numbers may only go **down**.

Why per code rather than one total: the codes are not the same kind of work.
`no-untyped-def` is 547 signatures nobody has annotated, mostly mechanical.
`no-any-return` is 40 places where a function's declared return type is a lie.
Collapsing them into one number lets a hundred easy annotations hide a new
returned Any, which is the error class most likely to be an actual defect.

Scope is `src/astinuslab`. The test tree is far less typed and much of it
never will be; holding production code to a moving floor is the point.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [pytest.mark.unit]

# Strict flags already enabled tree-wide in pyproject.toml, listed here so a
# reader can tell what this debt is the remainder *of*. Mypy enforces these
# directly; nothing in this file needs to.
ALREADY_ENFORCED = (
    "check_untyped_defs",
    "no_implicit_optional",
    "warn_redundant_casts",
    "strict_equality",
    "extra_checks",
    "disallow_subclassing_any",
    "warn_unused_ignores",
)

# Remaining `--strict` findings in src/astinuslab, by error code.
#
# These may only be restated downward. Raising one means production code got
# less typed than it was, which is a decision to make deliberately and in the
# open, not a number to quietly bump.
STRICT_DEBT: dict[str, int] = {
    # Functions with no annotations at all. The bulk, and the most mechanical.
    # 547 -> 546: the worker task-signal handlers are annotated, which is one
    # more than the observability work added.
    "no-untyped-def": 546,
    # Bare generics: `dict` where `dict[str, int]` is meant. 248 -> 247:
    # `attach_nic` returns `{"nic_id": ..., "mac": ...}` and was annotated bare.
    # Splitting it into a task wrapper and a body would have made that two bare
    # annotations instead of one, so it was annotated rather than restated up.
    #
    # 247 -> 246 was recorded here as the same thing happening to `spawn_vm`'s
    # `nic_specs: list[dict]`. That was wrong, and the Part 1 review of the
    # Playground split caught it. The signature that lost its bare generic in the
    # VM slice was `_reserve_nic_rows`, which nothing recorded; `spawn_vm`'s split
    # annotated the *wrapper* in `orchestrator.py` and left the body in
    # `vm_lifecycle.py` bare, so for two slices the wrapper was better typed than
    # the function it delegates to. The count was right and the reason was not,
    # which is the failure mode a per-code ratchet is supposed to prevent: the
    # total moved in the right direction and nobody checked which signature moved
    # it. 246 -> 245: the body is annotated now, so wrapper and body agree.
    "type-arg": 245,
    # Calls into the untyped functions counted above; falls with them.
    "no-untyped-call": 58,
    # Celery task decorators, which are untyped upstream. 42 -> 48: a deliberate
    # rise. Six are `@task_prerun.connect` and its siblings in
    # `jobs/celery_worker_signals.py`, plus the `@shared_task` the eager-mode
    # request-id handling added. Annotating the handlers does not clear these --
    # the decorator is what is untyped, not the function it wraps -- so the only
    # way down is Celery shipping types or these handlers not existing.
    "untyped-decorator": 48,
    # A declared return type that the body does not actually guarantee. The
    # smallest count here and the likeliest to be hiding a real defect.
    "no-any-return": 40,
    # Re-exports that are not declared as such. 9 -> 5: `orchestrator` now
    # declares `settings`, `session_scope` and `libvirt` explicitly, because
    # `acquisition.py` reads them back through the module object on purpose.
    # That also cleared the same complaint in `lab_packages/preflight.py`,
    # `playground/routes/vms.py` and `playground/routes/vm_actions.py`, which
    # were already reaching through `orchestrator` the same way.
    "attr-defined": 5,
}

_CODE = re.compile(r"\[([a-z][a-z-]*)\]\s*$")


def _strict_errors_by_code() -> Counter[str]:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "src", "--strict", "--no-incremental", "--cache-dir=/dev/null"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # Mypy exits non-zero precisely because there are findings, so the return
    # code carries no information here. A crash does: it produces no parseable
    # lines at all, which the emptiness check below turns into a failure rather
    # than into a free pass.
    counts: Counter[str] = Counter()
    for line in result.stdout.splitlines():
        if ": error:" not in line:
            continue
        match = _CODE.search(line)
        if match is not None:
            counts[match.group(1)] += 1
    assert counts, (
        "no mypy findings parsed at all -- the run probably failed rather than passed:\n"
        + (result.stdout or result.stderr)[-2000:]
    )
    return counts


@pytest.mark.slow
def test_the_strict_debt_is_restated_exactly():
    actual = _strict_errors_by_code()

    recorded = set(STRICT_DEBT)
    seen = set(actual)
    problems = []
    for code in sorted(seen - recorded):
        problems.append(f"  {code}: new error code, {actual[code]} findings, not in STRICT_DEBT")
    for code in sorted(recorded - seen):
        problems.append(f"  {code}: gone from the tree ({STRICT_DEBT[code]} recorded) -- drop the entry")
    for code in sorted(recorded & seen):
        was, now = STRICT_DEBT[code], actual[code]
        if was != now:
            direction = "worse" if now > was else "better"
            problems.append(f"  {code}: recorded {was}, actual {now} ({direction})")

    if not problems:
        return

    got_worse = any(actual.get(code, 0) > was for code, was in STRICT_DEBT.items()) or bool(seen - recorded)
    guidance = (
        "Production code got less typed. Restating STRICT_DEBT upward is a deliberate, reviewed "
        "decision -- prefer annotating what you touched instead."
        if got_worse
        else "Restate STRICT_DEBT in the same commit."
    )
    raise AssertionError("`mypy src --strict` has moved:\n" + "\n".join(problems) + f"\n{guidance}")


def test_the_flags_this_ratchet_excludes_are_actually_enforced():
    """Every flag named in ALREADY_ENFORCED is on in pyproject.toml.

    Without this, deleting a global flag would silently move its findings into
    the debt above, where restating the number would look like ordinary drift.
    """
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = config[config.index("[tool.mypy]") : config.index("[[tool.mypy.overrides]]")]

    missing = [flag for flag in ALREADY_ENFORCED if f"{flag} = true" not in section]

    assert not missing, "these strict flags are recorded as tree-wide but are not set in [tool.mypy]: " + ", ".join(
        missing
    )
