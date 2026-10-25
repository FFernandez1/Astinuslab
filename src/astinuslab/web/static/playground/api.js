// API wrappers for the playground page. All fetches go through here so
// errors can be surfaced uniformly and modules consume one stable DTO shape.
import { csrfHeaders } from "../csrf.js";
import { isVmGone } from "./vm-state.js";
import { getState } from "./state.js";

const BASE = "/api/v1/playground";

// FastAPI returns `detail` as a string for HTTPException but as a list of
// {loc, msg, type, input} objects for request validation errors (422). Flatten
// the list form into a readable string so callers see the real message instead
// of "[object Object]".
function formatErrorDetail(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (typeof d === "string") return d;
        const loc = Array.isArray(d?.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        const msg = d?.msg || JSON.stringify(d);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

async function request(method, path, body, signal) {
  return requestUrl(method, BASE + path, body, signal);
}

async function requestUrl(method, url, body, signal) {
  const cleanup = getState().cleanup;
  const ownsDelete =
    method === "DELETE" &&
    cleanup?.phase === "requesting" &&
    url === `${BASE}/sessions/${encodeURIComponent(cleanup.session_id)}`;
  if (method !== "GET" && cleanup && !ownsDelete)
    throw new Error(
      globalThis.window?.t?.("pg_cleanup_readonly") ||
        "Cleanup is selected. Review its status before changing the lab.",
    );
  const res = await fetch(url, {
    method,
    headers: csrfHeaders(method, body ? { "Content-Type": "application/json" } : {}),
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = new Error(formatErrorDetail(data?.detail) || res.statusText);
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  return data;
}

function byId(items) {
  return Object.fromEntries((items || []).map((x) => [x.id, x]));
}
function nicReachability(nics) {
  const rows = nics || [];
  if (!rows.length) return { ip: "—", primaryIp: null, status: "none" };
  const ips = rows.filter((n) => n.ip);
  const missing = rows.filter((n) => !n.ip);
  if (ips.length && missing.length)
    return { ip: "partial", primaryIp: ips[0].ip, status: "partial" };
  if (ips.length) return { ip: ips[0].ip, primaryIp: ips[0].ip, status: "ready" };
  return { ip: "—", primaryIp: null, status: "missing_ip" };
}
function formatCreds(credentials) {
  if (!credentials) return "";
  if (typeof credentials === "string") return credentials;
  return Object.entries(credentials)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

export function normalizeSession(raw) {
  if (!raw || !Array.isArray(raw.vms) || !Array.isArray(raw.networks)) return raw;
  if (["destroy_requested", "destroying"].includes(raw.status)) {
    return { ...raw, id: raw.session_id || raw.job_id || raw.id, vms: [], networks: [] };
  }
  const nets = byId(raw.networks);
  // Only genuinely removed machines are dropped. A failed or teardown_failed VM
  // stays on the board: it is the sole evidence the operator has that a machine
  // was requested and broke, and teardown_failed may mean leaked host resources.
  const visibleVms = raw.vms.filter((vm) => !isVmGone(vm.state));
  const vms = visibleVms.map((vm) => {
    const nics = (vm.nics || []).map((nic) => {
      const networkId = nic.network_id || nic.job_network_id;
      const net = nets[networkId];
      return {
        ...nic,
        network_id: networkId,
        network_name: nic.network_name || net?.name || networkId || "—",
        network_state: nic.network_state || net?.state || "—",
        network_isolated: nic.network_isolated ?? net?.isolated ?? null,
        state: nic.state || "active",
      };
    });
    const services = (vm.services || []).map((s) => ({
      ...s,
      creds: s.creds || formatCreds(s.credentials),
      status: s.status || vm.workload_state || "planned",
    }));
    const sshUser =
      vm.ssh?.user || vm.default_username || (vm.role === "attacker" ? "kali" : "ubuntu");
    const reachability = nicReachability(nics);
    return {
      ...vm,
      nics,
      services,
      ip: vm.ip || reachability.ip,
      primary_ip: vm.primary_ip || reachability.primaryIp,
      reachability_status: vm.reachability_status || reachability.status,
      net:
        vm.net ||
        nics
          .map((n) => n.network_name)
          .filter(Boolean)
          .join(", ") ||
        "—",
      os: vm.os || vm.image_id || "—",
      uptime: vm.uptime || "—",
      ssh: vm.ssh || { user: sshUser, pass: vm.ssh_password || "key auth", port: 22 },
      storage: vm.storage || { host: "—", global: raw.storage?.global_label || "—" },
    };
  });
  return { ...raw, id: raw.session_id || raw.job_id || raw.id, vms, networks: raw.networks };
}

export async function refreshSession({ signal } = {}) {
  return normalizeSession(await request("GET", "/sessions/current-or-null", undefined, signal));
}
export async function getSession(options) {
  return refreshSession(options);
}
export async function getRecentFailure({ signal } = {}) {
  return request("GET", "/sessions/recent-failure", undefined, signal);
}

export async function getCleanupStatus(sessionId, { signal } = {}) {
  return request(
    "GET",
    `/sessions/${encodeURIComponent(sessionId)}/cleanup-status`,
    undefined,
    signal,
  );
}
async function requestAllPages(url) {
  const pageSize = 500;
  const rows = [];
  for (let offset = 0; ; offset += pageSize) {
    const separator = url.includes("?") ? "&" : "?";
    const page = await requestUrl("GET", `${url}${separator}limit=${pageSize}&offset=${offset}`);
    rows.push(...page);
    if (page.length < pageSize) return rows;
  }
}
export async function getArtifactsForJob(jobId) {
  return requestAllPages(`/api/v1/artifacts?job_id=${encodeURIComponent(jobId)}`);
}
export async function getReportsForJob(jobId) {
  return requestAllPages(`/api/v1/reports?job_id=${encodeURIComponent(jobId)}`);
}
export async function getSessionMetrics({ signal } = {}) {
  return request("GET", "/sessions/current/metrics", undefined, signal);
}
export async function getCatalogImages(role) {
  return request("GET", `/catalog/images?role=${encodeURIComponent(role)}`);
}
export async function getCatalogWorkloads(imageId) {
  return request("GET", `/catalog/workloads?image_id=${encodeURIComponent(imageId)}`);
}
export async function createVm({
  label,
  role,
  image_id,
  disk_gb,
  vcpus,
  memory_mb,
  network_ids,
  new_networks,
  workload_id,
}) {
  return request("POST", "/vms", {
    label,
    role,
    image_id,
    disk_gb,
    vcpus,
    memory_mb,
    network_ids,
    new_networks,
    workload_id,
  });
}
export async function createNetwork({ name, subnet_cidr, isolated }) {
  return request("POST", "/networks", { name, subnet_cidr, isolated });
}
export async function suggestCidr({ jobId, extra } = {}) {
  const params = new URLSearchParams();
  if (jobId) params.set("job_id", jobId);
  (extra || []).forEach((c) => params.append("extra", c));
  const qs = params.toString();
  return request("GET", `/networks/suggest-cidr${qs ? `?${qs}` : ""}`);
}
export async function attachNic(vmId, networkId) {
  return request("POST", `/sessions/current/vms/${encodeURIComponent(vmId)}/nics`, {
    network_id: networkId,
  });
}
export async function detachNic(vmId, nicId) {
  return request(
    "DELETE",
    `/sessions/current/vms/${encodeURIComponent(vmId)}/nics/${encodeURIComponent(nicId)}`,
  );
}
export async function deleteNetwork(networkId) {
  return request("DELETE", `/networks/${encodeURIComponent(networkId)}`);
}
export async function deleteVm(vmId) {
  return request("DELETE", `/vms/${encodeURIComponent(vmId)}`);
}
export async function getTopologyLayout() {
  return request("GET", "/sessions/current/topology-layout");
}
export async function saveTopologyNode(node) {
  return request(
    "PATCH",
    `/sessions/current/topology-layout/${encodeURIComponent(node.node_id)}`,
    node,
  );
}
export async function exportRecipe() {
  return request("GET", "/sessions/current/recipe");
}
export async function exportLabPackage(payload) {
  if (getState().cleanup)
    throw new Error(
      globalThis.window?.t?.("pg_cleanup_readonly") ||
        "Cleanup is selected. Review its status before changing the lab.",
    );
  const res = await fetch("/api/v1/lab-packages/export", {
    method: "POST",
    headers: csrfHeaders("POST", { "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      // Keep the raw status text below when the server response is not JSON.
    }
    const err = new Error(formatErrorDetail(data?.detail) || res.statusText);
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/i);
  return { blob: await res.blob(), filename: match?.[1] || `${payload.package_id}.astinuslab` };
}
export async function importRecipe(recipe) {
  return request("POST", "/recipes/import", recipe);
}
export async function vmAction(vmId, action) {
  return request("POST", `/vms/${encodeURIComponent(vmId)}/actions`, { action });
}
export async function startCapture(vmId, kind) {
  return request("POST", `/vms/${encodeURIComponent(vmId)}/captures`, { kind });
}
// Acquisitions belonging to one VM. Artifacts are only listed per session, so
// this is what scopes the panel's evidence to the selected machine instead of
// showing every capture taken anywhere in the lab.
export async function listCaptures(vmId) {
  return request("GET", `/vms/${encodeURIComponent(vmId)}/captures`);
}
export async function endSession(sessionId) {
  return request("DELETE", `/sessions/${encodeURIComponent(sessionId)}`);
}

// Returns an EventSource for live capture progress. Caller is responsible for closing it.
export function openCaptureStream(acquisitionId) {
  return new EventSource(`${BASE}/captures/${encodeURIComponent(acquisitionId)}/events`);
}
