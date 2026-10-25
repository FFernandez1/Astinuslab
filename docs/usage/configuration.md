# Configuration

AstinusLab has three configuration layers:

| Layer | Files | Purpose |
| --- | --- | --- |
| Static settings | `astinuslab.toml`, `ASTINUSLAB_*` environment variables, `config/astinuslab.env` | Backend storage, libvirt, catalog, Celery, Volatility 3, auth, and API bind settings. |
| Runtime settings | `app_settings` table in the configured application database (`db_path`) | UI-edited retention, disk guardrail and image-admin settings. |
| Catalogs | `config/catalog/*.yaml` | Target images, scenarios, workloads, attacker images, network nodes, and profiles. |

## Configuration precedence

Static settings use this precedence chain:

1. `ASTINUSLAB_*` environment variables.
2. `astinuslab.toml`.
3. Built-in code defaults.

`config/astinuslab.env` is application configuration, not launcher-only state: `astinuslab.sh` sources it with `set -a`, and release systemd units read it with `EnvironmentFile=`, so every `ASTINUSLAB_*` value in that file is exported into the API server, worker, and DB CLI. Plain shell exports use the same highest-precedence layer.

Runtime settings changed from the UI are separate from static configuration and are authoritative in the `app_settings` database table. Retention and the disk guardrail use its `app_settings` key; image administration uses its own keys. The current loader does not read or import `<storage_dir>/settings.json`. Copying or editing a legacy JSON file does not back up or change the active policy. Back up the application database with the [verified SQLite backup procedure](../reference/ADVANCED_CONFIG.md#schema-migration-and-rollback), together with the relevant configuration and evidence; changing `storage_dir` does not relocate `db_path`.

Split-identity installations pin managed SQLite endpoints to
`<install_dir>/storage/databases/` in `config/astinuslab.env`. The defaults table
below describes source defaults; maintenance must resolve the installed overrides.
See [installed split database paths](../reference/ADVANCED_CONFIG.md#installed-split-database-paths).

Retention is evidence-aware: capture artifacts linked to an Acquisition or a non-closed Case, or whose Job is not terminal, are never auto-deleted. Eligible deletion commits a custody tombstone before removing the artifact and its sidecars, with interrupted cleanup tracked for retry. See [Forensic Integrity](../reference/FORENSIC_INTEGRITY.md#evidence-deletion).

## Application Settings

`src/astinuslab/core/config.py` loads `astinuslab.toml` from the repository or install root. `./astinuslab.sh setup` seeds `astinuslab.toml` from `astinuslab.toml.example` when missing; upgrades leave the live file untouched because it is local operator state. Relative paths are resolved from that root. Values can also be overridden with `ASTINUSLAB_<SETTINGS_FIELD>` environment variables, where the field name is the flattened `Settings` field, for example `ASTINUSLAB_STORAGE_DIR` or `ASTINUSLAB_CELERY_BROKER_URL`. Unknown TOML sections or keys fail startup; keep custom values on documented keys or `ASTINUSLAB_<SETTINGS_FIELD>` overrides.

The default `astinuslab.toml` contains these sections:

| Section | Key settings |
| --- | --- |
| `[orchestration]` | `api_host`, `api_port`, `api_log_level`, `log_to_file`, `log_max_bytes`, `log_backup_count`, `log_format`, `libvirt_uri`, `libvirt_network`, `max_concurrent_vms` |
| `[storage]` | `storage_dir`, `db_path`, `images_dir`, `playground_disk_dir`, `playground_capture_dir`, `libvirt_image_base_dir` |
| `[packages]` | Package feature switch, source-network and guest-egress policies, archive/content limits, and package-content GC grace period |
| `[catalogs]` | `targets`, `scenarios`, `workloads`, `attackers`, `network_nodes` |
| `[vol3]` | `enabled`, `symbols_path`, `dwarf2json_path`, `volatility_bin`, provider limits and mirror URLs |
| `[vol3.provider_timeouts]` | provider-specific timeouts for `prebuilt_isf`, `debuginfod`, `host_fetch`, and `live_guest` |
| `[celery]` | `broker_url`, `result_backend`, reconciliation settings |
| `[image_inspection]` | `enabled`, `timeout_seconds`, `virt_inspector_bin` |
| `[auth]` | `enabled`, `admin_api_token_hash`, network-bind safety override |

`playground_disk_dir` and `playground_capture_dir` default to `<repo>/storage/playground/disks` and `<repo>/storage/playground/captures` whether or not the TOML keys are present. They must be different and must not be nested. The settings model rejects nested paths because startup recovery cleans disk overlays and must never clean capture artifacts. `libvirt_image_base_dir` defaults to `/var/lib/libvirt/images/astinuslab` because libvirt/qemu must traverse that system path.

### Portable package settings

Portable packages are experimental, unaccepted and outside supported 1.0.0
scope. `[packages]` stays disabled by default. With `enabled = false`, AstinusLab does not
mount `/api/v1/lab-packages` and does not render the Playground package-export
control. Enable it only for controlled experimental testing of inspection/import,
Prepare, Preflight, Launch, Reset, Teardown and content GC. See the
[experimental package guide](../user/PORTABLE_LABS.md) for the acceptance limits.

`allow_source_network` is the explicit local policy for pinned source retrieval.
`allow_guest_egress` is the local policy for guest NAT/egress and still requires
an explicit launch acknowledgement. Keep both `false` to deny network use;
embedded or cached content alone does not prove offline acceptance. `max_asset_bytes` and
`max_archive_bytes` enforce package content limits (16 GiB and 40 GiB by
default). `gc_grace_seconds` controls how long unreferenced package content is
retained before garbage collection may remove it (24 hours by default).

### Jobs capacity

Multiple Jobs may retain live targets, each with its optional attacker. Playground remains exclusive with Jobs and permits one active session. Configure the following keys in the existing `[orchestration]` section, or use their `ASTINUSLAB_` environment overrides:

| Key | Default | Accounting |
| --- | --- | --- |
| `max_concurrent_vms` | unset | Optional aggregate VM count limit, including pending target/attacker reservations and retained or unknown domains. Physical RAM and storage checks still apply when unset. |
| `capacity_ram_headroom_mb` | `2048` | RAM in MiB reserved for the host. Both total committed RAM and current available RAM must fit. |
| `capacity_storage_headroom_gb` | `10` | Free storage in GiB reserved on the evidence and VM-image filesystems. |
| `capacity_vm_overhead_mb` | `256` | Additional RAM in MiB charged per VM. |

Admission reserves each complete target/attacker pair atomically before boot. RAM uses frozen guest sizes plus overhead, or observed domain maximum RAM plus overhead when larger. Storage reserves the full virtual disks plus capture staging: twice the target RAM for memory capture and twice the target disk size for disk capture. These conservative reservations remain charged alongside actual filesystem usage until cleanup is confirmed.

Requests exceeding total limits are rejected. Temporary shortages return `waiting_for_capacity` with `capacity_reason`; Jobs displays **Waiting for capacity**. Admission checks are scheduled every 15 seconds on the single lifecycle worker, preserving FIFO order without occupying a worker while waiting. Earlier waiting pairs have priority over later single targets. Capture and cancellation remain available for existing live Jobs. Resources are released only after both expected domains and their owned overlay/temporary directories are absent; failed cleanup is retried through that same worker. A target configured with `keep_failed_vm` deliberately retains its failed resources and capacity until cleanup.

See the capacity settings below for current defaults.

## Launcher Environment

Copy the example file when you need to override local launcher values:

```bash
cp config/astinuslab.env.example config/astinuslab.env
```

`astinuslab.sh` reads:

| Variable | Default in script | Purpose |
| --- | --- | --- |
| `ASTINUSLAB_API_HOST` | `127.0.0.1` | API bind host read by Settings and used by `astinuslab.serve`. |
| `ASTINUSLAB_API_PORT` | `8000` | API and UI port read by Settings and used by `astinuslab.serve`. |
| `ASTINUSLAB_API_LOG_LEVEL` | `info` | Uvicorn log level read by Settings and used by `astinuslab.serve`. |
| `ASTINUSLAB_AUTH_ENABLED` | `false` | Enables single-admin auth for every HTTP route except `/health`, `/ready`, `/login`, `/logout`, and `/static/*`. Required before non-loopback binds unless unsafe override is set. |
| `ASTINUSLAB_ADMIN_API_TOKEN_HASH` | unset | SHA-256 hash of the admin API token, formatted as `sha256:<64-hex>`. |
| `ASTINUSLAB_ALLOW_UNSAFE_NETWORK_BIND_WITHOUT_AUTH` | `false` | Explicit escape hatch for controlled local experiments only. |
| `ASTINUSLAB_DOCS_PORT` | `9000` | MkDocs development server port when using `start --docs`. |
| `ASTINUSLAB_LIBVIRT_URI` | `qemu:///system` | Libvirt URI used by launcher cleanup helpers. |
| `ASTINUSLAB_LIBVIRT_NETWORK` | `default` | Libvirt network used by launcher cleanup helpers. |
| `ASTINUSLAB_SERVICE_USER` | current user | Systemd instance user checked by `status` and guarded by `start`. |

The systemd API unit also reads `/opt/astinuslab/config/astinuslab.env`; `ASTINUSLAB_API_HOST`, `ASTINUSLAB_API_PORT`, and `ASTINUSLAB_API_LOG_LEVEL` flow through Settings before `astinuslab.serve` starts Uvicorn. If `ASTINUSLAB_API_HOST` is set to a LAN address, configure `ASTINUSLAB_AUTH_ENABLED=true` and `ASTINUSLAB_ADMIN_API_TOKEN_HASH` first.

### Admin bearer-token auth

When `[auth].enabled` (or `ASTINUSLAB_AUTH_ENABLED`) is `true`, AstinusLab requires admin authentication for every HTTP route except the explicit public allowlist: `/health`, `/ready`, `/login`, `/logout`, and static assets under `/static/*`. API clients should send `Authorization: Bearer <admin-token>` for JSON routes such as `/api/v1/*` and `/volatility/api/*`.

A successful browser login issues an independent opaque session cookie (HttpOnly, SameSite=Strict), with a fixed 12-hour lifetime and only its hash stored server-side. The raw admin API token is not stored in this cookie; logout revokes the server-side session. See [Authentication](../api/authentication.md).

## Admin Console

The server-rendered admin console is available from the left sidebar. In release mode, these pages require the admin login; development checkouts normally serve them without authentication on loopback.

| Page | Path | Purpose |
| --- | --- | --- |
| Images | `/images` | List local guest images, upload or fetch images, inspect image metadata, and remove images. |
| Workloads | `/workloads` | Search, create, edit, and delete workload templates. Changes are written to the active workload catalog. |
| Storage | `/storage` | Inspect storage capacity and evidence-storage buckets. The tabs also link to Dumps and Workload Cache. |
| Dumps | `/dumps` | Review memory and disk artifacts, download evidence, remove selected artifacts, and clean up orphan files. Deletion is irreversible. |
| Workload Cache | `/workload-cache` | Inspect cached workload snapshot images and delete individual, selected, or all cache entries. |
| Logs | `/logs` | View application and job logs, filter by level, search the current file, follow updates, copy the visible log, or download it. |
| Security | `/ssh-keys` | Generate, select, and delete SSH keypairs used by lab VMs. The page also exposes API-key generation when admin authentication is enabled. |
| Settings | `/settings` | Inspect service, network, and storage status and apply operational settings. |

Settings includes service status and controls, managed libvirt network and DHCP lease information, retention windows, retention preview and immediate cleanup, a minimum-free-space disk guardrail, and Volatility symbol/ISF cache clearing. The disk guardrail blocks new jobs below its configured free-space threshold; set it to `0` to disable it. Clearing the reusable symbol cache does not delete job-local analysis outputs.

Treat `storage/ssh_keys/` as a credential store: its passphraseless private keys grant automated access to lab guests and must not be copied with evidence or placed on shared storage. Evidence deletion from Dumps, retention cleanup, and workload-cache cleanup cannot be undone. Follow [Forensic Integrity](../reference/FORENSIC_INTEGRITY.md) when exporting evidence.

### Operational probes

Use public `GET /health` for dependency-free process liveness and `GET /ready` for dependency readiness. `/ready` returns `503` only when the database or writable storage is unavailable; worker or libvirt failures return `200` with `status: degraded`. Both responses include `X-Request-ID` for correlation with backend logs. See [API endpoints](../api/endpoints.md#operational-probes).

## Catalogs

The app reads catalog paths from `[catalogs]` in `astinuslab.toml`.

| File | Defines |
| --- | --- |
| `config/catalog/targets.yaml` | Linux base images, usernames, distro metadata, VM resources, cloud-init behavior, and lifecycle flags. |
| `config/catalog/scenarios.yaml` | Job execution modes such as `immediate`, `post_script`, and `manual_trigger`. |
| `config/catalog/workloads.yaml` | Workload install scripts, services, CVE tags, health checks, and compatibility metadata. |
| `config/catalog/attackers.yaml` | Attacker VM images usable with manual lab sessions. |
| `config/catalog/network_nodes.yaml` | Network Node images for routed, filtered, inspected, or shaped Playground topologies. |
| `config/catalog/profiles.yaml` | Profile metadata used by the catalog layer. |

Example catalogs are provided with `.example.yaml` suffixes. Live catalog files are local operator state and are gitignored. `./astinuslab.sh setup` copies missing `.example.yaml` files to live `.yaml` paths on first run and never overwrites existing live catalogs, so upgrades do not clobber operator edits.

## Image Inspection

When `virt-inspector` is available, AstinusLab inspects images in `images/definitions/` and writes `<image>.meta.json` sidecars. If inspection is disabled or unavailable, filename heuristics are used.

## Volatility 3

Reusable Linux ISFs are stored under `vol3/symbols/linux/`. Per-job plugin output is written under `storage/dumps/<job_id>/vol3/`. Prebuilt ISF mirrors must use `https://` in normal deployments; `file://` is intended for local tests and controlled offline environments.

Volatility 3 Framework 2.28.0 and `dwarf2json` are operator-installed tools,
not AstinusLab dependencies. Keep them outside the checkout and configure
`volatility_bin`, `dwarf2json_path`, and `dwarf2json_sha256` explicitly. The
operator chooses the installation source, reviews upstream licenses and hashes,
and records provenance. AstinusLab never downloads or accepts those licenses.
After restarting services, check `/api/v1/volatility/readiness`; a missing tool
disables only its capability, while manual ISF upload remains available.

## Privileged storage roots

Installed ACL helpers accept only roots registered in root-owned
`/etc/astinuslab/acl-policy.json`. Register a custom, absolute existing storage
directory from the installed checkout; `/`, broad system prefixes, and `/home`
or `/var` themselves are rejected, while dedicated subdirectories below
`/home` and `/var` are supported:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab \
  --storage-root /srv/astinuslab
```

First set `[storage] storage_dir` or `ASTINUSLAB_STORAGE_DIR` to that same
path. This changes the destination for future Job output only: it does not copy
historical evidence or rewrite database references. Playground captures and
disks use their independently configured roots, and `db_path` remains independent
under `<data_dir>/storage` by default. Rendered units keep required roots writable.
Keep old roots available until a separately planned migration has copied and
verified artifacts and sidecars, reconciled their database references, and proved
read access. Registration alone is not a migration or permission to retire data;
see [Privileged storage policy](../reference/ADVANCED_CONFIG.md#privileged-storage-policy).
Use the current service identity and the stable `astinuslab` group for the root
(mode `2770`). Every parent must be root-owned and not writable by group or
others; use `root:astinuslab` mode `0750` where the group needs traversal. That
layout remains valid after an optional split-identity migration.

The command validates policy JSON, fsyncs temporary root-owned files, atomically
re-renders systemd units, and runs no restart unless `--restart` is supplied.
Use `--check` to report policy or unit mismatch without mutation. Custom roots
outside `/home` are also reflected in `ReadWritePaths`; roots below `/home` give
every service unit `ProtectHome=tmpfs` and a `BindPaths=` mount for the configured root.

## Browser origin allowlist

Browser mutations (`POST`, `PUT`, `PATCH`, `DELETE`) must carry an `Origin` AstinusLab recognises
and echo the `astinuslab_csrf` cookie in the `X-CSRF-Token` header or a `csrf_token` form field.
Without configuration the accepted origins are the loopback ones on `api_port`:
`http://127.0.0.1:<port>`, `http://localhost:<port>`, and `http://[::1]:<port>`.

Serving the UI through a reverse proxy or another hostname requires listing that origin, which
**replaces** the loopback defaults:

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

Entries are exact scheme, host, and port; paths, credentials, and wildcards are rejected at
startup. API clients that send no `Origin` header, and requests carrying an admin bearer token, are
unaffected. See [Authentication](../api/authentication.md#browser-mutation-protection).

## Environment variable reference

<!-- BEGIN GENERATED ENV REFERENCE (tools/dev/generate_env_reference.py) -->

| Variable | Type | Default |
|---|---|---|
| `ASTINUSLAB_BASE_DIR` | `Path` | `PosixPath('<base_dir>')` |
| `ASTINUSLAB_DATA_DIR` | `Path` | `PosixPath('<data_dir>')` |
| `ASTINUSLAB_API_HOST` | `str` | `'127.0.0.1'` |
| `ASTINUSLAB_API_PORT` | `int` | `8000` |
| `ASTINUSLAB_DOCS_PORT` | `int` | `9000` |
| `ASTINUSLAB_API_LOG_LEVEL` | `str` | `'info'` |
| `ASTINUSLAB_LOG_TO_FILE` | `bool` | `False` |
| `ASTINUSLAB_LOG_MAX_BYTES` | `int` | `10485760` |
| `ASTINUSLAB_LOG_BACKUP_COUNT` | `int` | `5` |
| `ASTINUSLAB_LOG_FORMAT` | `str` | `'text'` |
| `ASTINUSLAB_LIBVIRT_URI` | `str` | `'qemu:///system'` |
| `ASTINUSLAB_LIBVIRT_NETWORK` | `str` | `'default'` |
| `ASTINUSLAB_MAX_CONCURRENT_VMS` | `int &#124; None` | `None` |
| `ASTINUSLAB_CAPACITY_RAM_HEADROOM_MB` | `int` | `2048` |
| `ASTINUSLAB_CAPACITY_STORAGE_HEADROOM_GB` | `int` | `10` |
| `ASTINUSLAB_CAPACITY_VM_OVERHEAD_MB` | `int` | `256` |
| `ASTINUSLAB_AUTH_ENABLED` | `bool` | `False` |
| `ASTINUSLAB_ADMIN_API_TOKEN_HASH` | `str &#124; None` | `None` |
| `ASTINUSLAB_ALLOW_UNSAFE_NETWORK_BIND_WITHOUT_AUTH` | `bool` | `False` |
| `ASTINUSLAB_BROWSER_ALLOWED_ORIGINS` | `list` | `[]` |
| `ASTINUSLAB_STORAGE_DIR` | `Path` | `PosixPath('<data_dir>/storage')` |
| `ASTINUSLAB_DB_PATH` | `Path` | `PosixPath('<data_dir>/storage/astinuslab.db')` |
| `ASTINUSLAB_IMAGES_DIR` | `Path` | `PosixPath('<data_dir>/images/definitions')` |
| `ASTINUSLAB_SERIAL_LOG_DIR` | `Path` | `PosixPath('<data_dir>/storage/logs/serial')` |
| `ASTINUSLAB_PLAYGROUND_DISK_DIR` | `Path` | `PosixPath('<data_dir>/storage/playground/disks')` |
| `ASTINUSLAB_PLAYGROUND_CAPTURE_DIR` | `Path` | `PosixPath('<data_dir>/storage/playground/captures')` |
| `ASTINUSLAB_LIBVIRT_IMAGE_BASE_DIR` | `Path` | `PosixPath('/var/lib/libvirt/images/astinuslab')` |
| `ASTINUSLAB_PLAYGROUND_DEFAULT_VCPUS` | `int` | `2` |
| `ASTINUSLAB_PLAYGROUND_DEFAULT_MEMORY_MB` | `int` | `2048` |
| `ASTINUSLAB_PLAYGROUND_CAPTURE_STREAM_TIMEOUT_SECONDS` | `int` | `3600` |
| `ASTINUSLAB_PLAYGROUND_CAPTURE_TIMEOUT_SECONDS` | `int` | `900` |
| `ASTINUSLAB_PACKAGES_ENABLED` | `bool` | `False` |
| `ASTINUSLAB_PACKAGES_ALLOW_SOURCE_NETWORK` | `bool` | `False` |
| `ASTINUSLAB_PACKAGES_ALLOW_GUEST_EGRESS` | `bool` | `False` |
| `ASTINUSLAB_PACKAGES_MAX_ASSET_BYTES` | `int` | `17179869184` |
| `ASTINUSLAB_PACKAGES_MAX_ARCHIVE_BYTES` | `int` | `42949672960` |
| `ASTINUSLAB_PACKAGES_GC_GRACE_SECONDS` | `int` | `86400` |
| `ASTINUSLAB_TARGETS_CATALOG_PATH` | `Path` | `PosixPath('<data_dir>/config/catalog/targets.yaml')` |
| `ASTINUSLAB_SCENARIOS_CATALOG_PATH` | `Path` | `PosixPath('<data_dir>/config/catalog/scenarios.yaml')` |
| `ASTINUSLAB_WORKLOADS_CATALOG_PATH` | `Path` | `PosixPath('<data_dir>/config/catalog/workloads.yaml')` |
| `ASTINUSLAB_ATTACKERS_CATALOG_PATH` | `Path` | `PosixPath('<data_dir>/config/catalog/attackers.yaml')` |
| `ASTINUSLAB_NETWORK_NODES_CATALOG_PATH` | `Path` | `PosixPath('<data_dir>/config/catalog/network_nodes.yaml')` |
| `ASTINUSLAB_VOL3_ENABLED` | `bool` | `True` |
| `ASTINUSLAB_VOL3_SYMBOLS_PATH` | `Path` | `PosixPath('<data_dir>/vol3/symbols')` |
| `ASTINUSLAB_VOL3_DWARF2JSON_PATH` | `pathlib.Path &#124; None` | `None` |
| `ASTINUSLAB_VOL3_DWARF2JSON_SHA256` | `str &#124; None` | `None` |
| `ASTINUSLAB_VOL3_VOLATILITY_BIN` | `str &#124; None` | `None` |
| `ASTINUSLAB_VOL3_DEFAULT_PLUGINS` | `str` | `'linux.bash.Bash,linux.pslist.PsList,linux.pstree.PsTree,linux.pmap.Pmap'` |
| `ASTINUSLAB_VOL3_FETCH_TIMEOUT_SECONDS` | `int` | `1800` |
| `ASTINUSLAB_VOL3_ISF_MIRROR_URLS` | `list` | `[]` |
| `ASTINUSLAB_VOL3_PREBUILT_MAX_COMPRESSED_BYTES` | `int` | `67108864` |
| `ASTINUSLAB_VOL3_PREBUILT_MAX_DECOMPRESSED_BYTES` | `int` | `1073741824` |
| `ASTINUSLAB_VOL3_DEBUGINFOD_URLS` | `list` | `[]` |
| `ASTINUSLAB_VOL3_DEBUGINFOD_MIN_FREE_BYTES` | `int` | `2147483648` |
| `ASTINUSLAB_VOL3_PROVIDER_TIMEOUTS` | `dict` | `{'prebuilt_isf': 60, 'debuginfod': 900, 'host_fetch': 1800, 'live_guest': 1800}` |
| `ASTINUSLAB_VOL3_DWARF2JSON_TIMEOUT_SECONDS` | `int` | `600` |
| `ASTINUSLAB_VOL3_ALLOW_INVASIVE_GUEST_SYMBOL_INSTALL` | `bool` | `False` |
| `ASTINUSLAB_VOL3_PLUGIN_RUN_MAX_ROWS` | `int` | `50000` |
| `ASTINUSLAB_IMAGE_INSPECTION_ENABLED` | `bool` | `True` |
| `ASTINUSLAB_IMAGE_INSPECTION_TIMEOUT_SECONDS` | `int` | `60` |
| `ASTINUSLAB_IMAGE_INSPECTION_VIRT_INSPECTOR_BIN` | `str` | `'virt-inspector'` |
| `ASTINUSLAB_CELERY_BROKER_URL` | `str` | `'sqla+sqlite:///<data_dir>/storage/celery_broker.db'` |
| `ASTINUSLAB_CELERY_RESULT_BACKEND` | `str` | `'db+sqlite:///<data_dir>/storage/celery_results.db'` |
| `ASTINUSLAB_CELERY_RECONCILIATION_ENABLED` | `bool` | `True` |
| `ASTINUSLAB_CELERY_RECONCILIATION_INTERVAL_SECONDS` | `int` | `60` |
| `ASTINUSLAB_CELERY_RECONCILIATION_GRACE_SECONDS` | `int` | `120` |

List-typed settings accept comma-separated values or a JSON array; dict-typed settings accept a JSON object.

<!-- END GENERATED ENV REFERENCE -->
