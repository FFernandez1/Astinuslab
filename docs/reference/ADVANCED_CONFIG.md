# Advanced Configuration

This is the detailed reference for backend settings. For the operator-facing configuration guide, see [Configuration](../usage/configuration.md).

AstinusLab reads `astinuslab.toml` from the source checkout or install
directory. `src/astinuslab/core/config.py` then applies `ASTINUSLAB_<SETTINGS_FIELD>`
environment overrides for fields on the flattened `Settings` model. For
example, `ASTINUSLAB_STORAGE_DIR` overrides `storage_dir` and
`ASTINUSLAB_CELERY_BROKER_URL` overrides `celery_broker_url`.

---

## 1. Global Settings (`astinuslab.toml`)

The TOML file is the source of truth for platform infrastructure settings.

### [orchestration]
| Key | Default | Description |
| :--- | :--- | :--- |
| `api_host` | `127.0.0.1` | The address the API server binds to. |
| `api_port` | `8000` | The port for the API and UI. |
| `api_log_level` | `info` | Uvicorn/application log level used when configuring backend logging. |
| `log_to_file` | `false` | Also write this service's records below `storage/logs/`, which the in-app Logs page reads. Split installations use [separate role directories](#installed-split-service-logs). Installed systemd units enable it because their stdout goes to the journal; the development launcher leaves it off because it already redirects stdout to the flat log files. |
| `log_max_bytes` | `10485760` | Size bound per log file before it rotates. `0` disables rotation. |
| `log_backup_count` | `5` | How many rotated files to keep per service. |
| `log_format` | `text` | Record format: `text` (the human format the Logs page renders) or `json` (one object per record, correlation fields named). An unrecognised value falls back to `text`. |
| `libvirt_uri` | `qemu:///system` | Connection string for libvirt. |
| `libvirt_network` | `default` | The libvirt network used for guest VMs. |

### [auth]
| Key | Default | Description |
| :--- | :--- | :--- |
| `enabled` | `false` | Require a single admin bearer token for every route except the public `/health` check and `/static/*` assets (covers the API, Playground, artifact downloads, OpenAPI docs, and the web UI pages). |
| `admin_api_token_hash` | `null` | SHA-256 hash of the admin API token. Use `sha256:<64-hex>` format. Required when `enabled=true`. |
| `allow_unsafe_network_bind_without_auth` | `false` | Explicit escape hatch for controlled local experiments. When `false`, non-loopback `api_host` values require auth. |
| `browser_allowed_origins` | `[]` | Exact origins accepted on browser mutations. Empty means the loopback origins on `api_port`. Comma-separated or a JSON list in the environment; an entry with a path, credentials, or wildcard fails validation at startup. |

Installed releases enable auth during clean install: `ops/deploy/install.sh` generates one token,
writes only its hash into `astinuslab.toml`, and prints the raw token once. Re-running the
installer preserves an existing valid hash and refuses to rotate it silently. A development
checkout keeps `enabled = false` and loopback binding. See
[Authentication](../api/authentication.md).

Generate a token hash without printing the raw token:

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

### [storage]
| Key | Default | Description |
| :--- | :--- | :--- |
| `storage_dir` | `storage` | `storage_dir` selects the destination for future Jobs evidence; changing it or registering privileged roots does not copy historical bytes or rewrite their database references. `playground_capture_dir`, `playground_disk_dir` and `db_path` are independent settings. Plan and validate a separate migration before retiring an old root. |
| `db_path` | `storage/astinuslab.db` | Path to the SQLite database. |
| `images_dir` | `images/definitions` | Path where base cloud images are stored. |
| `serial_log_dir` | `storage/logs/serial` | Directory for guest serial console logs. |
| `playground_disk_dir` | `storage/playground/disks` | Root for ephemeral Playground VM overlays. Must differ from and not contain `playground_capture_dir`. |
| `playground_capture_dir` | `storage/playground/captures` | Root for Playground capture artifacts. Must differ from and not contain `playground_disk_dir`. |
| `libvirt_image_base_dir` | `/var/lib/libvirt/images/astinuslab` | System libvirt image cache root used by host helpers. |
| `playground_default_vcpus` | `2` | Default vCPU count for Playground VMs when a role or request does not override it. |
| `playground_default_memory_mb` | `2048` | Default memory for Playground VMs when a role or request does not override it. |
| `playground_capture_stream_timeout_seconds` | `3600` | Server-side timeout for Playground capture event streams. |
| `playground_capture_timeout_seconds` | `900` | Host subprocess timeout for Playground disk capture conversion. |

Job requests that provide an explicit `image_path` are accepted only when the
resolved path is inside `images_dir`. The libvirt storage helper applies the
same allowed-source-prefix check before copying a base image into libvirt
storage.

### [celery]
| Key | Default | Description |
| :--- | :--- | :--- |
| `broker_url` | `sqla+sqlite:///{base_dir}/storage/celery_broker.db` | Connection URL for the Celery broker. SQLite is the supported V1 broker. |
| `result_backend` | `db+sqlite:///{base_dir}/storage/celery_results.db` | Where task results are stored. |
| `reconciliation_enabled` | `true` | Enable startup/periodic reconciliation of interrupted jobs and orphaned libvirt resources. |
| `reconciliation_interval_seconds` | `60` | Interval for periodic reconciliation. |
| `reconciliation_grace_seconds` | `120` | Grace period before reconciliation treats resources as stale. |

### [vol3]
| Key | Default | Description |
| :--- | :--- | :--- |
| `enabled` | `true` | Enables Volatility 3 ISF and analysis workflows. |
| `symbols_path` | `vol3/symbols` | Global reusable ISF cache root. Linux symbols are stored under `linux/`. |
| `dwarf2json_path` | unset | Operator-supplied `dwarf2json` executable outside the AstinusLab tree; it requires the matching `dwarf2json_sha256` pin. |
| `dwarf2json_sha256` | unset | Exact SHA-256 pin checked immediately before each local execution and guest transfer. |
| `volatility_bin` | unset | Operator-supplied Volatility 3 Framework 2.28.0 executable outside the AstinusLab tree. |
| `default_plugins` | Linux process/module plugins | Comma-separated plugin list for job-local analysis. |
| `fetch_timeout_seconds` | `1800` | Host-side debug package fetch timeout. Large distro debug packages can exceed 1 GB. |
| `isf_mirror_urls` | `[]` | Prebuilt ISF mirror URLs. Entries must use `https://` or `file://`. |
| `prebuilt_max_compressed_bytes` | `67108864` | Maximum compressed prebuilt ISF download size. |
| `prebuilt_max_decompressed_bytes` | `1073741824` | Maximum decompressed prebuilt ISF size. |
| `dwarf2json_timeout_seconds` | `600` | Host-side `dwarf2json` build timeout before live-guest fallback is attempted. |
| `plugin_run_max_rows` | `50000` | Maximum JSON rows retained in API/workbench responses for an interactive Volatility plugin run; full raw JSON output remains on disk. |
| `debuginfod_urls` | `[]` | Extra debuginfod endpoints. Fedora, Arch, and openSUSE get distro defaults when no explicit endpoint is configured. |
| `debuginfod_min_free_bytes` | `2147483648` | Minimum free space required before debuginfod fetches run. |
| `provider_timeouts` | `prebuilt_isf=60`, `debuginfod=900`, `host_fetch=1800`, `live_guest=1800` | Provider-specific timeouts. |
| `allow_invasive_guest_symbol_install` | `false` | When `true`, live-guest symbol builds may install debug packages inside the guest. Default `false` allows only an existing matching DWARF-bearing `vmlinux` or debuginfod and passes `VOL3_ALLOW_GUEST_PACKAGE_INSTALL=0`; BTF-only input is not supported by `dwarf2json`. Even when `true`, invasive install is permitted only for ISF-only jobs (`capture_type: none`); jobs that capture memory or disk evidence always run with `VOL3_ALLOW_GUEST_PACKAGE_INSTALL=0`, and the suppression is recorded as a job event. |

AstinusLab checks `<volatility_bin> -q frameworkinfo.FrameworkInfo` before plugin execution and records the first output line as `vol3_version` in `analysis-summary.json`. Keep this binary pinned at the deployment level; tested deployments should record the installed Volatility 3 package or commit in their release notes.

### [image_inspection]
Controls automatic distro identification of disk images uploaded or downloaded
into `images_dir`. When enabled, AstinusLab runs `virt-inspector` (from
`libguestfs-tools`) against each new image, parses `/etc/os-release` and friends,
and persists the result as a `<image>.meta.json` sidecar next to the image. The
sidecar is the source of truth for catalog auto-discovery and the image admin UI;
filename heuristics are used only as a fallback when inspection is unavailable.

| Key | Default | Description |
| :--- | :--- | :--- |
| `enabled` | `true` | Set to `false` to skip inspection entirely and always use filename heuristics. |
| `timeout_seconds` | `60` | Maximum wall-clock time for a single `virt-inspector` invocation. |
| `virt_inspector_bin` | `virt-inspector` | Override if the binary lives outside `$PATH`. |

If `virt-inspector` is missing or fails, the sidecar is still written with
`source: "filename"` and an `inspect_error` field for the UI to surface; nothing
blocks. Users can edit fields via the image admin page or trigger
re-inspection — fields recorded in `user_edited_fields` survive subsequent
re-inspections.

---

## 2. Catalog Paths

Catalog paths are configured in the `[catalogs]` section of `astinuslab.toml`.
Use absolute paths or paths relative to the source checkout / install directory.

---

## 3. Launcher and Service Settings

`config/astinuslab.env` is sourced by `astinuslab.sh` and by the systemd API
unit command line after release installation. It is primarily a launcher file
for values such as API bind host and port. Because those values are exported
into the process environment, names that also match `Settings` fields can affect
backend settings too.

### Static asset caching (`AL_ASSET_VERSION`)

Templates cache-bust their own links with `?v=<asset version>`, resolved from
`AL_ASSET_VERSION`, else the short git SHA, else `dev`. The `/static` mount
sends `Cache-Control: no-cache` by default: browsers still cache, but revalidate
before use, so a deploy is picked up on the next page load. This matters because
the ESM `import` specifiers inside the page modules are deliberately unversioned
— their URLs do not change between deploys, and without an explicit policy a
browser would apply heuristic freshness and keep running the old module.

Setting `AL_ASSET_VERSION` to a value the deploy chooses (a release tag, a build
id) additionally makes requests carrying that exact token cacheable for a year
as `immutable`. A git SHA does **not** enable this: it stays put while a working
tree is edited, so a development checkout would otherwise pin stale bytes under
a token it goes on to reuse. Set it only where the assets for a given token
never change again.

## 4. Tuning for Concurrency

Use the launcher or installed systemd units. Keep the lifecycle worker at `--pool=threads --concurrency=1` with Beat enabled; keep the separate `analysis` worker at concurrency 1. Do not edit worker concurrency to increase VM capacity. Use `[orchestration] max_concurrent_vms` for admission control; boot/capture lifecycle operations remain serialized. Multi-worker scaling requires a separate broker/locking/reconciliation design.

## 5. Security Boundary

### Deployment posture

V1 is localhost/trusted-local software by default. Keep `api_host` bound to `127.0.0.1` for normal local use. If you bind to a LAN address, enable `[auth] enabled = true` with `admin_api_token_hash`; AstinusLab refuses non-loopback binds without auth unless `allow_unsafe_network_bind_without_auth = true` is explicitly set. Even with admin API auth, use TLS, VPN/firewall, and reverse-proxy controls for shared networks. Guest workloads are intentionally vulnerable; prefer isolated Playground networks for hostile labs.

The SSH keystore at `storage/ssh_keys/` holds **passphraseless** private keys (required for non-interactive job automation) that grant sudo-capable access to every lab guest. Treat the directory as a credential store, not as data: exclude `storage/ssh_keys/` from any backup, sync, network share, or evidence export of the storage tree, and never place `base_dir/storage` on shared or networked storage without excluding it. Deleting a key from the keystore UI invalidates access for future jobs but does not retroactively protect copies that were already exfiltrated with a backup.

Current Playground recipes are trusted local JSON input for repeatable lab configuration. Treat recipe files as trusted because they can describe workloads, services, and guest-side behavior. YAML recipe support remains a roadmap direction.

### Browser mutation boundary

Loopback reachability is not authorization: any page in the operator's browser can post to
`http://127.0.0.1:8000`, and with auth disabled that request carries no credential. Every `POST`,
`PUT`, `PATCH`, and `DELETE` therefore passes two guards, whether or not auth is enabled.

- **Origin.** `Sec-Fetch-Site: cross-site`, or an `Origin` outside `browser_allowed_origins`, is
  refused with `403` / `cross_origin_forbidden`. A request with no `Origin` header is not a browser
  navigation and is unaffected, so `curl` and SDK clients keep working.
- **CSRF.** The `astinuslab_csrf` cookie must be echoed in the `X-CSRF-Token` header or a
  `csrf_token` form field: `403` / `csrf_required` when absent, `403` / `csrf_invalid` when it does
  not match. Requests carrying a valid admin bearer token are exempt, because a bearer token is not
  ambient browser authority.

Full client-side detail, including the reverse-proxy allowlist, is in
[Authentication](../api/authentication.md#browser-mutation-protection).

### Privileged storage policy

Installed ACL helpers run as root through `sudo -n` and no longer accept a trust root from their
caller. Roots come from `/etc/astinuslab/acl-policy.json`, which must be root-owned, non-symlink,
and not group- or world-writable, and which carries schema `astinuslab-acl-policy/v1` with
`allowed_roots`, `allowed_callers`, and optional `storage_groups`. A helper invocation whose target
falls outside every allowed root, or whose caller is not listed, fails instead of applying an ACL.

Register a root with the installed script, never by hand-editing the policy:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab \
  --storage-root /srv/astinuslab
```

`storage_dir` selects the destination for future Jobs evidence; changing it or registering privileged roots does not copy historical bytes or rewrite their database references. `playground_capture_dir`, `playground_disk_dir` and `db_path` are independent settings. Plan and validate a separate migration before retiring an old root.
The ACL helper refuses Playground disks and captures outside every registered root, so set `playground_disk_dir` and `playground_capture_dir` inside a registered root too. Until both are covered, deep readiness reports `acl_policy_playground_dir_mismatch` for `root_policy`.
Own the root by the current service identity and the stable `astinuslab` group
(mode `2770`). Every parent must be root-owned and not writable by group or
others; use `root:astinuslab` mode `0750` where the group needs traversal. This
keeps the path usable if the installation later migrates to split identities.

A registrable root is absolute, existing, and a non-symlink directory. `/` and the system prefixes
(`/etc`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/boot`, `/root`, `/tmp`, `/dev`, `/proc`, `/sys`,
`/run`) are rejected; `/home` and `/var` are rejected as roots themselves while dedicated
subdirectories below them are accepted. `--check` reports policy and systemd drift without
mutating the host, and no service restarts unless `--restart` is given.

Within an approved root the helper walks the path by directory descriptor
(`O_NOFOLLOW | O_DIRECTORY`), checks each component's owner and write bits through that descriptor,
grants only the traversal and read entries libvirt needs, confirms every entry inside the target
still resolves to the same inode afterwards, and reads the resulting ACL back and fails if the mask
clamped any entry — an entry that is present but not *effective* is treated as a failure, not
a success. Group write on a path component is tolerated only for a group named in `storage_groups`
(the database parent uses mode `2770` so the service group can create SQLite WAL
and SHM siblings). In split mode the outer storage root is sticky (`3770`) to
protect package-store entries; keep shared databases in their separate nonsticky
parent. Do not remove that protection to repair a SQLite permission failure.

`ops/deploy/uninstall.sh` removes the policy file and the whole known helper set, and fails loudly if
an unknown file remains in `/usr/local/libexec/astinuslab` rather than leaving root-owned residue
behind.

Reaching a capture needs `x` on every directory from `/` down to the storage root, and that walk
crosses directories this product does not own — an operator's home among them. The installer
records each entry it actually adds in `/etc/astinuslab/traversal-acl.manifest`; a component that
already carried the entry is left out, because it was never ours to grant. Uninstall removes
exactly what that manifest names and nothing else, so an operator's own grants survive.

### Installed release layout

An installed release is immutable and addressed by content. `ops/deploy/install.sh` stages the source
and builds its virtualenv under `releases/<version>-<digest>/`, then moves the `current` symlink
onto it with a rename, so a running service never resolves through a moment where `current` is
absent. Reinstalling the same source resolves to the release that is already there instead of
staging a second copy.

```text
/opt/astinuslab/
├── releases/1.0.0-<digest>/   immutable source and virtualenv, root-owned
├── current -> releases/1.0.0-<digest>
├── storage/                   evidence, databases, playground scratch
├── config/                    catalogs and the launcher env file
├── images/definitions/        operator-supplied base images
├── vol3/symbols/              Volatility symbol cache
└── astinuslab.toml            operator configuration
```

Mutable paths stay at the install root and survive every switch. That root is also what the
privileged policy binds: `install_sudoers_astinuslab.sh` bakes its device and inode into the
storage helper, and because the root never moves, the helper keeps working after an upgrade and
after a rollback. Binding the resolved release directory instead would change that identity on
every release.

The install root and every existing ancestor must be `root:root` and not writable by group or
other. Use a trusted location such as `/opt/astinuslab`; an ordinary operator-owned path below
`/home` is not a supported `INSTALL_DIR`. Disjoint custom storage roots follow the separate
privileged-root parent-chain policy.

The systemd units run from `current` and are told where mutable state lives with
`Environment=ASTINUSLAB_DATA_DIR=/opt/astinuslab`. The application keeps the two roots apart:
`base_dir` is the code root the release supplies, `data_dir` is the install root, and every
storage, config, image and symbol path follows `data_dir`.

Upgrading an install that predates this layout moves its source into a release and removes the
superseded copies from the root, leaving mutable paths and anything else the operator put there
untouched. Because `systemctl start` is a no-op on an already-active unit, a changed release always
restarts the units rather than starting them.

### Installed split database paths

The split-identity installer moves the three managed SQLite databases into
`<install_dir>/storage/databases/` and records their paths in
`config/astinuslab.env`. It sets `ASTINUSLAB_DB_PATH`,
`ASTINUSLAB_CELERY_BROKER_URL`, and `ASTINUSLAB_CELERY_RESULT_BACKEND` only for
managed endpoints. Source checkouts and single-identity defaults remain flat
under `storage/`. Changing the evidence root does not move these databases.

Use the installed environment evaluator below for backups, diagnostics and
restores. A bare Python command or a hard-coded `storage/astinuslab.db` can select
the wrong database. The **Clear storage state** control refuses a relocated DB
before deleting database or SSH state; use stopped-service operator maintenance.

The installer preserves supported explicit endpoints and refuses unsupported or
ambiguous SQLite layouts before migration. Do not manually move databases or
introduce symlink aliases. It retains original database families and recovery
records privately under `backups/`; an unfinished layout transaction must be
reconciled before a normal installer rerun. Preserve the reported recovery
location and all SQLite sidecars until recovery is complete.

### Installed split service logs

Split installations write service logs to `storage/logs/api/api.log`,
`storage/logs/runner/runner.log` and
`storage/logs/analysis/analysis-worker.log`. Each worker can rotate its own logs;
the API can read and truncate them without replacing worker-owned files. The
Logs page and `./astinuslab.sh logs` resolve this layout automatically. Source
checkouts and single-identity installations keep the flat file layout.

Existing flat logs remain available on the Logs page through the read-only
`legacy-api.log`, `legacy-runner.log` and `legacy-analysis-worker.log` names.
Clearing runtime storage preserves the installed log directories and history.
Do not remove the layout marker or change directory ownership manually; a failed
installation retains new log material for recovery before removing service users.

### Schema migration and rollback

Switching `current` changes code, not the database schema. Returning to an earlier
release requires a compatible schema or restoration of its verified pre-migration
backup while all writers are stopped; an arbitrary old symlink is not a safe rollback.
An upgrade takes that backup first and restores both halves if a later step fails:

1. **Quiesce.** Every unit is stopped, so the schema is never migrated with a writer attached,
   and nothing is running while `current` moves.
2. **Back up.** The application database is copied through SQLite's own backup API after a
   `wal_checkpoint(TRUNCATE)`. A plain file copy can miss committed rows and tear under a
   concurrent writer, because the database runs in WAL mode. The copy is verified with
   `integrity_check`, created mode `0600`, and its path, SHA-256 and stamped revision are recorded
   in the installer output. The installer refuses to migrate if this step does not succeed.
3. **Migrate.** A failed migration aborts before publishing the candidate, including
   with `--no-start`. Otherwise start the services and run deep readiness.
4. **Roll back on failure.** The database is restored from the backup *before* the services are
   restarted, so nothing comes up against a schema its code does not match, and `current` is
   switched back to the release that was live before. A restore that fails names the backup file so
   an operator can put it back by hand. The rollback then verifies the release it put back, the same
   way an upgrade verifies a new one: `systemctl` returns as soon as it has started the units, which
   is before the restored release is serving, so a host that never comes back would otherwise be
   reported as recovered.

The switch itself is one ordered sequence, taken by every install and every upgrade alike: stop
every unit, move `current` with a rename, `daemon-reload`, then **restart** every unit. `restart` is
the only verb used. `systemctl start` is a no-op against an already-active unit, so an upgrade that
used it left the old processes attached to a release the installer had just superseded; `restart`
also starts an inactive unit, so a clean install and an upgrade take the same path and there is no
second branch to get wrong. The `--restart` flag is still accepted and no longer changes anything.

`--no-start` stops the services, moves `current`, and leaves them stopped and enabled. Deep
readiness cannot pass against services that were never started, so it is skipped with a warning that
says the release switch is unverified rather than silently reporting success.

Backups live in `/opt/astinuslab/backups/`, mode `0700` — beside the storage tree, never inside it.
Storage is what evidence bundles are cut from and it holds `storage/ssh_keys/`; copying only the
database is what keeps private key material out of a backup that might travel.

Backup, migration, restore and readiness read `config/astinuslab.env` through the
same non-shell evaluator as installed maintenance, with env values above TOML and
defaults. Ambient `ASTINUSLAB_*` shell exports are cleared. The installer records
`effective_database=...`; verify it names the service database. The runtime env file
is never sourced as root. Do not change configuration during an upgrade/rollback.

The database CLI exposes the same primitives. When invoking it manually, pass the
installed environment explicitly with the evaluator (a bare `python -m` does not
read `config/astinuslab.env`). For example, after quiescing all writers:

```bash
sudo env -i PATH=/usr/bin:/bin /usr/bin/python3 -I /opt/astinuslab/current/ops/deploy/runtime_environment.py --data-dir /opt/astinuslab --settings-only --user "$USER" -- /opt/astinuslab/current/.venv/bin/python -m astinuslab.db backup /opt/astinuslab/backups/manual.db
```

Use the actual installed service user in place of `$USER`. With that effective
environment already supplied, the underlying CLI commands are:

```bash
/opt/astinuslab/current/.venv/bin/python -m astinuslab.db revision
/opt/astinuslab/current/.venv/bin/python -m astinuslab.db backup  /opt/astinuslab/backups/manual.db
/opt/astinuslab/current/.venv/bin/python -m astinuslab.db restore /opt/astinuslab/backups/manual.db
/opt/astinuslab/current/.venv/bin/python -m astinuslab.db downgrade a020_artifact_publication_attempts
```

`downgrade` requires an explicit revision. Relative targets such as `-1`, and the aliases `head`,
`heads` and `base`, are refused: `-1` resolves against whatever head happens to be at the time, so
adding a revision or a merge point later would silently change which transition runs.

Rolling back the schema does **not** un-write evidence. Captures, manifests and sidecars already
published to disk stay where they are; what the rollback restores is the database and the release
symlink.

### Install tree ownership

Application source and `.venv` stay installer-owned and are not writable by the runtime identity.
`ops/deploy/install.sh` reconciles this on upgrades as well as clean installs: it strips ACLs, clears
group/other write and setgid bits, and chowns `src`, `ops`, `tools`, `deploy`, `guest`,
`.venv`, `pyproject.toml`, and the lockfiles to `root:root` **inside the live release**.
Only `storage`, `config`, `images/definitions`, and `vol3/symbols`, which sit at the install
root rather than in the release, are runtime-owned.

The systemd units enforce the same shape at runtime, and each service gets only the mutable paths
its own code writes. `ReadOnlyPaths` covers the install directory and `UMask=0007` keeps new evidence
off world-readable modes while preserving the group write the shared SQLite files need.

| Unit | `ReadWritePaths` | Why |
|---|---|---|
| `astinuslab-api` | storage, `config`, `images/definitions` | stages uploaded and downloaded images, rewrites catalog YAML |
| `astinuslab-runner` | storage, `images/definitions`, `vol3/symbols`, `/var/lib/libvirt/images` | creates VM overlays and builds ISF symbols; the sudo-root storage helper runs inside this unit's mount namespace |
| `astinuslab-analysis` | storage, `vol3/symbols` | consumes only the analysis queue; it never creates a VM, so it reaches no virtualization path |

All three units also set `LockPersonality`, `ProtectKernelTunables`, `ProtectKernelModules`,
`ProtectKernelLogs`, and `ProtectControlGroups`. `RestrictSUIDSGID` is deliberately **not** set:
its filter rejects any `chmod` whose mode carries setgid, and the storage tree is setgid so the
service group is inherited — enabling it turns every memory capture into `Operation not permitted`.
`NoNewPrivileges` stays on the API only, because it makes every `sudo -n` helper call fail; the
split analysis identity gains it because that identity has no sudo at all.

A storage root under `/home` renders every unit with `ProtectHome=tmpfs` plus a
`BindPaths=` mount for that root. This keeps other home directories hidden while
making the configured storage reachable and writable.

### Image download network policy

URL downloads are validated at the socket, not only at the URL. AstinusLab resolves the hostname
itself, and under the default `network_policy = "safe"` refuses any answer that is not a global
address — loopback, RFC1918, link-local, unique-local, and the `169.254.169.254` metadata address
are blocked, including IPv4-mapped forms. It then connects to that pinned IP, checks `getpeername()`
against it before sending the request, and validates the TLS certificate against the original
hostname. DNS rebinding between validation and connection cannot change the peer.

Redirects are followed manually, not by the HTTP client: each hop is re-validated through the same
policy, capped at five hops, and a repeated URL is treated as a loop and fails. `source_policy`
selects which domains are acceptable — `official_only` (the built-in distribution mirrors),
`allowlist` (`allowed_domains`, which must be non-empty), or an unrestricted policy — and a
rejected domain fails with `403` before any connection is made. URLs carrying credentials, a
non-`http(s)` scheme, or port `0` are refused, and `max_size_gb` bounds the transfer.

Each download stages into a private per-attempt directory (`images/.staging/<uuid>`, mode `0700`)
and is published by linking the finished file into `images_dir` — the link fails if the name is
already taken, and the published inode is compared against the staged one. Two concurrent downloads
of the same image can therefore neither share nor destroy each other's partial file, and cleanup is
pinned to the operation's own directory inode.
