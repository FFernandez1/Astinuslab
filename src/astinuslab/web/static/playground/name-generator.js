const NOUNS = [
  "beacon",
  "circuit",
  "comet",
  "cookie",
  "kernel",
  "orbit",
  "packet",
  "portal",
  "signal",
  "socket",
  "widget",
  "wizard",
];

const VALID_PREFIXES = new Set(["target", "attacker", "network"]);

function randomIndex(max) {
  if (globalThis.crypto?.getRandomValues) {
    const values = new Uint32Array(1);
    globalThis.crypto.getRandomValues(values);
    return values[0] % max;
  }
  return Math.floor(Math.random() * max);
}

function randomSuffix() {
  return randomIndex(65536).toString(16).padStart(4, "0");
}

export function generatedPlaygroundName(prefix) {
  const role = VALID_PREFIXES.has(prefix) ? prefix : "target";
  const safePrefix = { target: "tgt", attacker: "atk", network: "net" }[role];
  const noun = NOUNS[randomIndex(NOUNS.length)];
  return `${safePrefix}-${noun}-${randomSuffix()}`;
}
