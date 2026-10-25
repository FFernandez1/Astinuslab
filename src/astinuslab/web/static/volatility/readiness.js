// Fail-closed capability state parsed from the server-rendered workbench root.
function readyAttribute(name) {
  var root = document.getElementById("vol-workbench");
  return !!root && root.getAttribute(name) === "1";
}

export function analysisReady() {
  return readyAttribute("data-analysis-ready");
}

export function isfBuildReady() {
  return readyAttribute("data-isf-build-ready");
}

// Whether the Volatility integration is switched on at all.  This is the only
// capability that blocks queueing an ISF build: an unready dwarf2json still leaves
// the prebuilt and debuginfod symbol sources usable.
export function vol3Enabled() {
  return readyAttribute("data-vol3-enabled");
}
