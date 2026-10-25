# Host nightly runner provisioning

Operator runbook for the `[self-hosted, libvirt]` runner that executes
`.github/workflows/host-nightly.yml`. It covers only what that runner needs on
top of a working libvirt/KVM host: the **operator-supplied** Volatility 3 and
`dwarf2json` tools, and the GitHub Actions variables that point AstinusLab at
them.

The workflow's nightly schedule is commented out until this runner exists, so
it currently runs only when dispatched by hand. Restore the `schedule:` block
once the runner is registered and green.

For the product-level tool runbook — install, verify, readiness codes, upgrade
and rollback — read [Volatility 3](../user/VOLATILITY3.md) first. This page does
not repeat it; it adds the CI-specific wiring.

## The boundary this page must not cross

AstinusLab never downloads, installs, vendors, or imports Volatility 3 or
`dwarf2json`. That boundary is load-bearing for licensing. Provisioning both
tools is an **operator action performed outside the AstinusLab tree**, by hand,
on the runner. No workflow step, script, or test in this repository may fetch
either tool, and the nightly asserts that both resolve to paths outside the
checkout.

## 1. Install the tools on the runner, outside the tree

Do this as the runner's service user, in operator-owned versioned paths. Review
the upstream licenses yourself before installing; see `THIRD_PARTY_NOTICES.md`.

```bash
# Volatility 3 — the supported banner is exactly "Volatility 3 Framework 2.28.0"
python3.12 -m venv /opt/volatility3-2.28.0
/opt/volatility3-2.28.0/bin/python -m pip install 'volatility3==2.28.0'
/opt/volatility3-2.28.0/bin/vol -q frameworkinfo.FrameworkInfo
```

Obtain or build `dwarf2json` from its upstream project into a separate operator
path, for example `/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64`, and make it
executable by the runner's service user.

Neither path may live under the runner's `_work` checkout directory. A tool
inside the checkout would be wiped by the next `actions/checkout`, and it would
turn an operator-supplied tool into a vendored one.

## 2. Record the exact SHA-256

The pin is per-file, not per-version: it must be the hash of the exact binary the
runner will execute.

```bash
sha256sum /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
```

Keep the 64-hex digest, the upstream tag or commit, the artifact origin, and the
architecture together in your provenance record. Whenever the file is replaced,
the path and the pin change as a matched pair.

## 3. Set the GitHub Actions variables

Set these three as repository (or environment) **variables** — `Settings →
Secrets and variables → Actions → Variables`. The workflow maps each one onto the
`ASTINUSLAB_*` setting override the backend reads.

| Actions variable | Backend setting it feeds | Value |
| --- | --- | --- |
| `HOST_RUNNER_VOL3_VOLATILITY_BIN` | `ASTINUSLAB_VOL3_VOLATILITY_BIN` (`[vol3].volatility_bin`) | Absolute path to the operator's `vol` executable, e.g. `/opt/volatility3-2.28.0/bin/vol` |
| `HOST_RUNNER_VOL3_DWARF2JSON_PATH` | `ASTINUSLAB_VOL3_DWARF2JSON_PATH` (`[vol3].dwarf2json_path`) | Absolute path to the operator's `dwarf2json` executable |
| `HOST_RUNNER_VOL3_DWARF2JSON_SHA256` | `ASTINUSLAB_VOL3_DWARF2JSON_SHA256` (`[vol3].dwarf2json_sha256`) | The 64-hex digest from step 2 |

The workflow also pins `ASTINUSLAB_VOL3_ENABLED=true` so a stale local
`astinuslab.toml` on the runner cannot silently disable the feature.

Variables, not secrets, on purpose: none of the three is a credential, and
masking the pin would redact it from the very failure output that makes a hash
mismatch diagnosable. If your threat model requires the runner's filesystem
layout to stay out of logs, move the two path values to secrets and change the
`vars.` references to `secrets.` — but leave the pin as a variable.

## 4. Other host prerequisites for the ISF path

The Volatility plugin row builds a real ISF when the runner has none cached for
the target kernel. That path runs the production ISF-only job, whose preflight
requires these host tools on top of libvirt: `rpm2cpio`, `cpio`,
`virt-customize` (libguestfs-tools), `qemu-img`, and `virsh`. Install them from
the distro packages; they are ordinary host dependencies, not third-party
forensic tooling, and the preflight names whichever one is missing.

## 5. Verify the provisioning

From the runner, with the same environment the workflow builds:

```bash
ASTINUSLAB_REAL_HOST=1 \
ASTINUSLAB_VOL3_ENABLED=true \
ASTINUSLAB_VOL3_VOLATILITY_BIN=/opt/volatility3-2.28.0/bin/vol \
ASTINUSLAB_VOL3_DWARF2JSON_PATH=/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64 \
ASTINUSLAB_VOL3_DWARF2JSON_SHA256=<64-hex> \
  python -m pytest tests/python/host_real -m host_real -q
```

Or trigger the workflow manually with `workflow_dispatch`.

## What an unprovisioned runner looks like

By design, it looks **red**, in two places:

1. The workflow's `Preflight - operator-supplied Volatility configuration` step
   fails before any dependency is installed, naming each variable that is unset,
   not an executable file, or inside the checkout.
2. If the preflight is bypassed, the `operator_volatility` fixture in
   `tests/python/host_real/conftest.py` **raises** — it does not
   `pytest.skip` — and every Volatility row errors with the readiness codes the
   probe returned.

That is the intended signal. A silently skipped Volatility row is not evidence
that Volatility works, so these rows must never be converted to a skip, an
`xfail`, or a conditional marker. The fix is to provision the runner.

The rest of the tier (the capture rows) keeps the pre-existing behaviour: it
skips when there is no real libvirt host at all, because that gate is about the
hypervisor, not about the tool configuration.

## What the nightly proves once it is green

- `test_readiness_reports_both_capabilities_available` — the readiness probe
  reports analysis and ISF-build ready, on a supported Volatility version.
- `test_operator_tools_resolve_outside_the_astinuslab_tree` — both executables
  resolve outside the AstinusLab tree, so the licensing boundary holds on the
  machine that actually runs them.
- `test_dwarf2json_pin_is_reverified_after_the_file_is_replaced` — after
  readiness passes, replacing the `dwarf2json` file makes the next verification
  fail with `dwarf2json_hash_mismatch`. Readiness is not execution
  authorization: the pin is re-checked at use, never cached. The row copies the
  operator's binary into a temporary directory and mutates only that copy.
- `test_single_plugin_run_against_a_real_dump_produces_parseable_output` — a
  real capture, a real ISF for that kernel, and one real `linux.pslist.PsList`
  run whose JSON output parses into columns and rows.
