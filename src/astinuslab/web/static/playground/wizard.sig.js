// Stable signature of the network facts the create-VM modal consumes.
// Used so the wizard subscription does not re-render on every session poll
// when the polled session object has a fresh identity but unchanged networks.
export function networkSignature(session) {
  return (session?.networks || []).map((n) => `${n.id}:${n.state}:${n.isolated ? 1 : 0}`).join("|");
}
