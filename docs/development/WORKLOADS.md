# Workloads & Scenarios Development

This guide explains how to create, configure, and debug workloads and scenarios (automated tasks) in AstinusLab.

## Overview

A **VM Task** in AstinusLab can be either a persistent workload or a standalone scenario script:

1.  **Workload**: A set of services or configurations installed for interactive use (e.g., a vulnerable web app). Used with `manual_trigger` mode.
2.  **Scenario**: A lifecycle configuration that determines when a capture occurs. Custom scenarios use `post_script` mode to run a standalone task and capture immediately upon its completion.

---

## 1. Creating a Single Workload

### Step A: Create the Install Script
Place your bash script in `guest/workloads/<workload-id>/install.sh`.

**Guidelines for Install Scripts:**
*   **Use `set -euo pipefail`**: Ensure the script exits on any error.
*   **Non-interactive**: Use `export DEBIAN_FRONTEND=noninteractive` and `-y` flags for package managers (e.g., `apt-get install -y`).
*   **Idempotency**: Scripts should ideally be runnable multiple times, though AstinusLab typically runs them once on a clean image.
*   **Logging**: Use `echo "[my-workload] message"` to provide progress updates that appear in the AstinusLab UI.
*   **ShellCheck**: AstinusLab runs `shellcheck` on your script before execution. Avoid unused variables or syntax errors.

**Example (`guest/workloads/hello-world/install.sh`):**
```bash
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[hello-world] Installing Nginx..."
sudo apt-get update -qq
sudo apt-get install -y nginx

echo "[hello-world] Configuring landing page..."
echo "<h1>AstinusLab Workload Ready</h1>" | sudo tee /var/www/html/index.html

echo "[hello-world] Installation complete."
```

### Step B: Register in `config/catalog/workloads.yaml`
Add an entry to the `workloads` list:

```yaml
- id: hello-world
  enabled: true
  display_name: Hello World Nginx
  tags:
    - web
    - tutorial
  install_script: guest/workloads/hello-world/install.sh
  health_check:
    url_path: /
    port: 80
    expected_status: 200
    timeout_seconds: 60
  services:
    - name: HTTP
      port: 80
      url_path: /
  compatibility:
    include:
      - family: debian
```

---

## 2. Creating a Workload Stack

Stacks allow you to combine existing workloads without duplicating scripts.

**Example (`config/catalog/workloads.yaml`):**
```yaml
- id: lamp-stack
  type: stack
  enabled: true
  display_name: Full LAMP Stack
  components:
    - mysql-base
    - apache-php
    - wordpress-6.4
  compatibility:
    include:
      - family: debian
```

---

## 3. Configuration Reference

| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | string | Unique identifier used in the API and DB. |
| `type` | string | `stack` for multi-component workloads, omit for single. |
| `display_name` | string | Human-readable name shown in the UI. |
| `install_script` | path | Path relative to the repository root. |
| `health_check` | object | (Optional) URL to poll to verify the workload is ready. |
| `services` | list | Services to display in the UI "Lab Services" panel. |
| `cve_ids` | list | CVEs associated with this workload (for searching). |
| `network_isolation` | bool | If true, blocks the VM from outbound internet/LAN access after provisioning. If the isolation rules cannot be applied, the lab boot fails (fail closed). |
| `compatibility` | object | Rules for matching this workload to target OS images. |

### Compatibility Rules
Workloads can be restricted to specific targets based on `family` (e.g., `debian`, `redhat`), `distro` (e.g., `ubuntu`), or `version`.

```yaml
compatibility:
  include:
    - family: debian
      version: '22.04'
  exclude:
    - distro: kali
```

---

## 4. The Provisioning Lifecycle

When a user launches a workload:
1.  **Image Preparation**: AstinusLab creates a QCOW2 overlay of the base target image.
2.  **VM Boot**: The VM is started using libvirt.
3.  **SSH Wait**: The runner waits for the guest to become accessible via SSH.
4.  **Workload Sync**: The repository is mounted into the guest via `virtiofs` (or copied via SSH if fallback is used).
5.  **Execution**: The `install_script` is executed inside the guest.
6.  **Health Check**: The runner polls the `health_check` URL until it returns the `expected_status`.
7.  **Snapshot**: A libvirt snapshot (`lab-clean`) is created.
8.  **Ready**: The job enters `waiting_for_capture` state and is handed over to the user.

---

## 5. Creating Custom Scenarios (Tasks)

While workloads are for interactive labs, **Scenarios** allow you to automate a task (e.g., simulating an attack or a system event) and capture the VM state automatically when it's done.

### Step A: Create the Scenario Script
Scenario scripts are standard bash scripts placed in `guest/scripts/`.

**Example (`guest/scripts/simulate-infection.sh`):**
```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[task] Simulating a malicious process..."
# In a real scenario, this might download a sample or execute a payload
sleep 10
nohup python3 -m http.server 9999 &

echo "[task] Simulation complete. Signalling for capture."
```

### Step B: Register in `config/catalog/scenarios.yaml`
Add a new scenario using `mode: post_script`:

```yaml
- id: auto-infect-capture
  enabled: true
  mode: post_script
  description: Simulates an infection and captures immediately
  script:
    enabled: true
    path: guest/scripts/simulate-infection.sh
    timeout_seconds: 300
    fail_job_on_nonzero_exit: true
```

### When to use Scenarios vs. Workloads?
*   **Use a Scenario** when you want a "fire and forget" task that leads to an automated capture.
*   **Use a Workload** when you want to provision a stable environment for manual exploration or multiple capture points.

---

## 6. Troubleshooting & Debugging

### View Logs
*   **Host side**: Check `storage/logs/runner.log` for script execution errors and exit codes.
*   **UI side**: The "Event Log" on the Job Detail page shows real-time output from the install script.

### Failed Provisioning
If a workload fails:
1.  Check if `shellcheck` reported errors in the Event Log.
2.  Check the "Error Summary" for exit codes.
3.  Set `keep_failed_vm = true` in `config/catalog/targets.yaml` for the target you are testing. This prevents AstinusLab from deleting the VM on failure, allowing you to SSH in manually and debug:
    ```bash
    ssh -i storage/ssh_keys/key <guest_user>@<guest_ip>
    ```

### Common Issues
*   **Permission Denied**: Ensure your script uses `sudo` for commands requiring root, as scripts run as the default `guest_user` (usually `ubuntu` or `cloud-user`).
*   **Missing Dependencies**: Remember that base cloud images are often minimal. You may need to run `apt-get update` before installing packages.
*   **Network Timeouts**: If your script downloads large files, ensure the `timeout_seconds` in the `health_check` is high enough to cover the entire installation time.
