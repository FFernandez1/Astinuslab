// REST client for the local portable-lab workflow.
// Keep this module deliberately boring: every state-changing request carries
// the same CSRF header as the Playground client and all errors retain the
// server's structured detail for the UI to explain.
import { csrfHeaders } from "../csrf.js";

const BASE = "/api/v1/lab-packages";

function formatDetail(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        const loc = Array.isArray(item?.loc)
          ? item.loc.filter((part) => part !== "body").join(".")
          : "";
        const message = item?.msg || JSON.stringify(item);
        return loc ? `${loc}: ${message}` : message;
      })
      .join("; ");
  }
  if (typeof detail === "object" && typeof detail.message === "string") return detail.message;
  return JSON.stringify(detail);
}

async function parseResponse(response) {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (_) {
    return text;
  }
}

async function request(method, path, body, { signal, base = BASE } = {}) {
  const response = await fetch(base + path, {
    method,
    ...(signal === undefined ? {} : { signal }),
    headers: csrfHeaders(method, body === undefined ? {} : { "Content-Type": "application/json" }),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await parseResponse(response);
  if (!response.ok) {
    const error = new Error(
      formatDetail(data?.detail) || (typeof data === "string" ? data : response.statusText),
    );
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

export async function inspectPackage(file) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${BASE}/inspect`, {
    method: "POST",
    headers: csrfHeaders("POST"),
    body: form,
  });
  const data = await parseResponse(response);
  if (!response.ok) {
    const error = new Error(formatDetail(data?.detail) || response.statusText);
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

export function commitInspection(inspectionId) {
  return request("POST", `/imports/${encodeURIComponent(inspectionId)}/commit`);
}

export function listPackages() {
  return request("GET", "");
}

export function getPackageExperience(recordId) {
  return request("GET", `/${encodeURIComponent(recordId)}/experience`);
}

export function getCurrentPlaygroundSession() {
  return request("GET", "/sessions/current-or-null", undefined, { base: "/api/v1/playground" });
}

export function listActivity(options) {
  return request("GET", "/activity", undefined, options);
}

export function deletePackage(recordId) {
  return request("DELETE", `/${encodeURIComponent(recordId)}`);
}

export function preparePackage(recordId, payload) {
  return request("POST", `/${encodeURIComponent(recordId)}/prepare`, payload);
}

export function getPrepareStatus(attemptId, options) {
  return request("GET", `/prepare/${encodeURIComponent(attemptId)}`, undefined, options);
}

export function cancelPrepare(attemptId) {
  return request("POST", `/prepare/${encodeURIComponent(attemptId)}/cancel`);
}

export function preflightPackage(recordId) {
  return request("POST", `/${encodeURIComponent(recordId)}/preflight`);
}

export function launchPackage(recordId, payload) {
  return request("POST", `/${encodeURIComponent(recordId)}/launch`, payload);
}

export function getOperationStatus(instanceId, options) {
  return request("GET", `/operations/${encodeURIComponent(instanceId)}`, undefined, options);
}

export function instanceAction(instanceId, action) {
  return request(
    "POST",
    `/operations/${encodeURIComponent(instanceId)}/${encodeURIComponent(action)}`,
  );
}

export function saveDraft(payload) {
  return request("POST", "/drafts", payload);
}

export function verifyCleanRun(payload) {
  return request("POST", "/verify-clean-run", payload);
}

export function getVerificationStatus(attemptId, options) {
  return request("GET", `/verifications/${encodeURIComponent(attemptId)}`, undefined, options);
}

// Archives are bytes, not JSON: keep the structured-error contract but return
// the blob and the server's filename so the caller can save it locally.
export async function exportVerification(attemptId, payload) {
  const response = await fetch(`${BASE}/verifications/${encodeURIComponent(attemptId)}/export`, {
    method: "POST",
    headers: csrfHeaders("POST", { "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await parseResponse(response);
    const error = new Error(
      formatDetail(data?.detail) || (typeof data === "string" ? data : response.statusText),
    );
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  const disposition = response.headers?.get?.("content-disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  return { blob: await response.blob(), filename: match?.[1] || `${attemptId}.astinuslab` };
}

export function cancelVerification(attemptId) {
  return request("POST", `/verifications/${encodeURIComponent(attemptId)}/cancel`);
}

export function definitionFromSession(metadata) {
  return request("POST", "/definitions/from-session", metadata);
}
