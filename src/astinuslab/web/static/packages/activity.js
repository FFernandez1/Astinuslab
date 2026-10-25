// The API already orders each collection by updated_at DESC / id ASC.
// Preserve that ordering when choosing the single visible result in each slot.
export function selectActivity({ prepares, instances, verifications }) {
  return {
    prepares: Object.fromEntries(prepares.map((record) => [record.id, record])),
    operations: Object.fromEntries(instances.map((record) => [record.id, record])),
    verifications: Object.fromEntries(verifications.map((record) => [record.id, record])),
    prepare: prepares[0] || null,
    operation: instances[0] || null,
    verification:
      verifications.find((record) => ["reserved", "running"].includes(record.state)) ||
      verifications.find((record) => ["failed", "cancelled"].includes(record.state)) ||
      verifications[0] ||
      null,
  };
}
