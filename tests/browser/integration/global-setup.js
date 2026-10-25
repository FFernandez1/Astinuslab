const fs = require("fs");
const path = require("path");

// Tier-2 isolation: write a fake catalog + fake images into the throwaway dir
// published by playwright.integration.config.js, so the isolated backend has a
// target and an attacker image to read without any real images on disk.
// Mirrors tests/python/e2e/conftest.py::_write_catalogs.
module.exports = async () => {
  const isoDir = process.env.ASTINUSLAB_INT_ISO_DIR;
  if (!isoDir) throw new Error("ASTINUSLAB_INT_ISO_DIR not set by integration config");

  const images = path.join(isoDir, "images");
  const catalog = path.join(isoDir, "catalog");
  fs.mkdirSync(images, { recursive: true });
  fs.mkdirSync(catalog, { recursive: true });
  fs.mkdirSync(path.join(isoDir, "storage"), { recursive: true });

  for (const name of ["ubuntu-22.04-test.qcow2", "kali-rolling-test.qcow2"]) {
    fs.writeFileSync(path.join(images, name), "fake qcow2\n");
  }

  fs.writeFileSync(
    path.join(catalog, "targets.yaml"),
    [
      "targets:",
      "  - id: ubuntu-22-04-default",
      "    enabled: true",
      "    distro:",
      "      slug: ubuntu",
      '      version: "22.04"',
      "      family: debian",
      "    image:",
      "      source_type: qcow2",
      "      image_ref: ubuntu-22.04-test.qcow2",
      "      default_username: analyst",
      "    virtualization:",
      "      os_variant: generic",
      "",
    ].join("\n"),
  );
  fs.writeFileSync(path.join(catalog, "workloads.yaml"), "version: 1\nworkloads: []\n");
  fs.writeFileSync(
    path.join(catalog, "attackers.yaml"),
    [
      "attackers:",
      "  - id: kali-rolling",
      "    enabled: true",
      "    display_name: Kali Rolling (test)",
      "    image_ref: kali-rolling-test.qcow2",
      "    username: kali",
      "    os_variant: generic",
      "",
    ].join("\n"),
  );
};
