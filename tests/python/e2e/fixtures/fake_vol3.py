#!/usr/bin/env python3
"""A stand-in for the operator-supplied Volatility 3 executable, for tests only.

This project never ships Volatility, so until now nothing below
``tests/python/host_real/`` could run a plugin: the workbench could be driven as
far as "the run control is blocked because no tool is configured" and no
further. What a *completed* run attributes its results to -- which artifact,
which session, which symbol table, which binary -- was therefore proved only at
service level, in the one place a misattribution would be least visible.

Pointing ``[vol3].volatility_bin`` at this file unblocks that, and deliberately
changes nothing in the product: there is no fake-analysis branch in
``vol3_plugin_run.py``. The whole real pipeline runs -- executable verification,
symbol staging, the subprocess, the saved-config ISF pin check, JSON parsing,
severity flagging, provenance and artifact attribution. Only the framework that
would read the memory image is absent.

Two things keep that honest:

* the run's provenance records the executable it actually ran -- absolute path,
  sha256 and shebang -- so a report produced through this file names this file.
  ``test_volatility_run_ui.py`` asserts exactly that rather than trusting it.
* every row it emits is visibly synthetic. It never reads the dump it is given;
  the process list below is a constant, and says so in its own data.

It implements only what the product actually invokes: the readiness probe, and
one plugin run with the JSON renderer.

``ASTINUSLAB_FAKE_VOL3_MODE`` makes it misbehave on purpose, so the workbench's
failure branches can be driven from a browser instead of only from a service
test. See ``MODES`` for what each one does. The readiness probe succeeds in
every mode: a tool that fails its own probe never reaches a run, and the
question these modes exist to ask is what the operator is told when a run that
*was* allowed to start goes wrong.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The version the product treats as tested. Reporting a plausible one is the
# point: the readiness gate is real code under test, and a run must reach the
# same "ready" state a real install reaches.
BANNER = "Volatility 3 Framework 2.28.0"

# Constant, and self-describing. Nothing here is read from the memory image --
# anyone looking at a report produced through this file should be able to tell
# from the rows alone, without checking the provenance block.
ROWS: list[dict[str, object]] = [
    {"PID": 1, "TID": 1, "PPID": 0, "COMM": "astinuslab-fake-vol3"},
    {"PID": 412, "TID": 412, "PPID": 1, "COMM": "synthetic-row-not-evidence"},
    {"PID": 907, "TID": 907, "PPID": 1, "COMM": "no-memory-image-was-read"},
]


MODE_ENV = "ASTINUSLAB_FAKE_VOL3_MODE"
# A file this process appends to once per plugin run, so ``after_first:`` below
# can tell the session's first run from the ones after it. Set by the conftest
# fixture that starts the server; a mode that needs it and does not have it is
# a hard stop, like an unknown mode.
MARKER_ENV = "ASTINUSLAB_FAKE_VOL3_MARKER"
# ``after_first:exit_nonzero`` succeeds once and then fails that way for the
# rest of the session. Without it a browser test can only ever see a failure
# with nothing before it, and "a failure clears the previous run's results off
# the screen" is unfalsifiable -- there is no previous run, and an empty table
# and a disabled export button are the page's own starting state.
AFTER_FIRST = "after_first:"

# What each mode makes a *plugin run* do. The readiness probe is unaffected.
MODES = {
    "": "behave",
    # The commonest real failure: symbols that do not fit the image. Volatility
    # says so on stderr and exits non-zero; the product's job is to carry the
    # tool's own words to the operator rather than replace them with its own.
    "exit_nonzero": "exit 3 after writing a plausible Volatility error to stderr",
    # Exit 0 with output the JSON renderer did not finish writing -- what a
    # killed or out-of-memory vol3 leaves behind.
    "unparseable_json": "exit 0 with a truncated JSON array on stdout",
    # Emit rows, but report having resolved the symbol table from the host's
    # own cache rather than the directory the product staged. The rows would
    # parse; the run must still be refused, because they were produced against
    # symbols the product cannot account for.
    "foreign_isf": "exit 0, but pin an ISF outside the staged symbols directory",
}

# Realistic enough that a test asserting the operator sees it is asserting the
# tool's words reached the screen, not that a constant round-tripped.
STDERR_DETAIL = (
    "volatility3.framework.exceptions.UnsatisfiedException: Unable to validate "
    "the plugin requirements: ['plugins.PsList.kernel.layer_name']"
)
# Where a real vol3 keeps symbol tables it downloaded itself. Never inside the
# directory the product stages the operator's ISF into, which is the point.
# Deliberately shares no substring with ROWS: a test asserting that a refused
# run's rows are absent from the page reads this path there otherwise, and
# passes or fails for the wrong reason.
FOREIGN_ISF_PATH = "/var/tmp/unstaged-symbol-cache/linux/kernel.json"


def _stop(reason: str) -> None:
    """Refuse rather than quietly running the happy path.

    A typo in a test must fail that test, not silently assert against
    behaviour nobody asked for. Same rule as the fake orchestration runtime's
    refusing ``run_command``.
    """
    sys.stderr.write(f"fake_vol3: {reason}\n")
    raise SystemExit(64)


def _runs_so_far() -> int:
    """How many plugin runs this session has already made, this one included."""
    marker = os.environ.get(MARKER_ENV, "")
    if not marker:
        _stop(f"{MODE_ENV} uses {AFTER_FIRST!r} but {MARKER_ENV} is unset")
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("run\n")
    return len(path.read_text(encoding="utf-8").splitlines())


def _mode() -> str:
    """The mode for *this* plugin run, or a hard stop."""
    configured = os.environ.get(MODE_ENV, "")
    mode = configured[len(AFTER_FIRST) :] if configured.startswith(AFTER_FIRST) else configured
    if mode not in MODES:
        _stop(f"unknown {MODE_ENV}={configured!r}; known: {sorted(MODES)}")
    if configured.startswith(AFTER_FIRST) and _runs_so_far() == 1:
        return ""
    return mode


def _option(argv: list[str], name: str) -> str | None:
    """The value of ``--name VALUE``, or None."""
    if name not in argv:
        return None
    index = argv.index(name)
    return argv[index + 1] if index + 1 < len(argv) else None


def _isf_urls(argv: list[str]) -> dict[str, str]:
    """The ``-e key=value`` pairs whose key pins a symbol table.

    Real Volatility writes its resolved configuration to ``--save-config``, and
    the product refuses a run whose saved config does not pin an ISF inside the
    staged symbols directory. Echoing back what was asked for reproduces that,
    so the pin check stays a real check: point ``--symbol-dirs`` elsewhere and
    the product still rejects the run.
    """
    pinned: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token != "-e" or index + 1 >= len(argv):
            continue
        key, _, raw = argv[index + 1].partition("=")
        if key.endswith(".isf_url") and raw:
            try:
                pinned[key] = json.loads(raw)
            except json.JSONDecodeError:
                pinned[key] = raw
    return pinned


def main(argv: list[str]) -> int:
    # Deliberately before the mode check: readiness is how the product decides
    # the tool exists at all, and every mode here is about a run that was
    # allowed to start.
    if any(token.startswith("frameworkinfo") for token in argv):
        print(BANNER)
        return 0

    mode = _mode()

    if mode == "exit_nonzero":
        sys.stderr.write(f"{STDERR_DETAIL}\n")
        return 3

    config_path = _option(argv, "--save-config")
    if config_path:
        pinned = _isf_urls(argv)
        if mode == "foreign_isf":
            pinned = {key: Path(FOREIGN_ISF_PATH).as_uri() for key in pinned} or {
                "plugins.PsList.kernel.isf_url": Path(FOREIGN_ISF_PATH).as_uri()
            }
        Path(config_path).write_text(json.dumps(pinned, indent=2), encoding="utf-8")

    if mode == "unparseable_json":
        # A prefix of the real output, cut mid-row. Nothing downstream should
        # be able to salvage rows from it and present them as results.
        sys.stdout.write(json.dumps(ROWS)[:40])
        return 0

    json.dump(ROWS, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
