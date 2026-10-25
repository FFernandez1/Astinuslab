// Which VMs the full tier must destroy when it is done.
//
// The tier boots a REAL libvirt VM, so getting this wrong costs a running
// machine and its disk on the developer's host. It got it wrong: the spec
// recorded the ids it created *after* a 60-second readiness poll, and that poll
// timing out is the most likely failure on a slow host. The throw skipped the
// assignment, `afterAll` iterated an empty array, and the VM stayed up.
//
// The fix is to stop relying on the test body having recorded anything.
// Teardown is a diff: whatever is in the session now that was not there when
// the tier started is this tier's to remove, whether or not the test lived long
// enough to write the id down.

/**
 * @param {object} args
 * @param {Iterable<string>} args.preexistingIds VM ids present before the tier ran.
 * @param {Array<{id: string}>} args.currentVms  VMs in the session now.
 * @param {Iterable<string>} [args.recordedIds]  Ids the test body managed to record.
 * @returns {string[]} ids to destroy, in a stable order, without duplicates.
 */
function vmsToTearDown({ preexistingIds, currentVms, recordedIds = [] }) {
  const preexisting = new Set(preexistingIds);
  const ordered = [];
  const seen = new Set();
  const add = (id) => {
    if (typeof id !== "string" || !id || seen.has(id) || preexisting.has(id)) return;
    seen.add(id);
    ordered.push(id);
  };
  // The diff first: it is the part that survives a test that died early.
  for (const vm of currentVms || []) add(vm?.id);
  // Then anything the body recorded. A VM that has already gone from the
  // session answers 404 and the caller ignores it, which is cheaper than
  // leaving a machine up because it briefly dropped out of the listing.
  for (const id of recordedIds) add(id);
  return ordered;
}

module.exports = { vmsToTearDown };
