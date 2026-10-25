// Lab map: a topological view of the lab. A network is a segment rail, a NIC is
// a stub hanging off it, and a machine sits in the band below the first segment
// it holds an address on — so position means segment membership, not role.
// Network Nodes (firewall / router appliances) are drawn as devices rather than
// recoloured hosts: wider chassis, cyan spine, ports where the rails land.
import { barLine, cpuColor, el, svgEl } from "./ui.js";
import { getState, setState, subscribe } from "./state.js";
import { getTopologyLayout, saveTopologyNode } from "./api.js";
import { VM_LIFECYCLE, vmLifecycle } from "./vm-state.js";
const MIN_SCALE = 0.65,
  MAX_SCALE = 1.35;
const MAP_W = 1120,
  MAP_H = 520;
// Nodes are sized by CSS from their own content (see the min/max widths on
// .pg-v2-map-node), so this is only the fallback for a box that cannot be
// measured — a node detached from the document reports zero.
const FALLBACK_BOX = { w: 150, h: 118 };
// Rails span the map; a cap at the left carries the segment's identity and is
// the thing you click to select the network.
const RAIL_X = 40,
  RAIL_W = MAP_W - 2 * RAIL_X,
  RAIL_GAP = 236,
  FIRST_RAIL_Y = 172,
  CAP_OFFSET = 24,
  // Where a stub meets a node, measured from the node's top edge. Wires run
  // under the node, so a node's height never has to be known to draw them.
  ANCHOR = 46,
  LINK_W = 300;

// Marks for the function_tags the network-node catalog declares. An unknown tag
// still draws — the catalog may grow without this file changing.
const FN_GLYPH = {
  firewall:
    "M1.4 2.8h9.2v6.4H1.4zM1.4 4.9h9.2M1.4 7.1h9.2M4.6 2.8v2.1M3 4.9v2.2M7.8 4.9v2.2M6.2 7.1v2.1",
  router: "M2 4.2h8M10 4.2L8.1 2.4M10 4.2L8.1 6.1M10 7.9H2M2 7.9l1.9-1.8M2 7.9l1.9 1.8",
  "nat-gateway": "M1.6 1.8v8.4M4.2 6h5.9M10.1 6L7.7 3.7M10.1 6l-2.4 2.4",
  _default: "M6 3.2l2.4 1.4v2.8L6 8.8 3.6 7.4V4.6z",
};
const FN_LABEL = { "nat-gateway": "nat" };

function clamp(s) {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, Number(s.toFixed(2))));
}
function applyScale(scale) {
  const world = document.getElementById("map-world");
  if (world) world.style.setProperty("--map-scale", String(scale));
}
function renderSaveStatus(state) {
  const el2 = document.getElementById("map-save-status");
  if (!el2) return;
  const s = state.ui.saveState;
  el2.dataset.state = s;
  el2.textContent =
    s === "saving"
      ? "saving…"
      : s === "saved"
        ? "saved"
        : s === "error"
          ? "save failed"
          : "topology autosave";
}
function cornerSpans() {
  return ["tl", "tr", "bl", "br"].map((cls) => el("span", { className: `corner ${cls}` }));
}
export function isDeviceVm(vm) {
  return (vm && vm.role) === "network";
}
function nicNetId(nic) {
  return nic.network_id || nic.job_network_id || nic.network || nic.net_id;
}
function vmNetIds(vm) {
  return (vm.nics || []).map(nicNetId).filter(Boolean);
}
function iconPath(d, size = 12) {
  const svg = svgEl("svg", {
    width: size,
    height: size,
    viewBox: "0 0 12 12",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.08",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  });
  svg.appendChild(svgEl("path", { d }));
  return svg;
}
function chassisIcon() {
  const svg = svgEl("svg", {
    width: 15,
    height: 15,
    viewBox: "0 0 15 15",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.1",
    "stroke-linecap": "round",
    "aria-hidden": "true",
  });
  svg.appendChild(svgEl("rect", { x: 1.2, y: 4, width: 12.6, height: 7, rx: 1.2 }));
  svg.appendChild(svgEl("path", { d: "M4 7.6h1.2M6.9 7.6h1.2M9.8 7.6H11" }));
  return svg;
}

// ── Layout ────────────────────────────────────────────────────────────────
// Nodes size themselves to their labels (CSS: max-content between a min and a
// max width), so the layout cannot be computed from constants — every node is
// measured once it is in the DOM and the geometry follows the real boxes. A
// name longer than the cap ellipsises rather than pushing the map apart.
function measure(node) {
  return { w: node.offsetWidth || FALLBACK_BOX.w, h: node.offsetHeight || FALLBACK_BOX.h };
}
// Machines flow left to right through the band under their segment, wrapping
// before the strip the boundary callouts occupy. Real widths mean a row of
// short names packs tight and a row of long ones still cannot collide.
function flowBand(items, top) {
  const limit = RAIL_X + RAIL_W - LINK_W - 20;
  let x = RAIL_X,
    y = top,
    rowH = 0;
  const placed = [];
  items.forEach((item) => {
    if (x > RAIL_X && x + item.w > limit) {
      x = RAIL_X;
      y += rowH + 16;
      rowH = 0;
    }
    placed.push({ id: item.id, x, y });
    x += item.w + 28;
    rowH = Math.max(rowH, item.h);
  });
  return { placed, bottom: y + rowH };
}
function defaultNetPos(railY) {
  return { x: RAIL_X, y: railY - CAP_OFFSET };
}
function layoutMap(topology) {
  return Object.fromEntries((topology?.nodes || []).map((n) => [n.node_id, n]));
}
function clampPos(pos, size, worldH) {
  return {
    x: Math.max(0, Math.min(MAP_W - size.w, pos.x)),
    y: Math.max(0, Math.min(Math.max(MAP_H, worldH) - size.h, pos.y)),
  };
}
function nodePos(id, size, fallback, positions, worldH) {
  const saved = positions[id];
  return clampPos(saved ? { x: saved.x, y: saved.y } : fallback, size, worldH);
}
async function loadTopology() {
  try {
    const topology = await getTopologyLayout();
    setState((prev) => ({ ...prev, topology: topology || { nodes: [] } }));
  } catch (_) {
    setState((prev) => ({ ...prev, topology: { nodes: [] } }));
  }
}

// ── Wires ─────────────────────────────────────────────────────────────────
// One rail per segment, one stub per NIC. A device's stubs are drawn apart so a
// firewall's connections can be traced across the whole map at a glance.
function renderWires(svg, vms, nets, geom, railY, litNets) {
  nets.forEach((net) => {
    const y = railY[net.id];
    if (y == null) return;
    svg.appendChild(
      svgEl("line", {
        class: "pg-v2-rail",
        "data-lit": String(litNets.has(net.id)),
        x1: RAIL_X,
        y1: y,
        x2: RAIL_X + RAIL_W,
        y2: y,
      }),
    );
  });
  vms.forEach((vm) => {
    const pos = geom[vm.id];
    if (!pos) return;
    const device = isDeviceVm(vm);
    // Centred on the box the browser actually laid out, so a wide name keeps
    // its stub under the middle of its node.
    const cx = pos.x + pos.w / 2;
    const anchor = pos.y + ANCHOR;
    vmNetIds(vm).forEach((netId) => {
      const y = railY[netId];
      if (y == null) return;
      svg.appendChild(
        svgEl("line", {
          class: device ? "pg-v2-wire pg-v2-wire--device" : "pg-v2-wire",
          x1: cx,
          y1: Math.min(y, anchor),
          x2: cx,
          y2: Math.max(y, anchor),
        }),
      );
      svg.appendChild(
        svgEl("line", {
          class: device ? "pg-v2-port pg-v2-port--device" : "pg-v2-port",
          x1: cx,
          y1: y - (device ? 5 : 3),
          x2: cx,
          y2: y + (device ? 5 : 3),
        }),
      );
    });
  });
}

// ── Drag / keyboard ───────────────────────────────────────────────────────
function persistNode(info, x, y) {
  setState((prev) => ({ ...prev, ui: { ...prev.ui, saveState: "saving" } }));
  return saveTopologyNode({ node_id: info.id, node_type: info.nodeType, x, y })
    .then((saved) => {
      setState((prev) => ({
        ...prev,
        topology: {
          nodes: [
            ...(prev.topology?.nodes || []).filter((n) => n.node_id !== saved.node_id),
            saved,
          ],
        },
        ui: { ...prev.ui, saveState: "saved" },
      }));
      setTimeout(
        () => setState((prev) => ({ ...prev, ui: { ...prev.ui, saveState: "idle" } })),
        1200,
      );
    })
    .catch((err) => {
      setState((prev) => ({
        ...prev,
        ui: { ...prev.ui, saveState: "error" },
        errors: { ...prev.errors, topology: err.message },
      }));
    });
}
function worldHeight() {
  return Math.max(
    MAP_H,
    parseFloat(document.getElementById("map-world")?.style.getPropertyValue("--map-world-h")) ||
      MAP_H,
  );
}
// A rail spans the map, so only its vertical position carries meaning: dragging
// a segment reorders the stack, it does not slide the segment sideways.
function makeDraggable(node, info) {
  // Read the box at event time: a node's width follows its label, and a rename
  // or a state change can resize it between renders.
  const sz = () => measure(node);
  const lockX = info.kind === "network";
  let drag = null;
  node.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    node.setPointerCapture(e.pointerId);
    drag = {
      id: e.pointerId,
      sx: e.clientX,
      sy: e.clientY,
      ox: parseFloat(node.style.left),
      oy: parseFloat(node.style.top),
      moved: false,
    };
    node.dataset.dragging = "true";
  });
  node.addEventListener("pointermove", (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const scale = getState().ui.mapScale || 1;
    const box = sz();
    const maxX = Math.max(0, MAP_W - box.w);
    const maxY = Math.max(0, worldHeight() - box.h);
    const dx = e.clientX - drag.sx,
      dy = e.clientY - drag.sy;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
    const x = lockX ? drag.ox : Math.max(0, Math.min(maxX, drag.ox + dx / scale));
    const y = Math.max(0, Math.min(maxY, drag.oy + dy / scale));
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
  });
  node.addEventListener("pointerup", (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    const moved = drag.moved;
    const x = parseFloat(node.style.left),
      y = parseFloat(node.style.top);
    drag = null;
    delete node.dataset.dragging;
    if (moved) persistNode(info, x, y);
  });
  const STEP = 10;
  let saveTimer = null;
  node.addEventListener("keydown", (e) => {
    const delta = {
      ArrowLeft: [-STEP, 0],
      ArrowRight: [STEP, 0],
      ArrowUp: [0, -STEP],
      ArrowDown: [0, STEP],
    }[e.key];
    if (!delta) return;
    e.preventDefault();
    const box = sz();
    const x = lockX
      ? parseFloat(node.style.left)
      : Math.max(0, Math.min(MAP_W - box.w, parseFloat(node.style.left) + delta[0]));
    const y = Math.max(0, Math.min(worldHeight() - box.h, parseFloat(node.style.top) + delta[1]));
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
    if (saveTimer) clearTimeout(saveTimer);
    setState((prev) => ({ ...prev, ui: { ...prev.ui, saveState: "saving" } }));
    saveTimer = setTimeout(() => persistNode(info, x, y), 200);
  });
}
function selectable(node, id) {
  node.addEventListener("click", () => setState((prev) => ({ ...prev, selectedId: id })));
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setState((prev) => ({ ...prev, selectedId: id }));
    }
  });
}

// ── Nodes ─────────────────────────────────────────────────────────────────
// Why a node broke, on the node — the panel keeps the full text.
export function failureNote(summary) {
  const line = String(summary || "")
    .split("\n")[0]
    .trim();
  // Guest-script failures surface as Python's CalledProcessError, which leads
  // with a long absolute-path invocation and ends with the part worth reading.
  const outcome = line.match(/'\s+(returned .+)$/);
  const text = outcome ? outcome[1] : line;
  return text.length > 96 ? `${text.slice(0, 95)}…` : text;
}
function nodeFooter(vm) {
  if (vmLifecycle(vm.state) === VM_LIFECYCLE.FAILED) {
    return [
      el("div", {
        className: "pg-v2-map-node-fail",
        attrs: { title: vm.error_summary || "" },
        text: vm.error_summary ? failureNote(vm.error_summary) : "failed",
      }),
    ];
  }
  if (vm.cpu_pct == null && vm.mem_pct == null) return [];
  return [
    barLine("cpu", vm.cpu_pct ?? 0, cpuColor(vm.cpu_pct ?? 0)),
    barLine("mem", vm.mem_pct ?? 0, "var(--c-info)"),
  ];
}
export function addressRows(vm, netName) {
  const nics = vm.nics || [];
  const statusRows =
    vm.reachability_status === "partial"
      ? [
          el("div", {
            className: "node-stat",
            children: [
              el("span", { className: "k", text: "reachability" }),
              el("span", { className: "v", text: "partial" }),
            ],
          }),
        ]
      : [];
  if (!nics.length)
    return [
      ...statusRows,
      el("div", {
        className: "node-stat",
        children: [
          el("span", { className: "k", text: "ip" }),
          el("span", { className: "v", text: vm.ip || "—" }),
        ],
      }),
    ];
  // Keyed by segment rather than a single "ip" row, so a machine sitting on two
  // segments says so on its own face.
  return [
    ...statusRows,
    ...nics.map((nic) => {
      const seg = netName[nicNetId(nic)] || "net";
      return el("div", {
        className: "node-stat",
        children: [
          el("span", {
            className: "k",
            dataset: { seg: "true" },
            // The column ellipsises a long segment name; the title keeps it whole.
            attrs: { title: seg },
            text: seg,
          }),
          el("span", { className: "v", text: nic.ip || "—" }),
        ],
      });
    }),
  ];
}
function baseNodeAttrs(vm, state, extraClass) {
  const isSelected = state.selectedId === vm.id;
  return {
    className: extraClass ? `pg-v2-map-node ${extraClass}` : "pg-v2-map-node",
    dataset: {
      nodeId: vm.id,
      type: "vm",
      role: vm.role,
      state: vm.state || "unknown",
      selected: String(isSelected),
    },
    attrs: {
      tabindex: "0",
      role: "button",
      "aria-label": vm.label,
      "aria-pressed": String(isSelected),
    },
    // Positioned once the browser has laid the box out and its real width is
    // known; placing it here would fix geometry to a guess.
    style: { left: "0px", top: "0px" },
  };
}
function vmNode(vm, state, netName, peer) {
  const spans = vmNetIds(vm).length > 1;
  const attrs = baseNodeAttrs(vm, state);
  attrs.dataset.peer = String(peer);
  attrs.children = [
    ...cornerSpans(),
    spans
      ? el("span", {
          className: "pg-v2-map-segchip",
          text: `on ${vmNetIds(vm)
            .map((id) => netName[id] || "net")
            .join(" + ")}`,
        })
      : null,
    el("div", {
      className: "node-head",
      children: [
        el("span", { className: "dot" }),
        el("span", { className: "node-name", text: vm.label }),
        el("span", { className: "role-pill", text: vm.role }),
      ],
    }),
    // One block, one grid (CSS): the key column is as wide as the longest key
    // on this card, so a segment name cannot run over the address it labels.
    el("div", {
      className: "node-stats",
      children: [
        el("div", {
          className: "node-stat",
          children: [
            el("span", { className: "k", text: "os" }),
            el("span", { className: "v", text: vm.os || vm.image_id || "—" }),
          ],
        }),
        ...addressRows(vm, netName),
      ],
    }),
    ...nodeFooter(vm),
  ].filter(Boolean);
  const node = el("article", attrs);
  makeDraggable(node, { id: vm.id, nodeType: "vm", kind: "vm" });
  selectable(node, vm.id);
  return node;
}
function deviceNode(vm, state, netName, peer) {
  const tags = vm.function_tags || [];
  const attrs = baseNodeAttrs(vm, state, "pg-v2-map-device");
  attrs.dataset.peer = String(peer);
  attrs.dataset.device = "true";
  attrs.children = [
    ...cornerSpans(),
    el("div", {
      className: "dev-head",
      children: [
        chassisIcon(),
        el("span", { className: "dev-name", text: vm.label }),
        el("span", { className: "dev-kind", text: "device" }),
      ],
    }),
    el("div", { className: "dev-img", text: vm.os || vm.image_id || "—" }),
    tags.length
      ? el("div", {
          className: "dev-fns",
          children: tags.map((tag) =>
            el("span", {
              className: "dev-fn",
              children: [
                iconPath(FN_GLYPH[tag] || FN_GLYPH._default),
                el("span", { text: FN_LABEL[tag] || tag }),
              ],
            }),
          ),
        })
      : null,
    el("div", {
      className: "dev-ports",
      children: (vm.nics || []).map((nic, i) => {
        const seg = netName[nicNetId(nic)] || "net";
        return el("div", {
          className: "dev-port",
          children: [
            el("span", { className: "pn", text: `eth${nic.ordinal ?? i}` }),
            // Ports share one grid, so a long segment name ellipsises rather
            // than running over the address; the title keeps it whole.
            el("span", { className: "pseg", attrs: { title: seg }, text: seg }),
            el("span", { className: "pip", text: nic.ip || "—" }),
          ],
        });
      }),
    }),
    ...nodeFooter(vm),
  ].filter(Boolean);
  const node = el("article", attrs);
  makeDraggable(node, { id: vm.id, nodeType: "vm", kind: "device" });
  selectable(node, vm.id);
  return node;
}
// The segment's identity, and the thing you click to select the network. It
// rides the left end of its rail; the rail itself is drawn in the wire layer.
function netCap(net, state, members) {
  const isSelected = state.selectedId === net.id;
  const devices = members.filter(isDeviceVm).length;
  const failed = members.filter((v) => vmLifecycle(v.state) === VM_LIFECYCLE.FAILED).length;
  const hosts = members.length - devices;
  const node = el("article", {
    className: "pg-v2-map-node pg-v2-map-rail-cap",
    dataset: {
      nodeId: net.id,
      type: "network",
      state: net.state || "unknown",
      selected: String(isSelected),
    },
    attrs: {
      tabindex: "0",
      role: "button",
      "aria-label": net.name,
      "aria-pressed": String(isSelected),
    },
    style: { left: "0px", top: "0px" },
    children: [
      el("span", { className: "nm", text: net.name }),
      el("span", { className: "cidr", text: net.subnet_cidr || "—" }),
      el("span", {
        className: "mode",
        dataset: { egress: String(net.isolated === false) },
        text: net.isolated === false ? "nat · egress" : "isolated",
      }),
      el("span", {
        className: "tally",
        text:
          `${hosts} ${hosts === 1 ? "host" : "hosts"}` +
          (devices ? ` · ${devices} ${devices === 1 ? "device" : "devices"}` : "") +
          (failed ? ` · ${failed} failed` : ""),
      }),
    ],
  });
  makeDraggable(node, { id: net.id, nodeType: "network", kind: "network" });
  selectable(node, net.id);
  return node;
}
// What two neighbouring segments have in common, said where the boundary is.
// A device put there on purpose and a host that merely has a leg on both are
// both bridges; only one was meant to be. Stating which is the point.
function linkCallout(a, b, both, y) {
  return el("div", {
    className: "pg-v2-map-link",
    dataset: { empty: String(both.length === 0) },
    style: { left: `${RAIL_X + RAIL_W - LINK_W}px`, top: `${y}px` },
    children: [
      el("span", {
        className: "hd",
        text: `${a.name} ↔ ${b.name} · ${both.length} in common`,
      }),
      ...(both.length
        ? both.map((vm) =>
            el("div", {
              className: "row",
              dataset: { kind: isDeviceVm(vm) ? "device" : "host" },
              children: [
                el("span", {
                  className: "who",
                  children: [
                    el("span", { text: vm.label }),
                    el("span", {
                      className: "pill",
                      text: isDeviceVm(vm)
                        ? (vm.function_tags || []).join(" · ") || "device"
                        : `${vm.role} · both`,
                    }),
                  ],
                }),
                el("span", {
                  className: "why",
                  text: isDeviceVm(vm)
                    ? "Ports on both segments. Traffic crossing here goes through it."
                    : `A host, not a device — traffic between ${a.name} and ${b.name} can cross here without touching any appliance.`,
                }),
              ],
            }),
          )
        : [
            el("span", {
              className: "why",
              text: "No machine holds an address on both. These segments cannot see each other.",
            }),
          ]),
    ],
  });
}

// ── Context bar ───────────────────────────────────────────────────────────
function legendItem(mark, text) {
  return el("span", {
    className: "pg-v2-map-lg",
    children: [el("span", { className: `mk mk--${mark}` }), el("span", { text })],
  });
}
function renderContext(state, vms, nets, netName) {
  const bar = document.getElementById("map-context");
  if (!bar) return;
  bar.textContent = "";
  const devices = vms.filter(isDeviceVm);
  const hosts = vms.filter((vm) => !isDeviceVm(vm));
  const failed = vms.filter((vm) => vmLifecycle(vm.state) === VM_LIFECYCLE.FAILED).length;
  const egress = nets.filter((n) => n.isolated === false);
  const selected = vms.find((vm) => vm.id === state.selectedId) || null;
  const selectedNets = selected ? vmNetIds(selected) : [];
  const peers = selected
    ? vms.filter(
        (vm) => vm.id !== selected.id && vmNetIds(vm).some((id) => selectedNets.includes(id)),
      ).length
    : 0;
  bar.appendChild(
    el("div", {
      className: "pg-v2-map-facts",
      children: [
        el("span", {
          children: [
            el("b", { text: String(nets.length) }),
            el("span", { className: "u", text: nets.length === 1 ? " segment" : " segments" }),
          ],
        }),
        el("span", { className: "sep", text: "·" }),
        el("span", {
          children: [
            el("b", { text: String(hosts.length) }),
            el("span", { className: "u", text: hosts.length === 1 ? " host" : " hosts" }),
          ],
        }),
        el("span", { className: "sep", text: "·" }),
        el("span", {
          className: "dev",
          children: [
            el("b", { text: String(devices.length) }),
            el("span", {
              text: devices.length === 1 ? " network device" : " network devices",
            }),
          ],
        }),
      ],
    }),
  );
  bar.appendChild(
    el("span", {
      className: "pg-v2-map-flag",
      dataset: { kind: "egress" },
      text: egress.length
        ? `${egress.map((n) => n.name).join(", ")} → host network`
        : "fully isolated",
    }),
  );
  bar.appendChild(
    el("span", {
      className: "pg-v2-map-flag",
      dataset: { kind: "fail", ok: String(failed === 0) },
      text: failed ? `${failed} failed` : "no failures",
    }),
  );
  bar.appendChild(el("span", { className: "pg-v2-map-rule" }));
  bar.appendChild(
    el("span", {
      className: "pg-v2-map-focus",
      dataset: { empty: String(!selected) },
      text: selected
        ? `${selected.label} · on ${selectedNets.map((id) => netName[id] || "net").join(" + ") || "no segment"} · shares a segment with ${peers}`
        : "Select a machine to light the segments it sits on.",
    }),
  );
  bar.appendChild(
    el("div", {
      className: "pg-v2-map-legend",
      children: [
        legendItem("rail", "segment"),
        legendItem("nic", "host nic"),
        legendItem("port", "device port"),
        legendItem("device", "network device"),
      ],
    }),
  );
}

// ── Render ────────────────────────────────────────────────────────────────
function renderNodes(state) {
  const world = document.getElementById("map-world"),
    svg = document.getElementById("connection-map-svg");
  if (!world || !svg) return;
  const focusedId = world.contains(document.activeElement)
    ? document.activeElement.dataset.nodeId
    : null;
  Array.from(world.children).forEach((c) => {
    if (c !== svg) world.removeChild(c);
  });
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const session = state.session;
  if (!session) return;
  const vms = session.vms || [],
    nets = session.networks || [];
  const netName = Object.fromEntries(nets.map((n) => [n.id, n.name]));
  const netIndex = Object.fromEntries(nets.map((n, i) => [n.id, i]));
  const membersOf = (netId) => vms.filter((vm) => vmNetIds(vm).includes(netId));

  const positions = layoutMap(state.topology);
  // Band = the strip under a segment. A machine defaults into the band of the
  // first segment it is attached to, so multi-homed machines land between the
  // two rails they join.
  const bandOf = (vm) => {
    const idxs = vmNetIds(vm)
      .map((id) => netIndex[id])
      .filter((i) => i != null);
    return idxs.length ? Math.min(...idxs) : Math.max(0, nets.length - 1);
  };
  const selected = vms.find((vm) => vm.id === state.selectedId) || null;
  const litNets = new Set(selected ? vmNetIds(selected) : []);

  // 1. Build and attach every node, unpositioned. Nothing can be laid out until
  //    the browser has sized the boxes to their labels.
  const nodeEl = {},
    capEl = {};
  vms.forEach((vm) => {
    const peer = !!(
      selected &&
      selected.id !== vm.id &&
      vmNetIds(vm).some((id) => litNets.has(id))
    );
    nodeEl[vm.id] = isDeviceVm(vm)
      ? deviceNode(vm, state, netName, peer)
      : vmNode(vm, state, netName, peer);
    world.appendChild(nodeEl[vm.id]);
  });
  nets.forEach((net) => {
    const cap = netCap(net, state, membersOf(net.id));
    cap.dataset.lit = String(litNets.has(net.id));
    capEl[net.id] = cap;
    world.appendChild(cap);
  });

  // 2. Measure once, then never touch the DOM for geometry again this pass.
  const box = {};
  vms.forEach((vm) => (box[vm.id] = measure(nodeEl[vm.id])));
  nets.forEach((net) => (box[net.id] = measure(capEl[net.id])));

  // 3. Stack the rails: each band is exactly as tall as the machines that
  //    default into it, so a busy segment pushes the next rail down instead of
  //    stacking machines on top of each other.
  const bands = nets.map((_, i) =>
    vms.filter((vm) => bandOf(vm) === i).map((vm) => ({ id: vm.id, ...box[vm.id] })),
  );
  const bandHeight = bands.map((band) => flowBand(band, 0).bottom);
  const railY = {};
  let y = FIRST_RAIL_Y;
  nets.forEach((net, i) => {
    railY[net.id] = y;
    y = Math.max(y + RAIL_GAP, y + 50 + bandHeight[i] + 40);
  });

  // 4. Saved rail positions win, and the bands follow wherever the rail ended
  //    up — dragging a segment carries the machines that never moved with it.
  const worldGuess = Math.max(
    MAP_H,
    y,
    ...Object.values(positions).map((p) => p.y + FALLBACK_BOX.h + 40),
  );
  const capPos = {};
  nets.forEach((net) => {
    capPos[net.id] = nodePos(
      net.id,
      box[net.id],
      defaultNetPos(railY[net.id]),
      positions,
      worldGuess,
    );
    railY[net.id] = capPos[net.id].y + CAP_OFFSET;
  });
  const place = {};
  nets.forEach((net, i) => {
    flowBand(bands[i], railY[net.id] + 50).placed.forEach((p) => {
      place[p.id] = { x: p.x, y: p.y };
    });
  });

  // 5. Position everything, and size the world to whatever it actually covers.
  const geom = {};
  let bottom = MAP_H;
  nets.forEach((net) => {
    capEl[net.id].style.left = `${capPos[net.id].x}px`;
    capEl[net.id].style.top = `${capPos[net.id].y}px`;
    bottom = Math.max(bottom, capPos[net.id].y + box[net.id].h);
  });
  vms.forEach((vm) => {
    const pos = nodePos(
      vm.id,
      box[vm.id],
      place[vm.id] || { x: RAIL_X, y: FIRST_RAIL_Y + 50 },
      positions,
      worldGuess,
    );
    geom[vm.id] = { ...pos, ...box[vm.id] };
    nodeEl[vm.id].style.left = `${pos.x}px`;
    nodeEl[vm.id].style.top = `${pos.y}px`;
    bottom = Math.max(bottom, pos.y + box[vm.id].h);
  });
  const worldH = Math.max(MAP_H, bottom + 40);

  world.style.setProperty("--map-world-w", `${MAP_W}px`);
  world.style.setProperty("--map-world-h", `${worldH}px`);
  svg.setAttribute("viewBox", `0 0 ${MAP_W} ${worldH}`);
  svg.setAttribute("width", String(MAP_W));
  svg.setAttribute("height", String(worldH));
  renderWires(svg, vms, nets, geom, railY, litNets);

  // Rails pair by drawn position, not declaration order, so a dragged segment
  // pairs with whatever it now sits next to.
  const ordered = [...nets].sort((a, b) => railY[a.id] - railY[b.id]);
  for (let i = 0; i < ordered.length - 1; i += 1) {
    const a = ordered[i],
      b = ordered[i + 1];
    const both = vms.filter((vm) => vmNetIds(vm).includes(a.id) && vmNetIds(vm).includes(b.id));
    world.appendChild(linkCallout(a, b, both, (railY[a.id] + railY[b.id]) / 2 - 52));
  }
  renderContext(state, vms, nets, netName);
  if (focusedId) (nodeEl[focusedId] || capEl[focusedId])?.focus({ preventScroll: true });
}
function wireZoom() {
  const zin = document.getElementById("btn-map-zoom-in"),
    zout = document.getElementById("btn-map-zoom-out"),
    fit = document.getElementById("btn-map-fit");
  if (zin)
    zin.addEventListener("click", () => {
      const s = clamp(getState().ui.mapScale + 0.1);
      setState((p) => ({ ...p, ui: { ...p.ui, mapScale: s } }));
    });
  if (zout)
    zout.addEventListener("click", () => {
      const s = clamp(getState().ui.mapScale - 0.1);
      setState((p) => ({ ...p, ui: { ...p.ui, mapScale: s } }));
    });
  if (fit)
    fit.addEventListener("click", () => setState((p) => ({ ...p, ui: { ...p.ui, mapScale: 1 } })));
}
export function mountMap() {
  wireZoom();
  subscribe(
    (s) => s.ui.mapScale,
    (scale) => applyScale(scale),
  );
  subscribe(
    (s) => s.ui.saveState,
    () => renderSaveStatus(getState()),
  );
  subscribe(
    (s) => s.session?.job_id || s.session?.id || null,
    (id) => {
      if (id) loadTopology();
      else setState((prev) => ({ ...prev, topology: { nodes: [] } }));
    },
  );
  subscribe(
    (s) => ({
      vms: s.session?.vms,
      nets: s.session?.networks,
      topology: s.topology,
      selectedId: s.selectedId,
    }),
    () => renderNodes(getState()),
  );
}
