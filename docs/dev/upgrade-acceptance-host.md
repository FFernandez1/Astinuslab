# Upgrade acceptance host provisioning

Operator runbook for the `[self-hosted, disposable]` runner that executes
`.github/workflows/upgrade-acceptance.yml`. That workflow runs
`tests/shell/host/upgrade_matrix.sh`: the install, upgrade and rollback
acceptance matrix, against a live systemd, as root.

## Why this is a separate host

The matrix is the only thing in this repository that installs AstinusLab
system-wide for real. Across fourteen rows it:

- installs a genuine prior version, upgrades to the candidate, and uninstalls;
- forces a failed migration, a failed service start, and a failed deep
  readiness probe, each of which must roll back cleanly;
- performs a split-identity migration, an upgrade over an existing split, and a
  rollback of one;
- installs to a custom storage root;
- uninstalls twice, once preserving data and once deleting it;
- checks that an install rooted under an operator's home is refused.

It creates and removes service identities, writes and removes systemd units,
moves ACLs and MAC policy, and deletes install roots. Anything else on the
machine is at risk.

This is why it must not share the `[self-hosted, libvirt]` runner that
[`host-nightly.yml`](host-nightly-runner.md) uses. That host is provisioned by
hand and long-lived — the operator installs Volatility 3 into a fixed path on
it — and the matrix would rewrite the machine underneath the tier that depends
on it.

## What the host needs

1. **Expendability.** Rebuild it, or restore it from a snapshot, between runs.
   Nothing else may depend on its state. A cloud VM or a libvirt guest you can
   recreate is the right shape; your workstation is not.

2. **The runner labels** `self-hosted` and `disposable`.

3. **Live systemd**, and passwordless `sudo` for the runner's user. The matrix
   calls `sudo` directly and cannot answer a prompt.

4. **The installer's host prerequisites**, the same set `ops/deploy/install.sh`
   expects: `python3`, `acl`, `rsync`, `curl`, and a working `getfacl`/`setfacl`.

5. **The marker file**, placed by hand:

   ```bash
   sudo touch /etc/astinuslab-disposable-host
   ```

   The run refuses without it. The label alone is not the safety mechanism — a
   label can be attached to the wrong machine by mistake, and a marker file is
   a second statement, made on the host itself, that this machine is
   expendable. Do not add it to a golden image; place it per host.

## Running it

From the Actions tab, dispatch **Upgrade acceptance (disposable host)** with:

- `prior_ref` — the genuine prior version to upgrade *from*: a release tag, or
  the previous release commit. It must resolve in the checkout and differ from
  the candidate. The workflow builds both trees with `git archive`, because the
  matrix verifies each tree against its archive and its commit and will refuse
  a copied worktree.
- `acknowledge_destructive` — literally `DESTROY-THIS-HOST`.

Every row runs even after an earlier one fails, so the run reports the whole
picture rather than the first stop. The job fails if any row did, and the
report and per-row logs upload either way as the `upgrade-acceptance-<sha>`
artifact.

## What a pass does and does not establish

A green run is real-host evidence for install, upgrade, rollback and uninstall
on *that* host's distribution and systemd version. It is not evidence for
another distribution, for a host with pre-existing AstinusLab state the matrix
did not create, or for anything about acquisition: no VM workload runs here.
Real libvirt and Volatility behaviour stay with
[`host-nightly.yml`](host-nightly-runner.md).
