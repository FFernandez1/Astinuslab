# Release acceptance evidence

This page is internal operator procedure. Private evidence contains absolute host paths, record identifiers, manifests, logs, traces, and screenshots. Never publish private bundle. Publish only generated `public/` summary after review.

## Preconditions

Run acceptance from exact frozen candidate on approved Ubuntu 24.04 LTS x86_64 KVM host. Before workflows:

1. Record full `git rev-parse HEAD`.
2. Require no tracked changes. Collector rejects dirty tracked checkout; untracked files do not affect source identity.
3. Use isolated storage and evidence roots. They must not overlap. An existing evidence root must already be a non-symlink directory with mode `0700`; collector never repairs permissive permissions.
4. Set explicit storage root used by services and tests:

   ```bash
   export ASTINUSLAB_STORAGE_DIR=/absolute/acceptance/storage
   export ASTINUSLAB_EVIDENCE_DIR=/absolute/acceptance/evidence
   ```

   `ASTINUSLAB_STORAGE_DIR` is mandatory. If `ASTINUSLAB_EVIDENCE_DIR` is omitted, collector writes below `$XDG_STATE_HOME/astinuslab/release-evidence` or `$HOME/.local/state/astinuslab/release-evidence`, outside checkout. In-repository output is accepted only when Git ignores it.
5. Keep rows input, command log, UI traces, screenshots, and other proof files outside storage root. Every input path must be absolute, regular, and non-symlink.
6. Register the acceptance storage root with root-owned policy before the first capture:

   ```bash
   sudo ops/deploy/configure-privileged-roots.sh --install-dir <install-dir> --storage-root /absolute/acceptance/storage
   ```

   Privileged ACL helpers accept only registered roots, so an unregistered acceptance root fails
   every capture row. Run `--check` afterwards and record its output; it reports policy and unit
   drift without mutating the host.

   The services read their storage settings from `<install-dir>/config/astinuslab.env`, not from
   the shell. Playground disk and capture directories are settings separate from
   `ASTINUSLAB_STORAGE_DIR`; the installed configuration keeps them below the install root's own
   storage, so the ACL helper refuses every Playground launch once only the acceptance root is
   registered. Before registering, create the Playground directories and set all three settings:

   ```bash
   sudo install -d -o <service-user> -g astinuslab -m 2700 \
     /absolute/acceptance/storage/playground /absolute/acceptance/storage/playground/disks
   printf '%s\n' \
     ASTINUSLAB_STORAGE_DIR=/absolute/acceptance/storage \
     ASTINUSLAB_PLAYGROUND_DISK_DIR=/absolute/acceptance/storage/playground/disks \
     ASTINUSLAB_PLAYGROUND_CAPTURE_DIR=/absolute/acceptance/storage/playground/captures \
     | sudo tee -a <install-dir>/config/astinuslab.env >/dev/null
   ```

   Then register with `--restart` so the services pick up the new settings. Authenticated deep
   readiness (`GET /api/v1/readiness`) must not report `acl_policy_playground_dir_mismatch` for
   `root_policy`; that code means a Playground directory is outside every registered root.
7. Record the auth mode of the candidate under test. An installed candidate enables admin auth and
   prints its token once — API rows authenticate with `Authorization: Bearer <token>`. UI rows are
   driven by a real browser, which carries the session cookie and the CSRF token; a scripted HTTP
   client must send a bearer token, or seed the `astinuslab_csrf` cookie and `X-CSRF-Token` header
   itself, or its mutations are refused with `403`.

## Required seven rows

All rows are mandatory for 1.0.0. No waiver is planned.

| Row | Workflow | Driver | Required proof |
| --- | --- | --- | --- |
| 1 | Job, `immediate`, memory | API or UI | Completed record, artifact triad, digest, workflow evidence |
| 2 | Job, `post_script` or interactive disk/`both` | API or UI | Completed record, artifact triad, digest, workflow evidence |
| 3 | Job, `post_script`, `workload-sample-post-script` | API or UI | Completed record, artifact triad, digest, state-transition log |
| 4 | Interactive Job capture | **UI only** | Completed record, artifact triad, Playwright trace, screenshot |
| 5 | Playground DVWA workload install | API or UI | Reachability or equivalent proof; no artifact triad |
| 6 | Playground memory capture | **UI only** | Completed acquisition, artifact triad, Playwright trace, screenshot |
| 7 | Playground disk capture and VM resume | **UI only** | Completed acquisition, artifact triad, resume proof, Playwright trace, screenshot |

Backend `host_real` tests exercise backend paths but cannot fill UI rows 4, 6, or 7.

## Install, upgrade and rollback acceptance

The seven workflow rows above accept the *product*. They run against one installation and never
see what an upgrade does to it, so a candidate is also accepted against
`tests/shell/host/upgrade_matrix.sh`, which installs, upgrades, separately fails a migration, a real
systemd start, deep readiness, and an existing-split readiness check, then uninstalls twice:

For the first public release, there is no published version to use for historical
upgrade row 0. On the designated disposable host, select `RELEASE_MODE=first`
explicitly and supply only the frozen candidate. The harness records
`row0=not_applicable:first_release`, without counting it as a pass, and runs every
row from 1 through 13. Candidate-local upgrade and rollback fixtures remain
mandatory; this result supplies no historical upgrade evidence and does not waive
any of the seven product workflows above. First-release mode rejects nonempty
`PRIOR_SRC`, `PRIOR_ARCHIVE` or `PRIOR_SHA` inputs.

```bash
candidate_sha="$(git rev-parse HEAD)"
work="$(mktemp -d /tmp/astinuslab-first-release.XXXXXXXX)"
git archive --format=tar --output="$work/candidate.tar" "$candidate_sha"
mkdir "$work/source"
tar -xf "$work/candidate.tar" -C "$work/source"
sudo -v
env -u PRIOR_SRC -u PRIOR_ARCHIVE -u PRIOR_SHA \
  RELEASE_MODE=first SRC="$work/source" \
  CANDIDATE_ARCHIVE="$work/candidate.tar" CANDIDATE_SHA="$candidate_sha" \
  INSTALL_DIR=/opt/astinuslab SERVICE_USER="$USER" \
  CUSTOM_ROOT=/srv/astinuslab-acceptance/storage \
  "$work/source/tests/shell/host/upgrade_matrix.sh"
```

For subsequent releases, the default `RELEASE_MODE=upgrade` requires a genuine
published prior archive and runs all fourteen rows. The manual GitHub workflow
continues to select this upgrade path; first-release execution uses the local
command above. Never substitute an unpublished commit to claim row 0 acceptance.

```bash
candidate_sha="$(git rev-parse HEAD)"
prior_sha="<full-published-prior-commit>"
git archive --format=tar.gz --prefix=astinuslab-candidate/ \
  -o /tmp/astinuslab-candidate.tar.gz "$candidate_sha"
git archive --format=tar.gz --prefix=astinuslab-prior/ \
  -o /tmp/astinuslab-prior.tar.gz "$prior_sha"
mkdir /tmp/astinuslab-candidate /tmp/astinuslab-prior
tar -xf /tmp/astinuslab-candidate.tar.gz -C /tmp/astinuslab-candidate --strip-components=1
tar -xf /tmp/astinuslab-prior.tar.gz -C /tmp/astinuslab-prior --strip-components=1

sudo -v
env \
  SRC=/tmp/astinuslab-candidate \
  PRIOR_SRC=/tmp/astinuslab-prior \
  CANDIDATE_ARCHIVE=/tmp/astinuslab-candidate.tar.gz \
  PRIOR_ARCHIVE=/tmp/astinuslab-prior.tar.gz \
  CANDIDATE_SHA="$candidate_sha" \
  PRIOR_SHA="$prior_sha" \
  INSTALL_DIR=/opt/astinuslab \
  SERVICE_USER="$USER" \
  CUSTOM_ROOT=/srv/astinuslab-acceptance/storage \
  /tmp/astinuslab-candidate/tests/shell/host/upgrade_matrix.sh
```

It installs and uninstalls for real against live systemd, so it runs on a disposable host and
nowhere else. Every row runs even after an earlier one fails, and the run ends `PASS=<n> FAIL=<n>`;
acceptance requires `FAIL=0` and the applicable release mode recorded in the log.
Every supplied archive must be produced by `git archive`: the matrix reads the
embedded commit IDs, requires them to equal `CANDIDATE_SHA` and `PRIOR_SHA`, independently extracts
them, and compares those trees with `SRC` and `PRIOR_SRC`. It prints both archive and normalized tree
SHA-256 digests into the matrix log and rejects a claimed SHA that is not bound to the supplied
bytes. Keep the archives and complete matrix log in the private evidence store.

| Row | Scenario | What it records |
| --- | --- | --- |
| 0 | Genuine prior-version upgrade | Required in upgrade mode: published prior archive installs first; candidate upgrade and uninstall preserve unowned legacy ACL/MAC policy while removing the isolated legacy install. Explicitly not applicable in first-release mode |
| 1 | Clean install | Unit state, `/health`, database revision against the candidate's own head, install-root and release ownership, storage mode `2770`, helper policy, sudoers, traversal manifest |
| 2 | Rerun on unchanged source | The staged release is reused rather than restaged, `current` unchanged, no duplicate manifest entries |
| 3 | Update from a changed matrix fixture | `current` moved, every service PID replaced, and each process's working directory is the new release; the exact candidate tree remains byte-for-byte immutable |
| 4 | Failed migration | Non-zero exit, `current` back on the prior release, the dropped table restored, database owner and group preserved, and the restored release serving |
| 5 | Failed service start | A real systemd `ExecStart=/bin/false` failure, non-zero installer exit, prior release restored and serving |
| 6 | Failed deep readiness | Real services start, only the candidate's first deep probe is forced to fail, then the prior release passes the original readiness probe |
| 7 | Custom storage root | The installed helper registers a disjoint root below a non-world-traversable parent, renders matching units, applies reversible qemu/MAC host access, and restarts into that root |
| 8 | Split-identity migration | Valid identity marker, three explicit units and process owners, health, runner-only helper access |
| 9 | Existing split upgrade | Existing-split path selected, release and every PID changed, all three process UIDs exact, mutable-root ownership/modes/ACLs unchanged, marker and custom root preserved, services healthy |
| 10 | Existing split rollback | Candidate unit bytes change, split readiness fails once, then release, privileged units/policy/sudoers, identities and health match the pre-upgrade state |
| 11 | Uninstall, preserving the install root | All single/split units inactive with no PID, unit files, sudoers and helpers gone, install root retained, every recorded traversal and MAC change absent, manifests consumed, operator-owned ACLs untouched |
| 12 | Uninstall, deleting the install root | All six unit variants inactive with no PID, install root gone, no standing traversal grant on `/opt`, helper directory fully removed; the harness then removes only its marked disposable custom-storage tree |
| 13 | Reject an install below an operator-writable parent | Non-zero installer exit before install-root or traversal-ACL mutation; the rejected root remains absent and the operator home's custody and ACLs are unchanged |

Row 13 fixes the parent-substitution boundary: the install directory and every existing ancestor
must be root-owned and non-writable by group or other. An ordinary `/home/<user>` therefore cannot
host `INSTALL_DIR`; use a trusted system path such as `/opt/astinuslab`. Custom mutable storage has
its own separately validated policy. Row 11 audits every
recorded AppArmor or SELinux change instead of inferring cleanup from a missing manifest.

## Rows input

Pass JSON with schema `astinuslab-release-evidence-rows/v1` and exactly rows 1 through 7. Each row contains only:

- `row`: integer 1–7;
- `driver`: `api` or `ui`;
- `record_type` and `record_id`: unique private identity for each row. Rows 1–4 use `job`, row 5 uses `workload`, and rows 6–7 use `acquisition`; corresponding manifest `job_id` or `acquisition_id` must equal `record_id` for artifact rows;
- `status`: exactly `completed`;
- `artifact_path`, `manifest_path`, `sidecar_path`: absolute storage paths, except all three are `null` for row 5;
- `ui_proof`: `null` for API rows, or `trace_path` plus nonempty `screenshot_paths` for UI rows;
- `evidence_paths`: absolute proof/log paths. Every row must carry proof: UI rows may rely on their `ui_proof` trace and screenshots, so rows without UI proof — and row 5 always — require at least one evidence file. Row 3 must include the `running_scenario_script` to `waiting_for_capture` transition log;
- `command_log_ref`: optional nonempty label present in copied operator command log;
- `waiver`: `null`, or exact six-field object described below.

Rows 1 and 6 require a manifest kind of `memory`; rows 2 and 7 require `disk`. Rows 3 and 4 accept their workflow-produced artifact kind. Distinct rows must not reuse an artifact inode or record identifier.

Example shape:

```json
{
  "schema_version": "astinuslab-release-evidence-rows/v1",
  "rows": [
    {
      "row": 1,
      "driver": "api",
      "record_type": "job",
      "record_id": "REAL-RECORD-ID",
      "status": "completed",
      "artifact_path": "/absolute/acceptance/storage/captures/memory.dump",
      "manifest_path": "/absolute/acceptance/storage/captures/memory.dump.manifest.json",
      "sidecar_path": "/absolute/acceptance/storage/captures/memory.dump.sha256",
      "ui_proof": null,
      "evidence_paths": ["/absolute/acceptance/proofs/row-1.log"],
      "command_log_ref": "row-1-command",
      "waiver": null
    }
  ]
}
```

Example is shape only; complete file must contain all seven real rows. Do not copy placeholder identifiers into candidate evidence.

Waiver object has exactly `row`, `reason`, `impact`, `owner`, `decision_owner`, and `expires_or_retest`. Every string must be nonempty, row must match, and expiry/retest value must be unexpired ISO-8601 date or timezone-qualified datetime. The collector rejects an unparseable expiry at input time; the validator additionally rejects an expired one at validation time. Complete waiver caps release verdict at GO-WITH-RISKS; malformed or expired waiver forces NO-GO.

## Collect

After workflows finish:

```bash
EVIDENCE_BUNDLE="$({
  ./tools/release/collect_release_evidence.sh \
    --rows-file /absolute/acceptance/rows.json \
    --command-log /absolute/acceptance/operator-command.log
} | tail -n 1)"
printf '%s\n' "$EVIDENCE_BUNDLE"
```

Collector:

- binds bundle name and canonical `SOURCE_COMMIT.txt` to full 40-character clean source commit, then rechecks source before and immediately after atomic publication;
- discovers every `*.manifest.json` recursively under storage, including unreferenced manifests;
- independently hashes raw artifact bytes and compares the complete producer-schema manifest, its top-level digest/size, its integrity provenance block, and the strict sidecar;
- rejects missing/mutating/symlinked/out-of-root paths, symlink directories anywhere in storage, fake-mode evidence, inconsistent record identity/filename/path/size/digest, reused row records or artifacts, and ISF/raw-memory overlap;
- copies exact manifests, sidecars, proofs, and optional command log into mode-private bundle;
- records a sha256 digest and size for every copied proof, trace, screenshot, and command log in `evidence.json`, so proof bytes cannot be swapped after collection;
- never copies raw memory or disk artifact bytes;
- renders a private checklist containing record, artifact, manifest, sidecar, digest, size, UI/backend proof, command-log reference, and waiver columns;
- rechecks source artifacts, manifests, sidecars, and storage inventory immediately before publication;
- refuses output collision and cleans only temporary/final trees whose created identity it still owns.

## Validate independently

Run the standalone validator before the final release review:

```bash
./tools/release/validate_release_evidence.py \
  --candidate-sha "$(git rev-parse HEAD)" \
  "$EVIDENCE_BUNDLE"
```

Omitting `--candidate-sha` in CLI uses current repository `HEAD`. Validator independently rereads private JSON, source artifacts, manifests, sidecars, proofs, command log, checksums, rendered checklists, and sanitized public projection, then rechecks source and bundle snapshots before PASS. It rejects malformed/non-finite JSON, missing/duplicate/out-of-range rows, non-completed rows, wrong record or artifact kinds, invalid or expired waivers, missing UI proof, path escape or symlink, source/bundle/candidate SHA mismatch, fake flags, producer-manifest disagreement, checksum mismatch, proof bytes that disagree with their recorded digest, an incomplete or unreferenced proof inventory, ISF/raw-memory overlap, unexpected or special files/directories, public leakage surfaces, and edited rendered views.

`evidence.json` is private authority. `HOST_WORKFLOW_CHECKLIST.md` is generated view, never independent evidence and never hand-edited into PASS.

## Output handling

Bundle name is UTC timestamp with microseconds plus full source SHA. Private root contains:

- `SOURCE_COMMIT.txt`, `evidence.json`, `SHA256SUMS.txt`, generated checklist;
- exact copied manifests and sidecars;
- referenced proofs and optional `operator-command.log`;
- `public/` sanitized summary.

Retain private bundle under release-owner control. It contains no raw artifact bytes but still contains sensitive absolute paths, IDs, logs, traces, screenshots, and full manifests. Public projection replaces private storage-relative artifact names with opaque row aliases such as `row-1/artifact`; its checksums cover only artifact-bearing workflow rows, never unreferenced private inventory. Only `public/evidence-summary.json`, `public/HOST_WORKFLOW_CHECKLIST.md`, and `public/SHA256SUMS.txt` are publication candidates. Review even sanitized output before publication. Tagging, pushing, or publication still requires separate Task 12 approval and every release gate.
