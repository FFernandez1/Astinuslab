# Third-Party Notices

This inventory separates AstinusLab-owned source from third-party material that
AstinusLab actually ships, and from external software and runtime material used
by optional forensic workflows. Inclusion in this notice inventory does not mean
that a listed third-party component is included in the AstinusLab source
archive, installer, or Python package: read the `Distribution status` column,
which is authoritative per row. `Shipped` does not imply AstinusLab-owned.

## Distribution inventory

| Component | Distribution status | License or terms | Notice |
|---|---|---|---|
| AstinusLab-owned source | Shipped | Volatility Software License (VSL) 1.0 | Copyright 2026 Francisco Fernandez. The root `LICENSE` applies only to AstinusLab-owned material. It does not relicense third-party material, and AstinusLab is not part of Volatility. |
| htmx 2.0.4, vendored at `src/astinuslab/web/static/htmx.min.js` | Shipped | Zero-Clause BSD (0BSD) | One of the three third-party browser payloads AstinusLab ships, alongside the two self-hosted type families below. It travels in both the source archive and the installed package because setuptools package data explicitly includes the package-owned web resources. The file is byte-identical to the upstream [`v2.0.4` `dist/htmx.min.js`](https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js). 0BSD imposes no notice-retention or attribution condition; the exact upstream text is retained in `LICENSES/htmx-0BSD.txt` for inventory completeness. AstinusLab does not relicense htmx. |
| Inter 4.1, self-hosted at `src/astinuslab/web/static/fonts/InterVariable.woff2` | Shipped | SIL Open Font License 1.1 | Copyright (c) 2016 The Inter Project Authors. The UI body face. Bundled so the strict `font-src 'self'` CSP is satisfied without any Google Fonts request. Extracted unmodified from the upstream [`v4.1` release archive](https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip); the file is not renamed, so the OFL Reserved Font Name condition is not engaged. OFL 1.1 requires the copyright notice and licence to accompany the font, which `LICENSES/inter-OFL-1.1.txt` does. AstinusLab does not relicense Inter. |
| JetBrains Mono 2.304, self-hosted at `src/astinuslab/web/static/fonts/JetBrainsMono-*.woff2` | Shipped | SIL Open Font License 1.1 | Copyright 2020 The JetBrains Mono Project Authors. The UI monospace face, bundled as the four static weights the interface uses (400, 600, 700, 800) because upstream ships no variable webfont. Extracted unmodified from the upstream [`v2.304` release archive](https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip); the files are not renamed, so the OFL Reserved Font Name condition is not engaged. The required notice and licence are retained in `LICENSES/jetbrains-mono-OFL-1.1.txt`. AstinusLab does not relicense JetBrains Mono. |
| Volatility 3 Framework | Not shipped; operator-supplied | Volatility Software License (VSL) 1.0 | Copyright 2019 Volatility Foundation. Review the [official VSL 1.0 page](https://www.volatilityfoundation.org/license/vsl-v1.0) and the [pinned Volatility 3 source](https://github.com/volatilityfoundation/volatility3/tree/v2.28.0). AstinusLab does not relicense Volatility 3. |
| `dwarf2json` | Not shipped; operator-supplied | VSL 1.0 | Copyright 2019 Volatility Foundation. Review the [official VSL 1.0 page](https://www.volatilityfoundation.org/license/vsl-v1.0) and the [audited pinned source](https://github.com/volatilityfoundation/dwarf2json/tree/9f14607e0d339d463ea725fbd5c08aa7b7d40f75). The operator verifies and records the chosen artifact. |
| `pflag` in the audited official `dwarf2json` 0.9.0 build | Not shipped by AstinusLab | BSD-3-Clause | Informational provenance only. The exact upstream notice is retained in `LICENSES/pflag-BSD-3-Clause.txt`. |
| Go runtime in the audited official `dwarf2json` 0.9.0 build | Not shipped by AstinusLab | BSD-3-Clause | Informational provenance only. The exact upstream notice is retained in `LICENSES/go-BSD-3-Clause.txt`. |
| Windows symbol packages/catalog content | Runtime external material, not source payload | Upstream provider terms | The material remains governed by applicable provider terms. Operators must review those terms before use; AstinusLab records source identifiers, URLs, hashes, and other available provenance for acquired symbol material. This inventory does not allocate legal responsibility. |

The exact license-source URLs, revisions, retrieval dates, and payload hashes for
the retained text copies are recorded in `LICENSES/README.md`.

## Locked dependencies and build tools

`LICENSES/dependency-inventory.json` records all 91 third-party Python and 75 npm
lockfile entries, including optional and platform-conditional packages. It binds
the review to the SHA-256 of `uv.lock`, `requirements.lock`,
`requirements-dev.lock`, `package-lock.json` and `pyproject.toml`. One source, Linux or universal
artifact per distribution was downloaded and checked against its lockfile digest.
The inventory records archive license-member hashes and upstream license
metadata; metadata alone is not a legal determination. Python scopes distinguish
runtime, development and documentation requirements for Linux x86_64 on Python
3.12 and 3.13. An empty applicable-platform list does not mean the package was
omitted from the inventory.

These packages are externally installed, not vendored dependency payloads in the
AstinusLab source archive or wheel. Preserve their own licenses and notices when
distributing an installed environment or bundled derivative. AstinusLab's own
license does not replace their terms. The inventory is a review snapshot: refresh
it and review changed terms whenever any bound lockfile changes.

Review remains open for exact-artifact obligations and any redistribution:

- `libvirt-python` declares LGPL terms; its selected archive contains LGPL 2.1
  text. Resolve the precise applicable grant from source headers and upstream
  terms rather than normalizing the metadata by assumption.
- MPL packages, the `text-unidecode` Artistic/GPL alternatives, and dual-license
  packages require a recorded license choice and applicable source/notice review.
- The selected npm archives for `@humanfs/types`, `esrecurse`, `imurmurhash`,
  `keyv` and `natural-compare` contain no separate license file. Preserve and
  review upstream and embedded/README license evidence before closing these rows.
- `pyproject.toml` declares build requirements `setuptools>=77` and `wheel`
  without exact pins. Record the actual build environment and its license
  evidence; the lock inventory does not attest those build artifacts.
- Playwright's installed browser binaries and native libraries have additional
  bundled component notices. They are not included by the npm/Python package
  inventory and are not AstinusLab source-release payloads.

## Workloads, images and runtime material

The repository ships orchestration scripts and metadata, not the external
payloads below. Names and versions describe the script-selected inputs; mutable
endpoints and guest package repositories still require an exact runtime manifest.
The source archive's lack of those payloads is not permission to redistribute
them in exported labs or installed environments.

| External component | Selected input and official terms evidence | Distribution and remaining review |
|---|---|---|
| WordPress workloads | WordPress 6.4 and 5.0; [WordPress license](https://wordpress.org/about/license/) identifies GPLv2 or later | Guest download from wordpress.org; inspect each actual archive and included components, retain GPL/source/notices as applicable. |
| WP-CLI | Mutable `wp-cli/builds` phar endpoint, with 2.10.0 fallback; [pinned MIT text](https://raw.githubusercontent.com/wp-cli/wp-cli/v2.10.0/LICENSE) | Guest download; record the actual phar version/hash and bundled dependency terms. The fallback license does not attest a mutable download. |
| DVWA | Workload installer clones the upstream default branch; training lab pins `3663d72c4c4f51ac5bcd9f0c8a16b9d890383347`; [pinned GPLv3 text](https://raw.githubusercontent.com/digininja/DVWA/3663d72c4c4f51ac5bcd9f0c8a16b9d890383347/COPYING.txt) | Guest installation/build input; review the actual checkout and its dependencies. The pinned text does not attest the default branch. |
| OWASP Juice Shop | 17.1.1, Node 20 Linux x64 asset; [pinned MIT text](https://raw.githubusercontent.com/juice-shop/juice-shop/v17.1.1/LICENSE) | Guest download; review bundled dependencies in the exact release asset. |
| GitLab CE | 11.4.7-ce.0; [pinned MIT text](https://gitlab.com/gitlab-org/gitlab-foss/-/raw/v11.4.7/LICENSE) | Guest package from packages.gitlab.com; the package's bundled third-party terms need their own review. |
| Node.js, guest web/database services and utilities | NodeSource 20.x setup and configured distribution repositories; scripts under `guest/workloads/` select packages | Externally installed; versions and constituent licenses depend on the selected repositories. Capture package versions, sources and copyright files for the actual guest. |
| Ubuntu 22.04 and 24.04 cloud images | `cloud-images.ubuntu.com`; release/current paths in `ops/images/download_images.sh` | External composite OS images; record actual image hash, publisher terms and per-package copyright/source information. No single project license covers the whole image. |
| Debian 12 cloud image | `cloud.debian.org/images/cloud/bookworm/latest/` | External composite OS image; exact download, package manifest and applicable terms remain runtime evidence. |
| Fedora 40 cloud image | `archives.fedoraproject.org`, Cloud Base 40-1.14 | External composite OS image; retain exact image provenance and constituent package terms. |
| Host virtualization and services | Distribution-provided QEMU/KVM, libvirt, Redis and associated host packages selected by installation instructions | Host prerequisites; not vendored. Record actual installed versions and package notices before distributing a configured host. |
| Linux debug packages and generated ISFs | Debian/Ubuntu debug repositories and the operator's selected kernel/debug input | Runtime input/output; record package and tool provenance. Review applicable source, symbol and generated-output rights before redistribution. |
| Windows OS, guest tools and symbols | Operator/provider-supplied content; Windows functionality is experimental | Not shipped. Provider-specific use and redistribution terms apply; source identifiers and hashes do not grant redistribution permission. |
| User-supplied images, workloads and portable package content | Operator-selected sources and embedded material | No blanket redistribution permission. The exporter must have the necessary rights and preserve applicable notices/source obligations. |

Contributor-rights confirmation, runtime artifact records and any required
additional notices remain publication prerequisites. This expanded inventory
does not close the legal gate.

## Shipped vendored frontend asset provenance

AstinusLab ships three third-party browser payloads: the vendored htmx script and
the two self-hosted brand type families. All of them are tracked in the repository,
so they travel with the source archive, and the declared setuptools package data
carries them into wheels and installed releases.

`src/astinuslab/web/static/htmx.min.js`:

- upstream project: <https://github.com/bigskysoftware/htmx>;
- upstream tag: `v2.0.4`;
- embedded version string in the shipped file: `version:"2.0.4"`;
- upstream artifact:
  <https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js>;
- shipped file size: 50917 bytes;
- shipped file SHA-256:
  `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.

The upstream artifact was independently downloaded on 2026-08-14 and its SHA-256
matched the shipped file byte-for-byte. htmx `v2.0.4` is licensed Zero-Clause
BSD, which grants use, copying, modification, and distribution with no notice,
attribution, or copyleft condition. Retaining `LICENSES/htmx-0BSD.txt` therefore
completes the inventory; it does not discharge an obligation.

`src/astinuslab/web/static/fonts/` holds the two brand type families. Each file was
extracted, without modification, from the pinned upstream release archive recorded
below; the shipped bytes and the archive were both hashed on 2026-09-23:

| Shipped file | Bytes | SHA-256 | Upstream archive member |
|---|---|---|---|
| `InterVariable.woff2` | 352240 | `693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3` | `Inter-4.1.zip` → `web/InterVariable.woff2` |
| `JetBrainsMono-Regular.woff2` | 92164 | `a9cb1cd82332b23a47e3a1239d25d13c86d16c4220695e34b243effa999f45f2` | `JetBrainsMono-2.304.zip` → `fonts/webfonts/JetBrainsMono-Regular.woff2` |
| `JetBrainsMono-SemiBold.woff2` | 94472 | `918edad542a1da608fd2ba8daebaff9ac802309103fe760eed465b8b4e47faf1` | `JetBrainsMono-2.304.zip` → `fonts/webfonts/JetBrainsMono-SemiBold.woff2` |
| `JetBrainsMono-Bold.woff2` | 94588 | `c503cc5ec5f8b2c7666b7ecda1adf44bd45f2e6579b2eba0fc292150416588a2` | `JetBrainsMono-2.304.zip` → `fonts/webfonts/JetBrainsMono-Bold.woff2` |
| `JetBrainsMono-ExtraBold.woff2` | 93692 | `88097a36a292c145a14394f1fd5133f76b9a3d6fdb133834b2c94a3db61dd39a` | `JetBrainsMono-2.304.zip` → `fonts/webfonts/JetBrainsMono-ExtraBold.woff2` |

Archive SHA-256:
`Inter-4.1.zip` is `9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e`;
`JetBrainsMono-2.304.zip` is
`6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf`.
Unlike htmx's 0BSD text, the two OFL 1.1 texts in `LICENSES/` are a licence
condition, not inventory completeness: OFL 1.1 requires them to accompany the
font files wherever those files are redistributed.

## Audited `dwarf2json` artifact provenance

The following facts document the upstream official Linux artifact audited for
this release-boundary review. The binary itself is not shipped:

- upstream version: `0.9.0`;
- upstream commit: `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`;
- commit date: `2024-10-08T23:53:08Z`;
- upstream source: <https://github.com/volatilityfoundation/dwarf2json>;
- official v0.9.0 Linux release asset:
  <https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64>;
- build metadata: `go1.18.10`, `linux/amd64`, `vcs.modified=false`;
- direct dependency: `github.com/spf13/pflag v1.0.5`;
- audited official binary SHA-256:
  `e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`.

The official release asset was independently downloaded on 2026-08-04 and its
SHA-256 matched the audited binary value above.

This provenance is documentation, not a bundled sidecar. It is not an
automatic allow-list. An operator may configure a different `dwarf2json`
artifact only after reviewing its license and provenance and recording its
exact SHA-256 in AstinusLab configuration and release evidence.
