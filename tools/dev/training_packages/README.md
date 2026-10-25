# Playground training package drafts

Three separately importable, unsigned **unlocked drafts** containing topology and
instructions. They contain no guest images, passwords, private keys or claimed
live results. They are not boot-ready training environments.

| Directory | Package lab machines | Exercise |
| --- | --- | --- |
| `01-single-vulnerability` | 1 Target | DVWA command injection |
| `02-pivot` | 3 Targets | Dual-interface pivot, internal web and database |
| `03-firewall` | 1 Network Node + 1 Target | OpenWrt filtering a protected web service |

The Attacker is an external exercise client, excluded from package VM inventories
and every lab machine count. Attach that client only to the entry segment for
network acceptance checks. Host-side reachability is not proof of isolation.

## Build and inspect

Run from the repository root with the project's locked Python environment:

```bash
PYTHONPATH=src .venv/bin/python tools/dev/training_packages/build.py \
  --output-dir build/training-packages
```

The builder validates the canonical `LabDefinition`, converts it to a Playground
recipe, validates guest checkpoint bindings, writes through the application's
archive writer, and reads each archive back through its passive inspector.
It refuses existing output files. Use a new output directory for another build.

The prepared-image IDs in each definition are deliberate catalog requirements,
not aliases for a bare Ubuntu/OpenWrt image. A missing prepared image must block
launch. The drafts omit `workload_id` so importing them cannot request an online
workload installation on an isolated network.

Lab 1's private prepared-image builder and measured guest contract are documented
in [`guest/training/lab1/README.md`](../../../guest/training/lab1/README.md).
The local image is not embedded in the draft archive, and its existence does not
turn the draft into a locked or creator-attested package.

`lab.json` is canonical package input. It is not a Playground recipe JSON.
Use the resulting `.astinuslab` archive in **Packages → Import / inspect**.
Import does not execute the runbook, configure guests or prove exploitability.
Enable the existing package feature in a task-owned application environment for
the integration run; do not restart another task's application or runner.

## Verification

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 ASTINUSLAB_FAKE_PLAYGROUND=1 \
ASTINUSLAB_FAKE_ORCHESTRATION=1 PYTHONPATH=src \
  .venv/bin/python -m pytest tests/python/tooling/test_training_packages.py -q
```

Use a temporary `ASTINUSLAB_DB_PATH` and `ASTINUSLAB_STORAGE_DIR` when invoking
tests from a configured operator checkout.

Readiness checkpoints are baseline guest observations. Exercise completion
requires the independent entry-client traffic tests in each runbook. Never
prewrite success observations, bake completed exercise markers into clean layers,
or describe archive tests as successful live tests.
