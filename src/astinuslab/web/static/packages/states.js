// Durable state vocabulary shared by package polling and rendering.
// Keep these sets aligned with PACKAGE_INSTANCE_STATES and the verification /
// preparation state constraints in src/astinuslab/db/models.py.
export const INSTANCE_TERMINAL = new Set(["failed", "destroyed"]);
export const VERIFICATION_TERMINAL = new Set(["verified", "failed", "cancelled"]);
export const PREPARE_TERMINAL = new Set(["completed", "failed", "cancelled"]);

export const INSTANCE_CLEANUP_INCOMPLETE = "cleanup_incomplete";
export const INSTANCE_POLL_STOP = new Set([
  "ready",
  ...INSTANCE_TERMINAL,
  INSTANCE_CLEANUP_INCOMPLETE,
]);
