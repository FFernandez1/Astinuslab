// One classification of the backend VM state vocabulary, shared by every module
// that needs to reason about a VM's lifecycle rather than render its label.
//
// The backend emits ~18 distinct states (see playground/repositories.py). Modules
// used to each carry a partial hardcoded list, which is how "0 failed" could be
// printed next to a failed VM. Add new backend states here, once.
export const VM_LIFECYCLE = {
  STARTING: "starting",
  RUNNING: "running",
  STOPPED: "stopped",
  FAILED: "failed",
  GONE: "gone",
  OTHER: "other",
};

const BY_STATE = {
  queued: VM_LIFECYCLE.STARTING,
  pending: VM_LIFECYCLE.STARTING,
  creating: VM_LIFECYCLE.STARTING,
  starting: VM_LIFECYCLE.STARTING,
  booting: VM_LIFECYCLE.STARTING,
  provisioning_workload: VM_LIFECYCLE.STARTING,
  provisioning_workload_active: VM_LIFECYCLE.STARTING,
  powering_on: VM_LIFECYCLE.STARTING,
  rebooting: VM_LIFECYCLE.STARTING,

  running: VM_LIFECYCLE.RUNNING,

  stopping: VM_LIFECYCLE.STOPPED,
  powering_off: VM_LIFECYCLE.STOPPED,
  stopped: VM_LIFECYCLE.STOPPED,
  shutoff: VM_LIFECYCLE.STOPPED,
  shutdown: VM_LIFECYCLE.STOPPED,

  // Wreckage, not absence: the operator asked for this machine and it broke.
  // teardown_failed additionally means host resources may still be leaked.
  failed: VM_LIFECYCLE.FAILED,
  teardown_failed: VM_LIFECYCLE.FAILED,

  destroy_queued: VM_LIFECYCLE.GONE,
  destroying: VM_LIFECYCLE.GONE,
  destroyed: VM_LIFECYCLE.GONE,
};

export function vmLifecycle(state) {
  return BY_STATE[state] || VM_LIFECYCLE.OTHER;
}

// True for states the operator should no longer see on the board. Deliberately
// narrow: anything unrecognised stays visible rather than vanishing silently.
export function isVmGone(state) {
  return vmLifecycle(state) === VM_LIFECYCLE.GONE;
}
