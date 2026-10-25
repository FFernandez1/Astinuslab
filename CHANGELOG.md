# Changelog

See [Security reporting and support](docs/community/SECURITY.md) for reporting
routes and the best-effort maintenance policy.

All notable changes to AstinusLab are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and AstinusLab uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

AstinusLab-owned source is licensed under the Volatility Software License 1.0. See
`LICENSE`, and see `THIRD_PARTY_NOTICES.md` for components that are not shipped
with AstinusLab but are supplied and installed by the operator.

## [Unreleased] — 1.0.0 release candidate

Not yet released. No tag exists.

### Changed

- **Portable local lab packages remain experimental and unaccepted.** The
  workflow is excluded from supported 1.0.0 capabilities and stays disabled by
  default (`packages.enabled = false`). P1–P3 have no complete accepted receipt:
  end-to-end, two-host, offline and performance guarantees are withdrawn from
  first-release scope. The retained opt-in code provides inert Import/Inspect, durable Prepare,
  host-aware Preflight, acknowledgement-gated Launch, operation status, Reset,
  Teardown, deletion, and garbage collection. Creator inputs include typed
  objectives, hints, checkpoints, and an explicit Verify Clean Run operation.
  Locked exports remain tied to attested immutable layers; unlocked exports are
  labelled non-reproducible. These format labels do not establish release
  acceptance. Future native acceptance targets Ubuntu 24.04 LTS on x86_64 with
  KVM; no portable-workflow host support is currently claimed.
  Runtime labels, confirmations, status panels, and verification-ID copy
  affordances are localized across the supported languages. Package Markdown is
  rendered as a passive, URL-filtered subset; image syntax never creates a
  browser image or link.

- **Fresh helper installation respects private umasks.** Newly created helper
  directories are made traversable so runtime identities can reach privileged
  helpers. Existing parent permissions are preserved. Regression and tooling
  checks pass; a new native installation remains required before acceptance.

- **Legacy-code audit completed without changing public contracts.** The audit
  removed all 35 top-level `sys.modules` compatibility modules and their five
  typing twins, reducing flat `src/astinuslab` Python modules from 45 to 10.
  Internal imports and every production worker launch point now use the owning
  packages, including `-A astinuslab.jobs.celery_main`. More than 500
  static-analysis candidates were reviewed across application code, web assets,
  tests, operations, guest tooling, Volatility integration, and image metadata;
  19 items were deleted. Roughly 97% were retained because decorated FastAPI
  routes, registered Celery tasks, and ORM attributes are runtime entry points.
  The work also restored a CI error-field audit that had scanned zero files,
  exposed all tests to Ruff, corrected the development reload directory, and
  repaired localized worker-launch instructions that omitted 11 Playground tasks.

  **Compatibility:** internal Python import paths changed. HTTP routes, all 23
  Celery task names, Alembic revision IDs, systemd service names, and
  configuration keys are unchanged.

  **Residual host-cycle verification:** the required install → upgrade →
  uninstall cycle was **not exercised in the audit environment**. It runs as
  EUID 1000 and prevents access to root-owned systemd and libvirt host state.
  On a disposable Ubuntu 24.04 host with working systemd/libvirt, run these
  commands from the candidate checkout:

  ```bash
  set -euo pipefail
  cycle_dir="$(mktemp -d /tmp/astinuslab-legacy-cycle.XXXXXXXX)"
  prior_sha=86dd06287e4afcb26838994e7e15bc798e39621d
  candidate_sha="$(git rev-parse HEAD)"
  git archive --format=tar --prefix=prior/ -o "$cycle_dir/prior.tar" "$prior_sha"
  git archive --format=tar --prefix=candidate/ -o "$cycle_dir/candidate.tar" "$candidate_sha"
  tar -xf "$cycle_dir/prior.tar" -C "$cycle_dir"
  tar -xf "$cycle_dir/candidate.tar" -C "$cycle_dir"
  sudo -v
  sudo virsh -c qemu:///system list --all
  sudo test ! -e /opt/astinuslab-legacy-audit
  sudo bash "$cycle_dir/prior/ops/deploy/install.sh" --install-dir /opt/astinuslab-legacy-audit --service-user "$USER" -y --generate-admin-token
  sudo systemctl cat "astinuslab-runner@$USER.service" "astinuslab-analysis@$USER.service" | grep -F -- '-A astinuslab.celery_main'
  sudo bash "$cycle_dir/candidate/ops/deploy/install.sh" --install-dir /opt/astinuslab-legacy-audit --service-user "$USER" -y
  sudo systemctl cat "astinuslab-runner@$USER.service" "astinuslab-analysis@$USER.service" | grep -F -- '-A astinuslab.jobs.celery_main'
  if sudo systemctl cat "astinuslab-runner@$USER.service" "astinuslab-analysis@$USER.service" | grep -F -- '-A astinuslab.celery_main'; then
    echo 'stale Celery module target remains after upgrade' >&2
    exit 1
  fi
  systemctl is-active "astinuslab-api@$USER.service" "astinuslab-runner@$USER.service" "astinuslab-analysis@$USER.service"
  printf 'y\ny\ny\ny\n' | sudo env INSTALL_DIR=/opt/astinuslab-legacy-audit SERVICE_USER="$USER" bash "$cycle_dir/candidate/ops/deploy/uninstall.sh"
  for unit in "astinuslab-api@$USER.service" "astinuslab-runner@$USER.service" "astinuslab-analysis@$USER.service"; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
      echo "unit still present after uninstall: $unit" >&2
      exit 1
    fi
  done
  sudo test ! -e /opt/astinuslab-legacy-audit
  ```

  Until that cycle passes, an interrupted or partial upgrade could retain stale
  rendered units. A current worker starts with
  `-A astinuslab.jobs.celery_main`; a pre-audit unit names
  `astinuslab.celery_main`, which no longer exists, so the service fails during
  import.

- **Installer preflight is now an authoritative readiness gate.** `deploy/install.sh
  --check` stays read-only, but required command, header, KVM, libvirt, trusted-root,
  policy, configuration, and unit-drift failures now return nonzero and end in
  `Preflight result: NOT READY`. Successful checks end in one `READY` or
  `READY WITH WARNINGS` verdict.

  **Compatibility:** automation that used the previous warning-only `--check`
  inventory must now treat its exit status as authoritative. Full installation now
  previews the resolved plan, reports nine stable phases, and ends with separated
  credential, required image/readiness, and optional Volatility actions.

- **Volatility onboarding is available through a read-only launcher command.**
  `./astinuslab.sh guide-volatility` reports independent analysis and ISF-build
  readiness and prints copy-paste commands for the recommended Volatility 3 version
  plus the audited dwarf2json URL, commit, and SHA-256. It never downloads, installs,
  upgrades, or accepts licenses for either operator-supplied tool.

- **API contract (breaking for pre-release consumers): Volatility enqueue endpoints
  now return HTTP `503` with code `dependency_unavailable` when the operator-supplied
  external tool is unavailable, replacing the previous HTTP `409` / `conflict`.**

  Affected endpoints:

  - `POST /api/v1/volatility/runs` — single-plugin run
  - `POST /jobs/{job_id}/vol3-analysis` — batch analysis from the jobs UI
    (browser form post; never returns `503` — the handler converts the readiness
    failure into a redirect carrying an error message, so no `code` or
    `context.dependency_code` reaches the client)
  - `POST /volatility/build` — Linux ISF build from the Volatility workbench
    (returns `503` for JSON clients; browser form posts redirect to an error banner).
    Since the ISF gate was narrowed, this endpoint refuses only when the integration
    is switched off (`feature_disabled`). An unready `dwarf2json` no longer blocks it:
    the local-cache and prebuilt providers deliver a ready ISF without that tool, so
    the queue accepts the request and the paths that do execute `dwarf2json` —
    debuginfod, host-fetch, and the live-guest transfer — fail later carrying the
    stable dependency code.

  Response envelope:

  ```json
  {
    "detail": "<safe, operator-actionable message>",
    "code": "dependency_unavailable",
    "context": { "dependency_code": "<stable readiness code>" }
  }
  ```

  `context.dependency_code` carries one of the stable readiness codes:
  `feature_disabled`, `volatility_not_configured`, `volatility_not_found`,
  `volatility_not_executable`, `volatility_probe_failed`,
  `volatility_version_unsupported`, `dwarf2json_not_configured`,
  `dwarf2json_hash_not_configured`, `dwarf2json_not_found`,
  `dwarf2json_not_executable`, or `dwarf2json_hash_mismatch`. The same codes are
  served by `GET /api/v1/volatility/readiness`, so a client can distinguish
  "not installed" from "installed but wrong version" from "hash mismatch"
  without parsing prose.

  **Why:** `409` / `conflict` states that the request conflicts with current
  resource state, which a client may reasonably retry or resolve. A missing or
  unverified operator-supplied tool is an unavailable dependency: the request
  cannot succeed until the operator installs and configures the tool, and no
  retry will change that. `503` also keeps a genuine state conflict
  distinguishable from a missing dependency.

  **Migration:** a client that treated `409` as "Volatility unavailable" must
  handle `503` and read `context.dependency_code`. `409` / `conflict` is
  unchanged for real state conflicts on these same endpoints — an analysis or
  plugin run already queued or running, a job that is not complete, or a memory
  dump that is not complete.

- **Status precedence on `POST /api/v1/volatility/runs`:** request validation now
  runs before the readiness probe, so a malformed plugin name or a YARA argument
  error returns `400` even when Volatility is unavailable. Previously the readiness
  probe ran first and such a request received `503`. A client that inferred "tool
  unavailable" from the status alone should read `code` instead. The change also
  stops a rejected request from paying for a full Volatility framework load.

  No documentation, frontend code, or external consumer in this repository
  depended on the old status. This is recorded here rather than treated as an
  internal refactor because it changes a published API surface at the `1.0.0`
  boundary.

### Security

Privileged-boundary remediation. Every item below is an intentional behaviour change; the
compatibility notes say what an existing install has to do.

- **Browser mutations require a same-origin `Origin` and a CSRF token.** `POST`, `PUT`, `PATCH`,
  and `DELETE` are refused with `403` / `cross_origin_forbidden` for a cross-site request, and with
  `403` / `csrf_required` or `csrf_invalid` when the `astinuslab_csrf` cookie is not echoed in the
  `X-CSRF-Token` header or a `csrf_token` form field. The guards run whether or not auth is
  enabled, because loopback reachability is ambient browser authority on its own.

  **Compatibility:** requests carrying no `Origin` header — `curl`, SDKs, CI — are unaffected, and
  a valid `Authorization: Bearer` token exempts a request from CSRF. A cookie-authenticated browser
  client written against a pre-release build must echo the token; the shipped UI does this already.
  Serving the UI through a reverse proxy or a non-loopback hostname requires listing that origin in
  `[auth] browser_allowed_origins` (`ASTINUSLAB_BROWSER_ALLOWED_ORIGINS`), which replaces the
  loopback defaults rather than extending them.

- **Installed releases enable admin authentication by default.** A clean `deploy/install.sh`
  generates one high-entropy token, stores only its `sha256:<64-hex>` hash in `astinuslab.toml`,
  shows the raw token when created and once more in the final save-now block. Re-running the installer preserves an existing valid hash and
  refuses to rotate it silently; `--admin-token-hash`, `--generate-admin-token`, and
  `--allow-unauthenticated-local` are mutually exclusive.

  **Compatibility:** an unauthenticated installed deployment now needs an explicit
  `--allow-unauthenticated-local`. Development checkouts are unchanged and stay unauthenticated on
  loopback.

- **Privileged ACL helpers no longer accept a caller-supplied trust root.** Roots come from
  root-owned `/etc/astinuslab/acl-policy.json` (schema `astinuslab-acl-policy/v1`). The helper
  walks by directory descriptor, checks owner and write bits per component, and fails when the ACL
  mask clamps a granted entry instead of reporting success. `/`, the system prefixes, and `/home`
  and `/var` themselves are not registrable roots; dedicated subdirectories below `/home` and
  `/var` are.

  **Compatibility:** a storage root outside the install tree must be registered once with
  `sudo deploy/configure-privileged-roots.sh --install-dir <dir> --storage-root <root>`; captures
  under an unregistered root fail closed. Upgrades keep a previously registered root — the
  installer reads it back from the policy — and `--check` reports drift without touching the host.
  Capture directories are now created at mode `0750`, and a group-writable ancestor is repaired
  rather than walked.

- **Runtime services can no longer write application source or `.venv`.** The installer reconciles
  ownership on upgrades as well as clean installs, and the systemd units add `ReadOnlyPaths` over
  the install directory, `UMask=0027`, and a `ReadWritePaths` set limited to storage, `config`, and
  `images/definitions` — plus `vol3/symbols`, `/var/lib/libvirt/images`, and `/tmp` for the workers. A storage root under `/home` renders the API unit with
  `ProtectHome=tmpfs` plus an explicit exception.

  **Compatibility:** anything that wrote into the install tree at runtime — an out-of-tree plugin
  drop, a hand-edited template on a live host — must move under a runtime-writable path.

- **Image downloads are pinned to the validated peer.** AstinusLab resolves the host itself,
  rejects non-global answers under the default safe network policy (loopback, RFC1918, link-local,
  unique-local, and `169.254.169.254`), connects to that address, and verifies `getpeername()`
  before sending the request. Redirects are followed manually — each hop revalidated, at most five,
  loops refused — so a redirect or DNS rebind cannot reach a blocked network. Each download stages
  into a private per-attempt directory and is published by linking the finished file into the image
  directory under an unclaimed name.

  **Compatibility:** a mirror that redirects to a private address, or a URL carrying credentials,
  now fails closed. Downloads from a host-local mirror require a non-default network policy.

- **Uninstall removes the ACL policy file and the full helper set**, and fails loudly when an
  unknown file remains in `/usr/local/libexec/astinuslab` rather than leaving root-owned residue.

  **Compatibility:** to move a live install to a different storage root, run
  `deploy/configure-privileged-roots.sh` against it — do not uninstall and reinstall, which
  discards the registered policy.

**Rollback.** Reinstalling older code does not reverse database migrations. A deliberate
downgrade requires a compatible schema or a verified pre-migration backup restored with all
writers stopped; reconcile evidence and records created after that backup. Follow
[Schema migration and rollback](docs/reference/ADVANCED_CONFIG.md#schema-migration-and-rollback).
The installer's automatic recovery from a failed upgrade restores its own database backup and
saved installation tree; it is a separate operation. Do not uninstall first or remove authentication
configuration as a rollback shortcut. Preserve configuration, catalogs, evidence and credentials.
