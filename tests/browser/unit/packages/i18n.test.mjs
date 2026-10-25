import test from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../src/astinuslab/web/static/packages",
);

class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.checked = false;
    this.disabled = false;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = children.filter(Boolean);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener() {}

  querySelectorAll() {
    return [];
  }

  querySelector() {
    return null;
  }
}

globalThis.document = { createElement: (tag) => new FakeNode(tag) };

const { renderPackageList } =
  await import("../../../../src/astinuslab/web/static/packages/render-library.js");
const { renderInspection } =
  await import("../../../../src/astinuslab/web/static/packages/render-inspection.js");
const { renderPreflight } =
  await import("../../../../src/astinuslab/web/static/packages/render-status.js");

function collectText(node) {
  return [node.textContent, ...node.children.flatMap(collectText)].join(" ");
}

test("package renderers use the active dictionary for Spanish labels", () => {
  const translations = {
    pkg_creator: "Creador",
    pkg_license: "Licencia",
    pkg_mode: "Modo",
    pkg_archive: "Archivo",
    pkg_bytes: "bytes",
    pkg_preflight: "Preflight",
    pkg_prepare: "Preparar",
    pkg_download: "Descargar",
    pkg_open_package: "Abrir paquete",
    pkg_delete: "Eliminar",
    pkg_no_description: "Sin descripción.",
    pkg_imported_action: "Importar paquete inspeccionado",
    pkg_reproducibility: "Reproducibilidad",
    pkg_required_capabilities: "Capacidades requeridas",
    pkg_assets: "Activos",
    pkg_host_resources: "Recursos del host",
    pkg_runbook: "Runbook",
    pkg_embedded: "integrado",
    pkg_external: "externo",
    pkg_none: "ninguno",
    pkg_no_boot_assets: "No hay activos de arranque declarados.",
    pkg_ready: "listo",
    pkg_blocked: "bloqueado",
    pkg_launch: "Iniciar paquete",
    pkg_retained_storage: "Almacenamiento retenido",
    pkg_launch_storage: "Almacenamiento de inicio",
    pkg_required_storage: "Almacenamiento requerido",
    pkg_host: "Host",
    pkg_unknown: "desconocido",
    pkg_warning: "advertencia",
    pkg_review_warning: "Revisa esta advertencia.",
    pkg_no_warnings: "No hay advertencias.",
    pkg_error: "error",
    pkg_egress_ack: "Reconozco que este laboratorio puede acceder a redes externas.",
    pkg_missing_value: "—",
  };
  globalThis.window = { t: (key, fallback) => translations[key] ?? fallback ?? key };

  const library = new FakeNode("div");
  renderPackageList(library, [
    {
      id: "record-1",
      package_id: "case",
      version: "1.0.0",
      creator: "Ana",
      license: "MIT",
      reproducibility: "locked",
      archive_size_bytes: 12,
      signature_status: "trusted",
      title: "Caso",
      description: "Descripción",
    },
  ]);
  const libraryText = collectText(library);
  assert.match(libraryText, /Creador/);
  assert.match(libraryText, /Abrir paquete/);
  assert.doesNotMatch(libraryText, /Creator|Open package/);

  const inspection = new FakeNode("div");
  renderInspection(inspection, {
    inspection_id: "inspection-1",
    package_id: "case",
    version: "1.0.0",
    creator: "Ana",
    license: "MIT",
    reproducibility: "locked",
    signature_status: "trusted",
    title: "Caso",
    runbook: "# Pasos",
    required_capabilities: [],
    assets: [],
    resources: {},
  });
  const inspectionText = collectText(inspection);
  assert.match(inspectionText, /Importar paquete inspeccionado/);
  assert.match(inspectionText, /Recursos del host/);
  assert.doesNotMatch(inspectionText, /Import inspected package|Host resources/);

  const preflight = new FakeNode("div");
  renderPreflight(preflight, {
    ready: true,
    warnings: [],
    errors: [],
    retained_disk_bytes: 1,
    launch_disk_bytes: 2,
    required_disk_bytes: 3,
    host: { architecture: "x86_64" },
  });
  const preflightText = collectText(preflight);
  assert.match(preflightText, /Iniciar paquete/);
  assert.match(preflightText, /Almacenamiento retenido/);
  assert.doesNotMatch(preflightText, /Launch package|Retained storage/);
});

test("package modules keep display strings behind translation lookups", async () => {
  const fs = await import("node:fs/promises");
  const files = ["index.js", "render-inspection.js", "render-library.js", "render-status.js"];
  for (const name of files) {
    const source = await fs.readFile(join(PACKAGE_DIR, name), "utf8");
    assert.doesNotMatch(
      source,
      /(?:text|textContent):\s*["'](?:Creator|License|Download|Delete|Cancel|Prepare|Preflight|Teardown)["']/,
    );
  }
  const index = await fs.readFile(join(PACKAGE_DIR, "index.js"), "utf8");
  assert.doesNotMatch(index, /window\.confirm\s*\(/);
});
