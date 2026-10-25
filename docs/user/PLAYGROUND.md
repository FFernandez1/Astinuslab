# Playground Mode

Use Playground for interactive lab changes and on-demand captures; use [Jobs](../usage/examples.md) for repeatable Scenario runs. If the image picker is empty, follow **Manage images** or the **Image guide** for that role. A failed catalogue read offers **Retry loading images**, which preserves the form and does not create a lab. **Form complete — host readiness not checked** confirms only the form; image presence is not proof that Workload bytes or host prerequisites are prepared.

See also: [Playground REST API reference](../api/playground.md).

The Playground is AstinusLab's front-door workflow for attack-and-forensics labs. It lets you spin up vulnerable targets, attacker VMs, and Network Nodes, connect them via multi-segment networks, run security experiments, capture evidence on demand, and tear everything down when you're done — all in a single session.

The API examples below use the reusable `api_curl` Bash helper documented in [Examples](../usage/examples.md), which handles Bearer authentication in release mode and CSRF token seeding on mutating requests in development mode.


For a first success, start with **+ Target → No workload** and capture a bare VM. A session is created automatically. For a vulnerable workload on the default isolated network, first prepare a matching image/workload cache in Jobs or use an offline-prepared image. Online installation needs an explicitly chosen egress-enabled network; the wizard does not grant internet access or check cache availability. See [Prepare workloads](PREBAKED_IMAGES.md) before launch. Its Review step checks the form, not host readiness.

## What is the Playground?

Think of the Playground as an ephemeral lab where you can:

- **Spin up targets, attackers, and Network Nodes** without first creating a Job. Pick catalog images, label them, and boot them.
- **Build networks** — isolated or routed, with custom CIDRs. Use multiple networks to model pivot topologies.
- **Capture evidence** — memory and disk dumps on any running VM, any time, with manifest and SHA-256 sidecars.
- **Work interactively** — inspect VMs, run attacks or defensive checks, trigger captures as you go.
- Cleanup is asynchronous; ending the session releases its VMs and networks while retaining completed evidence in Storage. Wait for the terminal cleanup status and resolve any failure.

### Key constraints

- **Capacity and exclusivity** — several Jobs can keep targets and optional Attackers alive within aggregate RAM, disk and VM-count limits. The single lifecycle worker serializes operations; it does not impose a one-live-Job limit. One ordinary Playground session is allowed at a time and is exclusive with Jobs. Experimental Packages instances have their own lifecycle and are outside supported 1.0.0 scope. See [Configuration](../usage/configuration.md).
- Restart behavior depends on the service and session owner; see the restart and cleanup section below.
- **Recipe and package import/export** — guided UI lab creation is the V1 front door. Current Playground recipes are JSON (`schema_version: 1`) and can be exported from the active session or imported into a session. Scenario packages wrap that recipe YAML plus metadata and optional scripts in a zip for easier sharing. Broader hand-authored YAML workflows remain the long-term repeatability path.

---

## Starting a Playground Session

1. Open **Playground** in the left sidebar.
2. Add a network, spawn a VM, or import a recipe.
3. AstinusLab creates or reuses the active session automatically for that first lab action.

You'll see:

- A **header** with **+ Target**, **+ Attacker**, and **+ Network Node** buttons.
- A **lab map** - a draggable canvas of your VMs and networks and the links between them.
- **Role groups** - your Targets, Attackers, and Network Nodes as cards below the map.
- A **detail panel** on the right showing the selected machine's state, IPs, NICs, workload/services, SSH command, capture buttons, and evidence artifacts.
- A **footer** with the session ID, elapsed time, **Import Recipe** / **Export Recipe**, and **End Session** (shown once a session exists).

The REST API still exposes `POST /api/v1/playground/sessions` for clients that want to create an empty session explicitly; the web UI normally creates sessions implicitly.

---

## Creating Networks

Networks define the connectivity topology of your VMs. Each network has a name, CIDR range, and an isolation flag.

### Isolated vs. Routed Networks

- **Isolated** (`isolated=true`) — this libvirt network has no external forwarding. Guests can communicate with each other and with the host through its bridge address, subject to host/guest firewall rules. This does not isolate host services from guests. Use it for internal pivot segments; restrict host listeners separately.
- **Routed** (`isolated=false`) — VMs on this network can reach the host and other routed networks via NAT/routing. Use this for outbound connectivity or to bridge segments.

### Create a network

Networks are created from the create machine wizard's **network step** (open
**+ Target**, **+ Attacker**, or **+ Network Node**, then use **+ New** in the
Networks field), or from the standalone **Create network** dialog:

1. In the create wizard's Networks step, click **+ New** (or open the Create network dialog).
2. Enter a **Name** (e.g., `internal`, `dmz`, `guest`). A name is suggested if you leave it blank.
3. Enter a **CIDR** (e.g., `10.42.1.0/24`), or accept the auto-suggested next-free `10.42.x.0/24`. CIDRs within a session must not overlap; an overlapping request is auto-substituted with a note.
4. Leave **Isolated (private)** checked to disable external forwarding on this segment; uncheck it for routed (NAT) connectivity.
5. In the machine wizard, confirm the network to add it to the form, then finish creating the VM to create the lab resources. Adding a network to the form alone does not start resources. The standalone Create network dialog starts network creation when confirmed.

Once creation starts, the network shows as `pending`, then `active`. Wait for `active` before treating it as ready for VMs.

---

## Spawning VMs

A VM is booted from a catalog image and attached to one or more networks via NICs (network interfaces). Targets, attackers, and Network Nodes all use this VM flow.

### Network Nodes

Network Nodes are lightweight, generic multi-NIC VMs for placing an operator-managed appliance between Playground networks.

Use Network Nodes when you want to boot an image that already provides routing, firewall, proxy, VPN, IDS, or traffic-shaping behavior. AstinusLab attaches the NICs and applies small default resources (1 vCPU, 512 MB RAM, 4 GB disk), but it does not configure packet forwarding, IDS rules, VPN tunnels, or traffic shaping for you. Attach a Network Node to one or more networks; attach two or more NICs when you want it placed between segments.

Captures are disabled by default for Network Nodes; capture target VMs when you need forensic evidence.

### Spawn a VM

1. In the header, click **+ Target**, **+ Attacker**, or **+ Network Node**.
2. **Basics:** enter a **Label** (unique within the session; a name is suggested) and pick an **OS image**. Only playground-eligible images (valid path, format, arch, credentials) are shown.
3. **Workload** (targets only): optionally drop a vulnerable service onto the target. Incompatible workloads are shown but locked.
4. **Resources / Networks:** attach one or more networks (each adds one NIC), and size the VM (targets expose vCPU/memory/disk sliders; attackers and Network Nodes apply role defaults). If you attach nothing, an isolated `default` network (`10.42.1.0/24`) is created and used.
   - For a multi-segment pivot, attach 2+ networks.
   - For a Network Node between segments, attach each segment it should bridge.
5. **Review** (targets) or **Create** (attacker/Network Node) to add it to the live session.

AstinusLab boots the VM, polls for guest IP addresses, and populates them as they become available. Select the machine to open its **detail panel**, which once `running` shows:

- **Label** and **Image**
- **State** (`pending`, `booting`, `running`, `destroying`, `failed`)
- **IPs per NIC** — the guest IP assigned to each network interface
- **Action buttons** — Reboot, Shutdown, Start, Destroy, Capture RAM, Capture Disk

---

## Accessing VMs via SSH

Until VNC/SPICE console access is available in a future release, you must interact with VMs via SSH. AstinusLab automatically injects an SSH key into the VMs it spawns.

To SSH into a VM on a **routed or isolated** network from your host machine, use the workspace's managed key and the guest IP shown on the VM card:

Use the SSH command shown for that VM. The active key name is recorded in
`<storage_dir>/ssh_keys/.active`; its private file is
`<storage_dir>/ssh_keys/<active-name>` without a suffix. The launcher starts with
`astinuslab-default`, and **Security** can select a different key. Resolve
`storage_dir` from configuration rather than assuming the current code directory.
An existing VM may retain the key injected when it was created: preserve that
key if the active selection changes. With split service identities, use authorized
access rather than copying private keys to public locations.
*(Check the Images catalog page if you are unsure of the default `<username>` for a specific image.)*

**Isolated networks:** the host has an address on the libvirt bridge, so direct
host-to-guest SSH can use the guest IP and key above when SSH and firewall rules
permit it. A pivot VM is optional for access from another segment. Conversely,
guests may reach host listeners: no-forward networking is not a host firewall.
See [libvirt isolated network semantics](https://libvirt.org/formatnetwork.html#isolated-network-config).

---

## Viewing the Topology

The Playground displays an always-on **topology map** that shows how VMs and networks relate:

- Networks, VMs, and NIC edges are rendered as draggable map nodes and links.
- Dragging a node updates its position locally and saves the layout through the topology-layout API.
- The map is useful for validating multi-segment pivot setups before you start captures.

---

## Importing and Exporting Recipes and Packages

Playground supports two sharing formats:

- **JSON recipe** — raw `PlaygroundRecipe` from the active session.
- **Scenario package zip** — shareable archive with `metadata.yaml`, `scenario.yaml`, and optional safe relative script entries.

### Export a recipe

1. Start or open a Playground session.
2. Build networks, spawn target, attacker, and Network Node VMs, and arrange the topology.
3. Click **Export Recipe**.
4. AstinusLab downloads `<session-id>.recipe.json`.

Exported recipes include:

- `schema_version: 1`
- `name`
- `networks` with names, CIDRs, and isolation flags
- `vms` with labels, roles, image IDs, optional workload IDs, optional `vcpus`, `memory_mb`, `disk_gb`, and network names
- `topology` node positions

### Import a recipe

1. Click **Import Recipe**.
2. Paste recipe JSON.
3. Submit import.

AstinusLab validates unique network names and VM labels, rejects unknown network references, creates missing networks, spawns VMs, and restores topology positions. Import into an existing session fails with `409 Conflict` if recipe names collide with existing networks or VMs.

### Export a scenario package

Use the REST API to download the active Playground as a one-click share package:

```bash
api_curl -OJ "$BASE_URL/api/v1/scenario-packages/playground/current"
```

Package contents:

- `metadata.yaml` — package type, schema version, and display metadata.
- `scenario.yaml` — Playground recipe data.
- Optional script paths — safe relative entries included by backend package helpers when referenced by future catalog/workload flows.

### Import a scenario package with resource overrides

Upload the zip to another AstinusLab instance:

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip"
```

To resize machines without editing the archive, pass `resource_overrides` as JSON keyed by VM label:

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip" \
  -F 'resource_overrides={"target-1":{"vcpus":4,"memory_mb":8192,"disk_gb":80}}'
```

Allowed override fields are `vcpus`, `memory_mb`, and `disk_gb`. The import validates overrides through the same Playground recipe schema before creating networks or spawning VMs.

---

## Capturing Forensic Artifacts

You can trigger memory and disk captures on running target and attacker VMs. Captures are disabled by default for Network Nodes; capture target VMs when you need forensic evidence.

### Memory Capture

1. Select the VM on the lab map to open its detail panel.
2. Click **Capture RAM**.
3. AstinusLab acquires the guest RAM via `virDomainCoreDump` (ELF format, no guest modification).
4. Progress streams in the **capture console**; once complete the acquisition appears under **Evidence artifacts** in the VM's detail panel with download, manifest, and SHA-256 links.

### Disk Capture

1. Select the VM on the lab map to open its detail panel.
2. Click the **Capture Disk** button.
3. AstinusLab pauses the domain briefly, converts the disk to an uncompressed qcow2 artifact, and resumes the VM.
4. The artifact appears under **Evidence artifacts** in the VM's detail panel.

### Accessing Captured Artifacts

Select a VM to see its captures under **Evidence artifacts** in the detail panel,
each with **artifact**, **manifest**, and **sha256** download links. All captures
also remain in the Storage browser after the session ends.

### Artifact lifecycle

Captured artifacts are forensic evidence and are treated separately from the ephemeral session that produced them:

- **Captures persist across worker restarts.** When the Celery worker restarts, the startup-recovery sweep purges only stale VM disk overlays (under `playground_disk_dir`) for sessions that are no longer active. The capture directory (`playground_capture_dir`) is never touched by recovery, so completed memory dumps, disk images, manifests, and SHA-256 sidecars remain intact.
- **Captures persist across session destruction.** Ending the playground session destroys its VMs and networks, but the artifacts already produced by `Capture RAM` / `Capture Disk` stay on disk and their `Acquisition` rows stay in the database. They remain available through **Storage** and the storage browser until you explicitly delete them.
- **Chain of custody.** Each capture is written with a manifest and SHA-256 file alongside it; these are written once at capture time and are never modified by recovery, cleanup, or session-end paths.

---

## VM Power Control

The VM detail panel has action buttons. Wait for a state transition to finish before repeating an operation:

- **Reboot** — graceful reboot via ACPI. The VM stays booted and connected to its networks.
- **Shutdown** — graceful shutdown. The VM stops but the VM record remains. Click **Start** to bring it back up.
- **Start** — boot a VM that was shut down (only available when the VM is not running).
- **Destroy** — graceful shutdown → force destroy. The VM is removed, its disk is deleted, and the VM record is marked destroyed.

---

## Stopping the Session

When you're done:

1. Click the **End Session** button in the session status bar.
2. Cleanup is asynchronous; ending the session releases its VMs and networks while retaining completed evidence in Storage. Wait for the terminal cleanup status and resolve any failure.

All captured artifacts remain available in **Storage** until you manually delete them from Storage.

---

## Constraints & Validation Rules

- **Single active session** — you cannot start a new playground if one is already running. You must destroy the current session first.
- **Label uniqueness** — VM labels must be unique within a session.
- **CIDR overlap** — network CIDRs within a session must not overlap.
- **Network in use** — you cannot delete a network while VMs are still attached to it. Destroy the VMs first.
- **Capture state** — you can only trigger captures on target and attacker VMs in the `running` state. Captures are disabled by default for Network Nodes.
- **No overlapping job execution** — if a forensic Job is active, you cannot start a playground, and vice versa.

---

## Security model

V1 is localhost/trusted-local by default. If you bind the API/UI beyond loopback, enable single-admin API auth and still place AstinusLab behind TLS, VPN, firewall, or reverse-proxy controls for shared-network use.

Guest VMs are intentionally hostile or vulnerable. Hostile-guest hardening should be selected during lab creation when available; until those controls are complete, prefer isolated networks for exploit labs and treat guest credentials, service URLs, and captured artifacts as sensitive.

---

## Known Limitations (Deferred)

- **Console access** — VNC/SPICE console proxy is planned for a future release. Until then, use the guest IP to SSH directly from your machine (see [Accessing VMs via SSH](#accessing-vms-via-ssh)).
- **Snapshots and restore** — the playground does not support save-state / restore checkpoints.
- **Resource controls** — the API accepts per-VM `vcpus`, `memory_mb`, and `disk_gb` in recipes, package imports, and VM spawn calls. The target creator exposes vCPU, memory, and disk as sliders (memory is shown in GB but stored in MB). The attacker and Network Node creators are intentionally minimal — Label, OS Image, and Networks only — and apply their role default sizing silently.
- **User-uploaded images** — you cannot upload raw qcow2 files outside the catalog for playground use.
- **Windows targets** — roadmap; V1 focuses on Linux web-app vulnerable labs.
- **Live telemetry and richer report experiences** beyond the Volatility summaries and plugin reports already published — roadmap; V1 centers on captures, artifacts, and Volatility 3 output.

---

## Example Workflows

### Single-VM memory dump

1. Start a session.
2. Spawn one VM from your chosen image.
3. Wait for it to reach `running` state.
4. Click **Capture RAM**.
5. Download the dump from **Storage**.
6. Destroy the session.

### Multi-segment pivot topology

1. Start a session.
2. Create three networks: `dmz` (10.0.0.0/24, routed), `internal` (10.1.0.0/24, isolated), `sensitive` (10.2.0.0/24, isolated).
3. Spawn an `attacker` VM on `dmz` only.
4. Spawn a `pivot` VM on both `dmz` and `internal`.
5. Spawn a `target` VM on both `internal` and `sensitive`.
6. The attacker can reach the pivot via `dmz`; the pivot can see both segments; the target is behind the pivot.
7. Trigger captures on any VM to get forensic snapshots at any point.
8. Destroy the session when complete.

### Exploit and capture

1. Start a session.
2. Create a routed network and spawn a vulnerable VM.
3. From your host machine, SSH to the guest IP and run an exploit.
4. Once the exploit state is captured in memory, click **Capture RAM** from the UI.
5. Analyze the dump with Volatility 3 or your chosen tools; Volatility 3 is the core V1 memory-analysis path.
6. Destroy the session.

---

## Troubleshooting

### VM fails to boot or times out

- Check that the image is valid and has a default username configured (visible in the Images page).
- Ensure SSH connectivity is not blocked by firewall rules.
- Check the runner logs for guest-IP polling errors.

### Network creation fails with CIDR overlap error

- Review the CIDRs of existing networks. AstinusLab does not allow overlapping ranges within a session.
- Choose a disjoint CIDR and retry.

### Capture hangs or fails

- Ensure the VM is in the `running` state (check the VM card).
- Memory captures may take several minutes on large images. Check the runner logs for progress.
- Disk captures pause the VM briefly (~1s for small disks, longer for large ones). Allow time for the pause and convert operation to complete.

### Cannot delete a network

- Verify no VMs are still attached to the network. Destroy or reconnect all dependent VMs first.
- Check the error message in the UI for the list of VMs blocking the deletion.

### Restart and cleanup behavior

Restarting only the API or the analysis worker does not run ordinary Playground startup cleanup. Restarting the lifecycle runner invokes recovery and ends ordinary Playground sessions. Frozen creator sessions and active package-owned sessions are protected by their own verification/instance lifecycle. A whole-host restart also restarts these services and interrupts running guests; protection of a record is not a guarantee of uninterrupted VM execution.

**End Session** queues cleanup. The page follows that session after reload and reports success only for `cancelled` (the End Session outcome) or `completed`. `destroy_failed` or `failed` requires attention; host resources may remain reserved. Check **Logs** using the retained session ID. A missing record or unavailable status does not confirm cleanup. Completed captures and their sidecars remain in **Storage**, independent of the current canvas.


---

## Next Steps

- **Analyze with Volatility 3** — once you have a memory dump, use [Volatility](VOLATILITY3.md) to analyze the guest memory with your preferred plugins.
- **Learn the Job workflow** — for repeatable, scenario-driven acquisitions, see [Getting Started](../usage/getting-started.md).
- **Configure custom networks** — create Playground networks and attach VM NICs from the session UI or API. Advanced users can inspect generated libvirt network XML with `virsh net-dumpxml`.
