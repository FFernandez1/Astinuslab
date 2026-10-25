// Right-sidebar detail panel for the currently selected VM.
import { el, kvRow, barLine, cpuColor, phaseLabel, roleAccent, safeHref } from "./ui.js";
import { getState, setState, subscribe } from "./state.js";
import { captureStreamOnError, MAX_STREAM_ERRORS } from "./capture-stream.js";
import {
  startCapture,
  openCaptureStream,
  deleteNetwork,
  deleteVm,
  vmAction,
  attachNic,
  detachNic,
  refreshSession,
  getArtifactsForJob,
  getReportsForJob,
  listCaptures,
} from "./api.js";
function section(title, children) {
  return el("section", {
    className: "pg-v2-section",
    children: [el("div", { className: "section-title al-eyebrow", text: title }), ...children],
  });
}
function copyBtn(text) {
  const btn = el("button", {
    className: "pg-ssh-copy-btn",
    attrs: { type: "button", "aria-label": "Copy SSH command", "data-cleanup-readonly": "true" },
    text: "⧉",
  });
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      btn.dataset.ok = "true";
      setTimeout(() => delete btn.dataset.ok, 1200);
    } catch (_) {
      btn.dataset.ok = "false";
      setTimeout(() => delete btn.dataset.ok, 1200);
    }
  });
  return btn;
}
function artifactName(a) {
  return a.filename || (a.path || "").split("/").pop() || a.id;
}
function artifactLinks(a) {
  return el("div", {
    className: "pg-v2-capture-actions",
    children: [
      el("a", {
        className: "wiz-btn wiz-btn--secondary",
        attrs: { href: `/api/v1/artifacts/${encodeURIComponent(a.id)}/download` },
        text: "artifact",
      }),
      a.manifest_path
        ? el("a", {
            className: "wiz-btn wiz-btn--ghost",
            attrs: { href: `/api/v1/artifacts/${encodeURIComponent(a.id)}/manifest` },
            text: "manifest",
          })
        : null,
      a.sha256_sidecar_path
        ? el("a", {
            className: "wiz-btn wiz-btn--ghost",
            attrs: { href: `/api/v1/artifacts/${encodeURIComponent(a.id)}/sha256` },
            text: "sha256",
          })
        : null,
    ],
  });
}
function evidenceRows(artifacts, reports) {
  return (artifacts || []).map((a) => {
    const report = (reports || []).find((r) => r.output_artifact_id === a.id);
    return el("div", {
      className: "pg-v2-service-row",
      children: [
        el("div", {
          className: "pg-v2-service-main",
          children: [
            el("span", { className: "pg-v2-service-name", text: artifactName(a) }),
            el("span", { className: "pg-v2-service-status", text: a.kind }),
          ],
        }),
        el("div", {
          className: "pg-v2-service-url",
          children: [el("code", { text: a.sha256 || "no sha256" })],
        }),
        report
          ? el("div", { className: "pg-v2-service-url", text: report.title || report.report_type })
          : null,
        artifactLinks(a),
      ],
    });
  });
}
let evidenceRequest = 0;
let capturesRequest = 0;
function sessionJobId() {
  const session = getState().session;
  return session?.session_id || session?.job_id || session?.id;
}
async function loadEvidence(jobId, force = false) {
  const st = getState();
  if (!jobId || (!force && st.evidenceAttemptJobId === jobId)) return;
  const request = ++evidenceRequest;
  setState((prev) => ({
    ...prev,
    evidenceAttemptJobId: jobId,
    evidenceLoadingJobId: jobId,
    evidenceError: null,
  }));
  try {
    const [artifacts, reports] = await Promise.all([
      getArtifactsForJob(jobId),
      getReportsForJob(jobId),
    ]);
    if (request !== evidenceRequest || sessionJobId() !== jobId) return;
    setState((prev) => ({
      ...prev,
      evidenceJobId: jobId,
      evidenceLoadingJobId: null,
      artifacts,
      reports,
    }));
  } catch (err) {
    if (request !== evidenceRequest || sessionJobId() !== jobId) return;
    setState((prev) => ({ ...prev, evidenceLoadingJobId: null, evidenceError: err.message }));
  }
}
async function loadCaptures(vmId, force = false) {
  const st = getState();
  if (!vmId || (!force && st.capturesAttemptVmId === vmId)) return;
  const request = ++capturesRequest;
  setState((prev) => ({
    ...prev,
    capturesAttemptVmId: vmId,
    capturesLoadingVmId: vmId,
    capturesError: null,
  }));
  try {
    const captures = await listCaptures(vmId);
    if (request !== capturesRequest || getState().selectedId !== vmId) return;
    setState((prev) => ({
      ...prev,
      capturesVmId: vmId,
      capturesLoadingVmId: null,
      captures,
    }));
  } catch (err) {
    if (request !== capturesRequest || getState().selectedId !== vmId) return;
    setState((prev) => ({ ...prev, capturesLoadingVmId: null, capturesError: err.message }));
  } finally {
    // Selection can move to a network without starting another capture-list
    // request. Release this abandoned attempt on either outcome so returning
    // to its VM can fetch again; never clear a newer request's markers.
    if (request === capturesRequest && getState().selectedId !== vmId) {
      setState((prev) => ({
        ...prev,
        capturesAttemptVmId: null,
        capturesLoadingVmId: null,
      }));
    }
  }
}
function refreshEvidence(jobId) {
  if (sessionJobId() !== jobId) return;
  loadEvidence(jobId, true);
  const vm = getState().session?.vms?.find((v) => v.id === getState().selectedId);
  if (vm) loadCaptures(vm.id, true);
}
function credsText(service) {
  if (service.creds) return service.creds;
  if (!service.credentials) return "";
  return Object.entries(service.credentials)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}
function captureIsInFlight(st) {
  return Boolean(st.ui?.captureBusyVmId);
}
function releaseCapture(vmId) {
  setState((prev) =>
    prev.ui?.captureBusyVmId === vmId
      ? { ...prev, ui: { ...prev.ui, captureBusyVmId: null } }
      : prev,
  );
}
async function triggerCapture(vm, kind) {
  // POST /vms/{id}/captures mints a fresh acquisition_id per call and takes no
  // idempotency key, so a second click is a second dump of the same machine --
  // on a real host, another full-size image racing the first for the disk, and
  // published beside it as separate evidence. Rendering the control disabled is
  // not enough on its own: the click can still reach the node this handler was
  // bound to after the panel has re-rendered.
  if (captureIsInFlight(getState(), vm.id)) return;
  const jobId = sessionJobId();
  const stamp = { kind: kind === "memory" ? "RAM" : "DISK", vmId: vm.id, vmLabel: vm.label };
  setState((prev) => ({
    ...prev,
    ui: {
      ...prev.ui,
      captureBusyVmId: vm.id,
      stream: { ...stamp, lines: ["[00:00] requesting capture…"] },
      lastStream: stamp,
    },
  }));
  try {
    const r = await startCapture(vm.id, kind);
    const acqId = r?.acquisition_id || r?.id;
    if (!acqId) {
      releaseCapture(vm.id);
      return;
    }
    const es = openCaptureStream(acqId);
    const ownsStream = () => getState().ui?.stream?.acquisitionId === acqId;
    setState((prev) => ({
      ...prev,
      ui: { ...prev.ui, stream: { ...prev.ui.stream, acquisitionId: acqId } },
    }));
    const appendCaptureLine = (e) => {
      if (!ownsStream()) return;
      let line = e.data;
      try {
        const parsed = JSON.parse(e.data);
        const message = parsed.message || parsed.line || JSON.stringify(parsed);
        line = parsed.status ? `${parsed.status} ${message}` : message;
      } catch (_) {}
      setState((prev) => ({
        ...prev,
        ui: {
          ...prev.ui,
          stream: prev.ui.stream
            ? { ...prev.ui.stream, lines: [...prev.ui.stream.lines, line] }
            : prev.ui.stream,
        },
      }));
    };
    let terminalReached = false;
    let errorCount = 0;
    const terminalStatuses = new Set(["completed", "failed", "stream_timeout"]);
    es.onmessage = appendCaptureLine;
    for (const status of ["queued", "running", "completed", "failed", "stream_timeout"]) {
      es.addEventListener(status, (e) => {
        if (!ownsStream() || terminalReached) return;
        appendCaptureLine(e);
        if (terminalStatuses.has(status)) {
          if (terminalReached) return;
          terminalReached = true;
          es.close();
          releaseCapture(vm.id);
          refreshEvidence(jobId);
        }
      });
    }
    es.onerror = () => {
      if (!ownsStream()) {
        es.close();
        return;
      }
      errorCount += 1;
      const { action, lost } = captureStreamOnError({
        terminalReached,
        errorCount,
        maxErrors: MAX_STREAM_ERRORS,
      });
      if (action === "close") {
        if (lost) appendCaptureLine({ data: "[stream lost - check Evidence artifacts]" });
        es.close();
        releaseCapture(vm.id);
        if (lost) refreshEvidence(jobId);
      }
    };
  } catch (err) {
    setState((prev) => ({
      ...prev,
      ui: {
        ...prev.ui,
        captureBusyVmId: prev.ui?.captureBusyVmId === vm.id ? null : prev.ui?.captureBusyVmId,
        stream: prev.ui.stream
          ? { ...prev.ui.stream, lines: [...prev.ui.stream.lines, `[err] ${err.message}`] }
          : prev.ui.stream,
      },
    }));
  }
}
function serviceRow(s, vm) {
  const creds = credsText(s);
  const href = safeHref(s.url);
  return el("div", {
    className: "pg-v2-service-row",
    children: [
      el("div", {
        className: "pg-v2-service-main",
        children: [
          el("span", { className: "pg-v2-service-name", text: s.name }),
          el("span", {
            className: "pg-v2-service-status",
            text: s.status || vm.workload_state || "planned",
          }),
        ],
      }),
      s.url
        ? el("div", {
            className: "pg-v2-service-url",
            children: href
              ? [el("a", { attrs: { href, target: "_blank", rel: "noopener" }, text: s.url })]
              : [el("span", { text: s.url })],
          })
        : null,
      creds ? el("pre", { className: "pg-v2-service-creds", text: creds }) : null,
    ],
  });
}
async function triggerVmDelete(vm) {
  try {
    await deleteVm(vm.id);
    const session = await refreshSession();
    setState((prev) => ({
      ...prev,
      session,
      selectedId: session?.vms?.[0]?.id || session?.networks?.[0]?.id || null,
      errors: { ...prev.errors, vmDelete: undefined },
    }));
  } catch (err) {
    setState((prev) => ({ ...prev, errors: { ...prev.errors, vmDelete: err.message } }));
  }
}
function destroyVmButton(vm) {
  const disabled = vm.can_destroy === false;
  const tt = window.t || ((k) => k);
  return el("button", {
    className: "wiz-btn wiz-btn--danger",
    attrs: {
      type: "button",
      disabled: disabled ? "" : false,
      title: disabled ? vm.action_block_reason || "VM cannot be destroyed" : undefined,
      "data-detail-key": `destroy-${vm.id}`,
    },
    text: tt("pg_destroy_vm"),
    onClick: async () => {
      if (disabled) return;
      if (!(await window.confirmDialog(tt("pg_destroy_vm_confirm")))) return;
      triggerVmDelete(vm);
    },
  });
}
async function triggerVmAction(vm, action) {
  try {
    await vmAction(vm.id, action);
    const session = await refreshSession();
    setState((prev) => ({ ...prev, session, errors: { ...prev.errors, vmAction: undefined } }));
  } catch (err) {
    setState((prev) => ({ ...prev, errors: { ...prev.errors, vmAction: err.message } }));
  }
}
function vmActionButton(vm, action, label, enabled, tone = "ghost") {
  const disabled = enabled === false;
  const klass =
    tone === "primary"
      ? "wiz-btn wiz-btn--primary"
      : tone === "secondary"
        ? "wiz-btn wiz-btn--secondary"
        : "wiz-btn wiz-btn--ghost";
  return el("button", {
    className: klass,
    attrs: {
      type: "button",
      disabled: disabled ? "" : false,
      title: disabled ? vm.action_block_reason || `${label} unavailable` : undefined,
    },
    text: label,
    onClick: () => {
      if (!disabled) triggerVmAction(vm, action);
    },
  });
}

async function triggerNicAttach(vm, networkId) {
  if (!networkId) return;
  try {
    await attachNic(vm.id, networkId);
    const session = await refreshSession();
    setState((prev) => ({
      ...prev,
      session,
      selectedId: vm.id,
      errors: { ...prev.errors, nicAttach: undefined },
    }));
  } catch (err) {
    setState((prev) => ({ ...prev, errors: { ...prev.errors, nicAttach: err.message } }));
  }
}
async function triggerNicDetach(vm, nic) {
  try {
    await detachNic(vm.id, nic.id);
    const session = await refreshSession();
    setState((prev) => ({
      ...prev,
      session,
      selectedId: vm.id,
      errors: { ...prev.errors, nicDetach: undefined },
    }));
  } catch (err) {
    setState((prev) => ({ ...prev, errors: { ...prev.errors, nicDetach: err.message } }));
  }
}
function nicStatusText(nic) {
  if (nic.ip) return nic.ip;
  if (nic.diagnostic) return "missing IP";
  return "no lease";
}
function captureButton(vm, kind, label, tone) {
  const tt = window.t || ((k) => k);
  const unavailable = (kind === "memory" ? vm.can_capture_memory : vm.can_capture_disk) === false;
  // Both controls lock while one acquisition runs, not only the one that was
  // clicked: the page has a single capture console, so a second acquisition
  // started alongside the first would write its phases into the same log with
  // nothing to tell the two apart.
  //
  // aria-disabled rather than disabled, and only for this transient lock: a
  // disabled button leaves the tab order and takes the operator's focus with
  // it at the moment the capture starts, and its title goes unannounced. The
  // refusal itself lives in triggerCapture, which is also the only place that
  // can stop a click still bound to a node the panel has since re-rendered.
  const busy = captureIsInFlight(getState(), vm.id);
  return el("button", {
    className: `wiz-btn wiz-btn--${tone}`,
    attrs: {
      type: "button",
      disabled: unavailable ? "" : false,
      "aria-disabled": busy ? "true" : false,
      title: busy
        ? tt("pg_capture_in_flight")
        : unavailable
          ? vm.action_block_reason || "Capture unavailable"
          : undefined,
      "data-detail-key": `capture-${kind}-${vm.id}`,
    },
    text: label,
    onClick: () => {
      if (!unavailable) triggerCapture(vm, kind);
    },
  });
}
// Capture and power live in one bar directly under the machine name, because
// they are why the panel is open. Start and Stop swap rather than sitting side
// by side permanently greyed — only one of them is ever available.
function actionBar(vm, st) {
  const actionError = st.errors?.vmAction;
  const deleteError = st.errors?.vmDelete;
  const power = vm.can_start
    ? vmActionButton(vm, "start", "Start", vm.can_start, "primary")
    : vmActionButton(vm, "stop", "Stop", vm.can_stop);
  return el("div", {
    className: "pg-v2-actionbar",
    children: [
      el("div", {
        className: "pg-v2-actionbar-row",
        children: [
          captureButton(vm, "memory", "Capture RAM", "primary"),
          captureButton(vm, "disk", "Capture Disk", "secondary"),
          power,
          vmActionButton(vm, "reboot", "Reboot", vm.can_reboot),
          destroyVmButton(vm),
        ],
      }),
      vm.action_block_reason
        ? el("div", { className: "pg-v2-actionbar-note", text: vm.action_block_reason })
        : null,
      actionError ? el("div", { className: "pg-v2-warning", text: actionError }) : null,
      // A refused destroy leaves the VM on screen; without this the operator
      // sees a button that does nothing and no reason why.
      deleteError ? el("div", { className: "pg-v2-warning", text: deleteError }) : null,
    ].filter(Boolean),
  });
}
function sshSection(vm) {
  const ssh = vm.ssh || {
    user: vm.role === "attacker" ? "kali" : "ubuntu",
    pass: "key auth",
    port: 22,
  };
  const sshHost = vm.primary_ip || (vm.reachability_status === "ready" ? vm.ip : "");
  const sshCmd = sshHost
    ? `ssh ${ssh.user}@${sshHost}${ssh.port && ssh.port !== 22 ? ` -p ${ssh.port}` : ""}`
    : "waiting for guest IP";
  return section("Connect", [
    el("div", {
      className: "pg-v2-kv",
      children: [
        kvRow("user", ssh.user || "—"),
        kvRow("auth", ssh.pass || "key auth"),
        kvRow("port", String(ssh.port ?? 22)),
      ],
    }),
    el("div", {
      className: "pg-v2-ssh-box",
      children: [el("code", { text: sshCmd }), copyBtn(sshCmd)],
    }),
  ]);
}
// Artifacts are listed per session, so scope them to the selected machine by way
// of its acquisitions — otherwise every VM's panel shows every capture in the lab.
function artifactsForVm(st, vm) {
  const acquisitionIds = new Set(
    (st.capturesVmId === vm.id ? st.captures || [] : []).map((c) => c.id),
  );
  return (st.artifacts || []).filter(
    (a) => a.acquisition_id && acquisitionIds.has(a.acquisition_id),
  );
}
function evidenceSection(st, jobId, vm) {
  const loading = st.evidenceLoadingJobId === jobId || st.capturesLoadingVmId === vm.id;
  const error = st.evidenceError || st.capturesError;
  if (error)
    return section("Evidence · this machine", [
      el("div", { className: "pg-v2-warning", attrs: { role: "status" }, text: error }),
      el("button", {
        className: "wiz-btn wiz-btn--secondary",
        attrs: { type: "button", disabled: loading ? "" : false },
        text: "Retry evidence",
        dataset: { cleanupReadonly: "true" },
        onClick: () => refreshEvidence(jobId),
      }),
    ]);
  if (st.evidenceJobId === jobId && st.capturesVmId === vm.id) {
    const rows = evidenceRows(artifactsForVm(st, vm), st.reports);
    if (rows.length) return section("Evidence · this machine", rows);
    if (!loading)
      return section("Evidence · this machine", [
        el("div", { className: "text-muted small", text: "no captures taken yet" }),
      ]);
  }
  if (loading)
    return section("Evidence · this machine", [
      el("div", { className: "text-muted small", text: "loading…" }),
    ]);
  return null;
}
function networkInterfacesSection(vm, st) {
  const nics = vm.nics || [];
  const attached = new Set(nics.map((n) => n.network_id || n.job_network_id));
  const activeSession = st.session?.status === "running";
  const vmReady = vm.state === "running";
  const attachOptions = (st.session?.networks || []).filter(
    (net) => net.state === "active" && !attached.has(net.id),
  );
  const canAttach = activeSession && vmReady && attachOptions.length > 0;
  const attachSelect = el("select", {
    className: "pg-v2-nic-select",
    attrs: { disabled: canAttach ? false : "", "aria-label": "Attach network" },
    children: attachOptions.length
      ? attachOptions.map((net) =>
          el("option", {
            attrs: { value: net.id },
            text: `${net.name} · ${net.subnet_cidr}`,
          }),
        )
      : [el("option", { attrs: { value: "" }, text: "No active networks available" })],
  });
  const attachBlockReason = !activeSession
    ? "session not active"
    : !vmReady
      ? `VM not running (state=${vm.state})`
      : attachOptions.length === 0
        ? "no unattached active networks"
        : null;
  return section("Network interfaces", [
    el("div", {
      className: "pg-v2-nics",
      children: nics.map((nic) => {
        const canDetach = activeSession && vmReady && nics.length > 1 && nic.state !== "pending";
        const title = canDetach
          ? undefined
          : nics.length <= 1
            ? "VM must keep at least one NIC"
            : attachBlockReason || "NIC cannot be detached";
        return el("div", {
          className: "pg-v2-nic-row",
          dataset: { state: nic.ip ? "ready" : "missing" },
          children: [
            el("div", {
              className: "pg-v2-nic-main",
              children: [
                el("span", {
                  className: "pg-v2-nic-name",
                  text: nic.network_name || nic.network_id || "—",
                }),
                el("span", { className: "pg-v2-nic-status", text: nic.state || "active" }),
              ],
            }),
            el("div", {
              className: "pg-v2-nic-meta",
              text: `${nic.mac || "—"} · ${nicStatusText(nic)}`,
            }),
            nic.diagnostic
              ? el("div", { className: "pg-v2-nic-diag", text: nic.diagnostic })
              : null,
            el("button", {
              className: "wiz-btn wiz-btn--ghost",
              attrs: {
                type: "button",
                disabled: canDetach ? false : "",
                title,
                "data-detail-key": `detach-${nic.id}`,
              },
              text: "Detach",
              onClick: () => {
                if (canDetach) triggerNicDetach(vm, nic);
              },
            }),
          ],
        });
      }),
    }),
    el("div", {
      className: "pg-v2-nic-attach",
      children: [
        attachSelect,
        el("button", {
          className: "wiz-btn wiz-btn--secondary",
          attrs: {
            type: "button",
            disabled: canAttach ? false : "",
            title: attachBlockReason || undefined,
          },
          text: "Attach network",
          onClick: () => {
            if (canAttach) triggerNicAttach(vm, attachSelect.value);
          },
        }),
      ],
    }),
    st.errors?.nicAttach
      ? el("div", { className: "pg-v2-warning", text: st.errors.nicAttach })
      : null,
    st.errors?.nicDetach
      ? el("div", { className: "pg-v2-warning", text: st.errors.nicDetach })
      : null,
  ]);
}
async function triggerNetworkDelete(net) {
  try {
    await deleteNetwork(net.id);
    const session = await refreshSession();
    setState((prev) => ({ ...prev, session, selectedId: session?.vms?.[0]?.id || null }));
  } catch (err) {
    setState((prev) => ({ ...prev, errors: { ...prev.errors, networkDelete: err.message } }));
  }
}
function renderNetwork(root, net) {
  const tt = window.t || ((k) => k);
  const body = el("div", { className: "pg-v2-detail-body" });
  const count = Number(net.attached_vm_count || 0);
  const disabled = net.can_delete === false;
  const st = getState();
  const blockReason = disabled ? net.action_block_reason || tt("pg_net_undeletable") : "";
  const networkError = st.errors?.networkDelete;
  const head = el("div", {
    className: "pg-v2-detail-head",
    children: [
      el("div", {
        className: "pg-v2-detail-title",
        children: [
          el("span", { className: "dot", style: { background: "var(--c-purple)" } }),
          el("span", { text: net.name }),
        ],
      }),
      el("span", {
        className: "pg-pill-role",
        style: { color: "var(--c-purple)", "border-color": "var(--c-purple)" },
        text: "network",
      }),
    ],
  });
  body.appendChild(
    section("Network Details", [
      el("div", {
        className: "pg-v2-kv",
        children: [
          kvRow(tt("pg_kv_name"), net.name),
          kvRow(tt("pg_kv_cidr"), net.subnet_cidr || "—"),
          kvRow(
            tt("pg_kv_mode"),
            net.isolated === false ? tt("pg_mode_shared") : tt("pg_mode_isolated"),
          ),
          kvRow("backend", "libvirt"),
          kvRow(tt("pg_kv_state"), net.state || tt("pg_state_unknown")),
          kvRow("vms", `${count} ${count === 1 ? "VM" : "VMs"}`),
          kvRow("attached", `${count} attached VM(s)`),
        ],
      }),
    ]),
  );
  body.appendChild(
    section("Actions", [
      el("button", {
        className: "wiz-btn wiz-btn--danger",
        attrs: { type: "button", disabled: disabled ? "" : false, title: blockReason || undefined },
        text: "Delete Network",
        onClick: () => {
          if (!disabled) triggerNetworkDelete(net);
        },
      }),
      blockReason
        ? el("div", { className: "pg-v2-warning", text: `Cannot delete: ${blockReason}` })
        : null,
      networkError ? el("div", { className: "pg-v2-warning", text: networkError }) : null,
    ]),
  );
  replaceDetails(root, net.id, head, body);
}
function inventoryDisclosure(vm, st) {
  const parts = [networkInterfacesSection(vm, st)];
  if (vm.workload_id)
    parts.push(
      section("Workload", [
        el("div", {
          className: "pg-v2-kv",
          children: [
            kvRow("name", vm.workload_display_name || vm.workload_id),
            kvRow("id", vm.workload_id, "al-hash"),
            kvRow("state", vm.workload_state || "planned"),
          ],
        }),
      ]),
    );
  if (vm.storage)
    parts.push(
      section("Storage", [
        el("div", {
          className: "pg-v2-kv",
          children: [
            kvRow("host", vm.storage.host || "—"),
            kvRow("global", vm.storage.global || "—"),
          ],
        }),
      ]),
    );
  const names = ["Network interfaces"];
  if (vm.workload_id) names.push("Workload");
  if (vm.storage) names.push("Storage");
  const summary = el("summary", {
    className: "pg-v2-disclosure-summary",
    text: names.join(" · "),
  });
  const details = el("details", {
    className: "pg-v2-disclosure",
    children: [summary, el("div", { className: "pg-v2-disclosure-body", children: parts })],
  });
  // A NIC without a lease is a reason to open the panel, not to hide it.
  if ((vm.nics || []).some((nic) => !nic.ip && nic.diagnostic)) details.open = true;
  return details;
}
// Preserve unfinished form state only for the same machine/network. New DOM
// keeps current capability checks and event handlers; focus returns to the
// equivalent enabled control after polling instead of falling to BODY.
function replaceDetails(root, selectedId, ...children) {
  const sameSelection = root.dataset.selectedId === selectedId;
  const controls = "button,input,select,textarea,summary,a[href]";
  const key = (node) =>
    node.id ||
    node.dataset.detailKey ||
    `${node.tagName}:${node.getAttribute("aria-label") || node.getAttribute("href") || node.textContent.trim()}`;
  const focused =
    sameSelection && root.contains(document.activeElement) ? key(document.activeElement) : null;
  const oldSelect = sameSelection && root.querySelector(".pg-v2-nic-select");
  const selectedNetwork = oldSelect ? oldSelect.value : null;
  const disclosure = sameSelection && root.querySelector(".pg-v2-disclosure");
  const open = disclosure ? disclosure.open : null;
  const scrollTop = root.querySelector(".pg-v2-detail-body")?.scrollTop || 0;
  root.replaceChildren(...children);
  root.dataset.selectedId = selectedId;
  root.dataset.focusScope = selectedId;
  const nextSelect = root.querySelector(".pg-v2-nic-select");
  if (
    nextSelect &&
    selectedNetwork !== null &&
    [...nextSelect.options].some((o) => o.value === selectedNetwork)
  )
    nextSelect.value = selectedNetwork;
  const nextDisclosure = root.querySelector(".pg-v2-disclosure");
  if (nextDisclosure && open !== null) nextDisclosure.open = open;
  if (focused) {
    const next = [...root.querySelectorAll(controls)].find(
      (node) => key(node) === focused && !node.disabled,
    );
    if (next) next.focus({ preventScroll: true });
    else {
      root.tabIndex = -1;
      root.focus({ preventScroll: true });
    }
  }
  const body = root.querySelector(".pg-v2-detail-body");
  if (body && sameSelection) body.scrollTop = scrollTop;
}
function render() {
  const root = document.getElementById("pg-detail-panel");
  if (!root) return;
  const st = getState();
  const jobId = st.session?.session_id || st.session?.job_id || st.session?.id;
  if (jobId) loadEvidence(jobId);
  const vm = (st.session?.vms || []).find((v) => v.id === st.selectedId);
  if (vm) loadCaptures(vm.id);
  if (!vm) {
    const net = (st.session?.networks || []).find((n) => n.id === st.selectedId);
    if (net) renderNetwork(root, net);
    else replaceDetails(root, st.selectedId || "");
    return;
  }
  const accent = roleAccent(vm.role);
  const body = el("div", { className: "pg-v2-detail-body" });
  const cpu = vm.cpu_pct ?? vm.cpu ?? 0,
    mem = vm.mem_pct ?? vm.mem ?? 0;
  const head = el("div", {
    className: "pg-v2-detail-head",
    children: [
      el("div", {
        className: "pg-v2-detail-title",
        children: [
          el("span", { className: "dot", style: { background: accent } }),
          el("span", { text: vm.label }),
        ],
      }),
      el("span", {
        className: "pg-pill-role",
        style: { color: accent, "border-color": accent },
        text: vm.role,
      }),
    ],
  });
  // A failed VM now stays on the board, so its reason is the first thing the
  // panel says rather than a note buried under the machine's vitals.
  if (vm.error_summary)
    body.appendChild(
      section("VM warning", [el("div", { className: "pg-v2-warning", text: vm.error_summary })]),
    );
  body.appendChild(sshSection(vm));
  body.appendChild(
    section("Machine", [
      el("div", {
        className: "pg-v2-kv",
        children: [
          kvRow("status", phaseLabel(vm)),
          kvRow("os", vm.os || "—"),
          kvRow("ip", vm.ip || "—"),
          kvRow("net", vm.net || vm.nics?.[0]?.network_name || "—"),
          kvRow("up", vm.uptime || "—"),
        ],
      }),
      barLine("cpu", cpu, cpuColor(cpu)),
      barLine("mem", mem, "var(--c-info)"),
    ]),
  );
  const services = vm.services || [];
  if (services.length)
    body.appendChild(
      section("Services", [
        el("div", {
          className: "pg-v2-services",
          children: services.map((s) => serviceRow(s, vm)),
        }),
      ]),
    );
  const evidence = evidenceSection(st, jobId, vm);
  if (evidence) body.appendChild(evidence);
  body.appendChild(inventoryDisclosure(vm, st));
  replaceDetails(root, vm.id, head, actionBar(vm, st), body);
}
export function mountDetails() {
  subscribe(
    (s) => ({
      vms: s.session?.vms,
      networks: s.session?.networks,
      selectedId: s.selectedId,
      evidenceJobId: s.evidenceJobId,
      evidenceLoadingJobId: s.evidenceLoadingJobId,
      evidenceError: s.evidenceError,
      captures: s.captures,
      capturesVmId: s.capturesVmId,
      capturesLoadingVmId: s.capturesLoadingVmId,
      capturesError: s.capturesError,
      vmActionError: s.errors?.vmAction,
      vmDeleteError: s.errors?.vmDelete,
      networkDeleteError: s.errors?.networkDelete,
      artifacts: s.artifacts,
      reports: s.reports,
      nicAttachError: s.errors?.nicAttach,
      nicDetachError: s.errors?.nicDetach,
      captureBusyVmId: s.ui?.captureBusyVmId,
      cleanup: s.cleanup,
    }),
    () => {
      render();
      if (getState().cleanup)
        document
          .querySelectorAll(
            "#pg-detail-panel button:not([data-cleanup-readonly]), #pg-detail-panel input, #pg-detail-panel select",
          )
          .forEach((control) => {
            control.disabled = true;
          });
    },
  );
}
