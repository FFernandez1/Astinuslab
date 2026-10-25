import test from "node:test";
import assert from "node:assert/strict";
import { generatedPlaygroundName } from "../../../../src/astinuslab/web/static/playground/name-generator.js";

test("generated labels are compact and retain the role and a random suffix", () => {
  for (const [role, prefix] of [
    ["target", "tgt"],
    ["attacker", "atk"],
    ["network", "net"],
    ["unknown", "tgt"],
  ]) {
    const name = generatedPlaygroundName(role);
    assert.match(name, new RegExp(`^${prefix}-[a-z]+-[0-9a-f]{4}$`));
    assert.ok(name.length <= 16, name);
  }
});
