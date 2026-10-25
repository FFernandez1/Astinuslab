# Linux Symbol Tables and ISFs for Volatility 3 — Obtain, Generate, Validate, Install, Verify

```text
Research verified as of: 2026-08-24

Checked:
- Volatility 3:        2.28.0 (tag v2.28.0, commit d61335e2a54961bfd619fe6cd7f7b27f01481604,
                       released 2026-04-30; develop branch also inspected @ 2026-08-19)
- ISF schema:          6.3.0 highest shipped; 6.2.0 is what today's converters emit
                       (schemas 0.1.0, 2.0.0, 2.1.0, 4.0.0, 4.1.0, 6.0.0, 6.1.0, 6.2.0, 6.3.0 ship)
- dwarf2json:          0.9.0 (release binary, sha256 e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b;
                       master source HEAD 9f14607e0d, 2024-10-08) — output schema 6.2.0
- BTF-to-ISF tooling:  vobst/btf2json 0.1.0 (master HEAD 4a64f9c322, 2024-12-21) — THIRD PARTY
                       eurecom-s3/lemon-btf2json (main HEAD 211005007e, 2026-05-07) — THIRD PARTY
                       Nissen96/vol3-profiler (main HEAD 7364750c39, 2026-08-03) — THIRD PARTY wrapper
- bpftool:             v7.7.0, libbpf v1.7 (local invocation)
- pahole / dwarves:    v1.31 latest tag (acmel/dwarves, pushed 2026-08-03); NOT installed locally,
                       kernel requirement is PAHOLE_VERSION >= 122 (v1.22)
- binutils:            2.42 (objcopy/readelf/nm/strings behaviour tested locally)
- Kernel docs:         torvalds/linux master, lib/Kconfig.debug + scripts/gen-btf.sh +
                       scripts/link-vmlinux.sh, fetched 2026-08-24;
                       kernel.org BTF documentation (bpf/btf.html), read 2026-08-24
- Workstation:         Ubuntu 24.04, kernel 7.0.0-28-generic, Python 3.12, jsonschema 4.10.3,
                       gcc 13.3.0, file 5.45, jq 1.7, xz 5.4.5
- Debug kernels used:  Ubuntu linux-image-unsigned-5.4.0-1133-kvm-dbgsym 5.4.0-1133.142 and
                       linux-image-unsigned-7.0.0-30-generic-dbgsym 7.0.0-30.30 (downloaded and
                       converted here), plus a CentOS 6 kernel vmlinux fetched from debuginfod
- pahole:              v1.25 (Ubuntu noble package, unpacked and run locally)
- Not available here:  cargo/rustc, go, root privileges (claims depending on these stay UNVERIFIED)
```

Every factual claim in this guide traces to a row of the evidence log in **Appendix A** (also shipped
separately as `phase1-evidence-log.md`). Rows are cited inline as *(log #N)*.

---

## 0. Invariants — statements the implementation must not contradict

Each row is a claim that is false for the versions in the header above, paired with the behaviour that
was actually observed. They are listed first because each one, if assumed rather than checked, produces
code or a procedure that cannot work. Every row is traceable to an evidence-log entry and, where a
command is involved, to a row of Appendix B.

| False claim | Verified behaviour | Evidence |
|---|---|---|
| "`dwarf2json` has (or might have) a BTF option" | It has none. No BTF flag, no BTF string in the shipped binary, no occurrence of "btf" in the source, and no issue or pull request about BTF has ever been opened in the repository | log #50 |
| "Volatility 3's banner plugin is `linux.banners`" | The plugin is top-level `banners.Banners`. There is no `linux.banners` | log #22 |
| "Volatility 3 supports Linux on AArch64" | Not in 2.28.0 and not on `develop` as of 2026-08-19: `LINUX_ARCHS = ["Intel32", "Intel64"]` and no ARM layer exists in the tree. AArch64 Linux analysis today means a **third-party fork** | log #34, #67 |
| "Symbols must live in `symbols/linux/`" | `linux/` is a convention. Discovery is recursive across all symbol base paths, and a Linux ISF at the top level, in a nested directory, or inside a ZIP was found in testing | log #13, #14 |
| "The Volatility cache directory cannot be changed" (shipped documentation says this) | `--cache-path` exists and works — but it must point at a directory that already exists | log #20 |
| "`.json`, `.json.gz`, `.json.xz` are the compressed forms" (shipped documentation) | `.json.bz2` is also accepted | log #15 |
| "If the ISF fails schema validation, Volatility reports it cleanly" | In 2.28.0 a schema *instance* failure raises `jsonschema.ValidationError`, which `schemas.valid()` does not catch. Under a plugin run it is swallowed by the stacker and only visible at `-vvv`; under `isfinfo --validate` it is an unhandled traceback | log #25, #37 |
| "Volatility validates every ISF it loads" | Only if `jsonschema` is importable. If it is missing, validation returns `True` for everything | log #26 |
| "The Volatility Foundation publishes a comprehensive Linux symbol pack" | The official `linux.zip` contains **four** ISFs (CentOS 2.6.18 and three old Debians), and the README says an exhaustive Linux set cannot be supplied | log #38, #39 |
| "`objcopy --dump-section` is read-only" | Without an output operand `objcopy` rewrites its input file. Always give an explicit output operand | log #99 |

---

## 1. Executive summary

### 1.1 The vocabulary, precisely

These terms are routinely conflated, and every conflation produces a different failure mode.

| Term | What it actually is | What it is **not** |
|---|---|---|
| **Symbol name** | A textual label, e.g. `init_task`, `linux_banner` | Not a type, not a layout |
| **Symbol address** | The address a label resolves to. In `System.map` and in an ELF `.symtab` this is the **link-time** address; in `/proc/kallsyms` it is the **runtime** address after KASLR | Not proof that the structure at that address is understood |
| **Kernel type / structure layout / member offset** | "`struct task_struct` exists, is N bytes long, and its `comm` member sits at offset M" (concrete numbers differ per build). This is what lets Volatility read a structure member out of raw memory | Not derivable from names or addresses |
| **Debugging information (DWARF)** | The compiler's full description of types, members, offsets, enumerations, variables, functions, line numbers. Emitted with `CONFIG_DEBUG_INFO` | Not present in a production `vmlinuz`; usually stripped from shipped `vmlinux`/`.ko` |
| **ELF symbol table (`.symtab`)** | A section inside an ELF listing symbol names, addresses, sizes, sections | Not type information |
| **`System.map`** | A plain-text, three-column file (`address type name`) produced at kernel build time | **Not** a type database; contains no structure layouts (log #47) |
| **kallsyms** | The kernel's *own*, compressed, in-image symbol table; exposed at runtime through `/proc/kallsyms` | Not DWARF; addresses are runtime and privilege/`kptr_restrict` dependent (log #93) |
| **BTF (BPF Type Format)** | A compact, deduplicated type encoding generated from DWARF by `pahole` at build time and shipped inside the kernel image; kernel documentation calls it "the metadata format which encodes the debug info related to BPF program/map" | Not "compressed DWARF"; see §6.5 |
| **Volatility symbol table** | The in-framework object created from an ISF | Not a file format |
| **ISF (Intermediate Symbol File)** | The JSON document Volatility 3 consumes: `metadata`, `base_types`, `user_types`, `enums`, `symbols` | Not a Volatility 2 profile |
| **Volatility 2 profile** *(historical)* | A ZIP containing `module.dwarf` (from `dwarfdump -di module.ko`) and a `System.map`, dropped into `volatility/plugins/overlays/linux/` | Structurally and procedurally unrelated to ISFs (log #103) |

### 1.2 What Volatility 3 actually requires from a Linux ISF

Broken down, and each part verified against 2.28.0:

1. **Type information** — `user_types` (structs/unions/classes with member offsets and sizes), `enums`, `base_types`. Without these, plugins cannot interpret memory at all. Schema-required top-level keys (log #5).
2. **Symbol information** — `symbols`, each with at least an `address` (log #10). The stacker needs at minimum `init_task`, `init_mm`, `init_files`, and one of `init_top_pgt` / `init_level4_pgt` / `swapper_pg_dir` / `pkmap_count` to build the page-table layer and resolve KASLR (log #35).
3. **Constant data — the banner** — the `linux_banner` symbol must carry `constant_data` (base64 of the exact bytes from `.rodata`). This *is* the identifier Volatility matches against the image: `LinuxIdentifier` base64-decodes `symbols.linux_banner.constant_data` and nothing else (log #21). An otherwise perfect ISF with no banner will never be selected automatically.
4. **Metadata** — `format` (`^6.[0-9]+.[0-9]+$` for the modern series) and `producer` (name+version, required from schema 6.3.0), optionally a `linux` block recording where the types and symbols came from (log #5, #6, #8).
5. **Runtime addressing** — nothing in the ISF; Volatility derives the KASLR slide and DTB from the image itself, either from a VMCOREINFO note or by scanning for `init_task` (log #33, #35). The ISF must contain *link-time* addresses for that arithmetic to work.

> **The single most important consequence:** "Volatility needs symbols" is wrong twice over. It needs *types* (which only DWARF, or BTF via third-party tooling, can supply), *addresses* (ELF symtab, `System.map`, or in-memory kallsyms), and *the exact banner bytes* (only recoverable from an ELF `.rodata`, or supplied by hand).

### 1.3 Decision tree

```text
Linux memory image
        |
        v
[1] Determine image format and architecture
    file / vol banners / vol linux.vmcoreinfo
    Upstream Volatility 3 2.28.0 Linux support is x86 / x86-64 ONLY (log #34)
        |
        v
[2] Recover the exact banner string
    vol -f IMAGE banners            (preferred; plugin `banners.Banners`, log #22)
    strings -a IMAGE | grep -m5 '^Linux version'   (fallback)
    vol -f IMAGE linux.vmcoreinfo   (ELF/kdump images: OSRELEASE, KERNELOFFSET, log #33)
        |
        v
[3] Does a matching ISF already exist?
       /                                   \
     yes                                    no
      |                                      |
      v                                      v
 verify identity (§14.4) ------------> [4] Obtain artifacts for that EXACT build
 then use                                    |
                                             +-- (a) ELF vmlinux WITH usable DWARF
                                             |       -> dwarf2json  ** SUPPORTED, UPSTREAM **
                                             |          (§8, Workflow A §32.1)
                                             |
                                             +-- (b) ELF vmlinux with DWARF but a broken/absent
                                             |       symtab
                                             |       -> dwarf2json --elf + --system-map
                                             |
                                             +-- (c) BTF present, no usable DWARF
                                             |       -> THIRD-PARTY btf2json + System.map + banner
                                             |          Requires relaxing Volatility's schema.
                                             |          Symbol *types* come from an embedded
                                             |          database, not from BTF (log #63).
                                             |          (§9, Workflow B §32.2)
                                             |
                                             +-- (d) System.map only
                                             |       -> addresses only; NO types, NO banner bytes.
                                             |          Insufficient on its own (log #47)
                                             |
                                             +-- (e) vmlinuz / bzImage only
                                             |       -> extract-vmlinux, then INSPECT the result:
                                             |          typically .BTF present, .symtab absent,
                                             |          DWARF absent (log #97) -> branch (c) or (f)
                                             |
                                             +-- (f) custom / vendor / embedded kernel
                                                     -> recover the original build tree and vmlinux,
                                                        or rebuild bit-for-bit; document the caveats
                                                        (§16)
        |
        v
[5] Install, validate, and prove correspondence (§21, §23, §24)
        |
        v
[6] Test in stages; one working plugin proves very little (§24)
```

---

## 2. Volatility 3's Linux symbol architecture

### 2.1 From file on disk to answered plugin

For a Linux image on Volatility 3 2.28.0, the chain is:

1. **`SymbolCacheMagic`** (automagic, priority 0) runs `SqliteCache.update()`. It enumerates every candidate ISF under every symbol base path — recursively, through `.json`, `.json.xz`, `.json.gz`, `.json.bz2`, and through ZIP archives — opens each, extracts an identifier, and records `(location, identifier, operating_system, hash, counts, local, cached)` into `~/.cache/volatility3/identifier.cache` (log #11–#18).
2. **Stacking.** `LinuxIntelVMCOREINFOStacker` (order 34) then `LinuxIntelStacker` (order 35) each pull the Linux identifier dictionary out of that cache and scan the physical layer for any of those banner byte strings (log #33, #35).
3. On a hit, the stacker builds a `LinuxKernelIntermedSymbols` table from the matching ISF URL, computes the KASLR shifts, converts the page-global-directory symbol to a physical address, and constructs an `Intel`/`Intel32e`/`IntelPAE` layer with `kernel_virtual_offset` set.
4. **`LinuxSymbolFinder`** (automagic, priority 40) satisfies any remaining `SymbolTableRequirement` by rescanning for banners and constructing the table, including setting `symbol_mask` from the layer's address mask.
5. Plugins then resolve `kernel!task_struct` and friends through the symbol space.

The practical implications:

* The **file name of an ISF is irrelevant.** Matching is by banner bytes. The docs say the same: "the precise file names don't matter and can be organized under any necessary hierarchy under the symbols directory" (log #14, #41).
* **Two ISFs with the same banner collide.** The cache is keyed by identifier; the last row read wins, and a debug line "Duplicate entry for identifier …" is emitted. In testing, four copies of one ISF (plain, `.xz`, `.gz`, in a ZIP) produced a single `isfinfo` row (log #24).
* **A first run after adding many ISFs is slow** — that is the cache update, and it is resumable (README, log #39).

### 2.2 What an ISF looks like

A minimal Linux ISF, shaped exactly like the real thing. **The structure and the key names are
verified against `schema-6.2.0.json` / `schema-6.3.0.json` in 2.28.0 (log #5, #6, #10) and against real
dwarf2json 0.9.0 output (log #108); the numeric values, hashes and offsets below are illustrative
placeholders, not measurements from any particular kernel.** A document of this shape (with the
`system-map` entry omitted or present) validated `True` in testing (log #7, baseline case).

```json
{
  "metadata": {
    "format": "6.2.0",
    "producer": { "name": "dwarf2json", "version": "0.9.0" },
    "linux": {
      "types":   [ { "kind": "dwarf",      "name": "vmlinux-<release>",
                     "hash_type": "sha256", "hash_value": "<sha256 of that vmlinux>" } ],
      "symbols": [ { "kind": "dwarf",      "name": "vmlinux-<release>",
                     "hash_type": "sha256", "hash_value": "<sha256 of that vmlinux>" },
                   { "kind": "symtab",     "name": "vmlinux-<release>",
                     "hash_type": "sha256", "hash_value": "<sha256 of that vmlinux>" },
                   { "kind": "system-map", "name": "System.map-<release>",
                     "hash_type": "sha256", "hash_value": "<sha256 of that System.map>" } ]
    }
  },
  "base_types": {
    "char": { "size": 1, "signed": true, "kind": "char", "endian": "little" },
    "pointer": { "size": 8, "signed": false, "kind": "int", "endian": "little" }
  },
  "user_types": {
    "task_struct": {
      "kind": "struct",
      "size": 0000,
      "fields": {
        "pid":  { "offset": 0000, "type": { "kind": "base", "name": "int" } },
        "comm": { "offset": 0000, "type": { "kind": "array", "count": 16,
                                            "subtype": { "kind": "base", "name": "char" } } }
      }
    }
  },
  "enums": {
    "pid_type": { "size": 4, "base": "unsigned int",
                  "constants": { "PIDTYPE_PID": 0, "PIDTYPE_TGID": 1 } }
  },
  "symbols": {
    "init_task":    { "address": 18446744071000000000,
                      "type": { "kind": "struct", "name": "task_struct" } },
    "linux_banner": { "address": 18446744071000000000,
                      "type": { "kind": "array", "count": 0,
                                "subtype": { "kind": "base", "name": "char" } },
                      "constant_data": "<base64 of the exact linux_banner bytes, including the\n                                        trailing newline and NUL>" }
  }
}
```

Field notes, all taken from `schema-6.3.0.json` and the 6.2.0 schema in 2.28.0 (log #5, #6, #10):

* The five top-level keys are **exactly** `metadata`, `base_types`, `user_types`, `enums`, `symbols`. All five are required and `additionalProperties` is `false` — you cannot add your own top-level key and stay valid.
* `metadata.linux` (and `metadata.mac`) is *provenance only*: which files the types and symbols came from, with `kind` restricted to `dwarf`, `symtab`, `system-map`, and `hash_type` restricted to `sha256`. It holds **no type definitions**. Confusing this provenance block with the containers that hold the data (`user_types`, `enums`, `base_types`, `symbols`) is a common misreading.
* From schema **6.3.0**, `producer` is mandatory in every metadata variant and `producer.version` must match `^[0-9]+.[0-9]+.[0-9]+$` (log #8).
* `symbols.<name>.address` is the only required per-symbol field; `type`, `linkage_name` and `constant_data` are optional. `constant_data` is base64.

### 2.3 Why "same kernel version" is not the same as "compatible"

`uname -r` names an ABI, not a build. Two builds that both call themselves `6.8.0-101-generic` can differ in `.config`, compiler version, patch level, structure randomisation, hardening options and therefore in member offsets. Volatility's own documentation is explicit that the banner must match exactly, "not just the version number … can include elements such as the compilation time and even the version of gcc used for the compilation" (log #41). The banner encodes builder host, compiler and linker versions, build number and build date — which is why it is used as the identity, and why a same-version-different-build ISF is rejected by design rather than silently misused.

---

## 3. Historical: Volatility 2 profiles (do not apply any of this to Volatility 3)

> **HISTORICAL — DEPRECATED.** The Volatility 2 repository is **archived and read-only** (last push
> 2025-05-16, GPL-2.0) (log #103). Nothing in this section is a Volatility 3 procedure.

Volatility 2 identified a Linux kernel by a *profile*: a ZIP file placed under
`volatility/plugins/overlays/linux/`, containing

* `module.dwarf` — a text dump of DWARF for a tiny purpose-built kernel module, and
* a copy of the target's `System.map`.

The module was built against the target's kernel headers by `tools/linux/Makefile`, whose recipe is
(verbatim from the archived repository, log #103):

```make
dwarf: module.c
	$(MAKE) -C $(KDIR)/lib/modules/$(KVER)/build CONFIG_DEBUG_INFO=y M="$(PWD)" modules
	dwarfdump -di module.ko > module.dwarf
	$(MAKE) -C $(KDIR)/lib/modules/$(KVER)/build M="$(PWD)" clean
```

Why this must not be carried over to Volatility 3:

| Volatility 2 profile | Volatility 3 ISF |
|---|---|
| ZIP containing `module.dwarf` (text) + `System.map` | JSON document (optionally `.xz`/`.gz`/`.bz2`, optionally inside a ZIP) |
| Built by compiling a kernel module **on a machine with matching kernel headers** | Built by converting the target build's DWARF (or third-party tooling from its BTF) — no compilation against headers |
| Requires `dwarfdump` (libdwarf) | Requires `dwarf2json` (log #42–#51) |
| Selected by profile name on the command line (`--profile=LinuxUbuntu1204x64`) | Selected automatically by exact `linux_banner` byte match (log #21, #41) |
| Lives in `volatility/plugins/overlays/linux/` | Lives anywhere under a symbol base path (log #11, #14) |

The Volatility 3 documentation states the same break plainly: "Volatility 3 no longer uses profiles"
(`doc/source/vol2to3.rst`, 2.28.0). A `module.dwarf` ZIP is not readable by Volatility 3 in any way; a
ZIP is only searched for *ISF JSON members* (log #16).

Volatility 2 ZIP profiles are still useful as *evidence about a kernel* (they contain a `System.map`),
but converting one into an ISF is not a supported path.

---

## 4. Kernel artifacts, one at a time

### 4.1 `vmlinux` — the linked ELF kernel

* **What it is:** the statically linked ELF image of the kernel produced by the build, before it is
  compressed and wrapped into a boot image.
* **What it may contain:** `.symtab` (link-time symbol addresses), `.debug_*` sections (DWARF) if the
  build enabled `CONFIG_DEBUG_INFO`, `.BTF` and `.BTF_ids` if `CONFIG_DEBUG_INFO_BTF=y` (log #88, #89),
  `.rodata` (which holds `linux_banner`), a GNU build ID.
* **What it may equally *not* contain:** any of the above. A `vmlinux` may have DWARF and no BTF, BTF
  and no DWARF, both, or neither — and may be stripped of `.symtab` entirely. The `vmlinux` recovered
  from a boot image in testing had `.BTF` and `.BTF_ids` but no `.symtab` and no DWARF (log #97).
* **Where investigators overestimate it:** "I have a vmlinux, so I have debug symbols." Check, don't
  assume (§5.4).

### 4.2 `vmlinuz` / `bzImage` — the compressed boot image

* **What it is:** a self-decompressing boot wrapper around a compressed copy of (a stripped) `vmlinux`.
* **Nothing is "restored" by decompressing it.** Extraction gives you back exactly the ELF the
  distribution chose to compress — which for every mainstream distribution is stripped of DWARF.
* **What you *can* get from it:** the banner string, `.BTF` (on any kernel built with
  `CONFIG_DEBUG_INFO_BTF=y`, which is now the norm), section layout, a build ID.
* **What you cannot get:** DWARF. Ever. It was never in there.
* **Verified example** (log #97): Debian installer kernel for `6.1.0-50-amd64`, 8,222,656 bytes.
  `extract-vmlinux` produced a 65,905,060-byte ELF whose sections were `.notes .BTF .BTF_ids .data
  .orc_unwind_ip … .bss .brk .init.scratch .shstrtab` — no `.symtab`, no `.debug_*`; `nm` reported
  "no symbols"; `dwarf2json linux --elf` failed.
* **Rebuilding a symbol table from the embedded kallsyms blob:** `vmlinux-to-elf`
  (github.com/marin-m/vmlinux-to-elf, GPL-3.0, last push 2026-06-05, log #104) parses the kernel's own
  compressed kallsyms table and writes an ELF with a reconstructed symbol table. That recovers
  *names and addresses* — the equivalent of a `System.map` — and **not** type information. It cannot
  make `dwarf2json` work.

### 4.3 `System.map`

* Three columns: `address type name`, e.g. `ffffffff81000000 T _stext`.
* Produced at build time by `nm` over `vmlinux`; ships in `/boot/System.map-<release>` (Ubuntu installs
  it mode **0600**, log #76, #94) and inside Debian's `-dbg` package under `/usr/lib/debug/boot/`
  (log #77).
* **It is a symbol/address source and nothing else.** No structure definitions, no member offsets, no
  sizes, no `constant_data`. An ISF built from `--system-map` alone contains symbols typed `void` and
  no banner (verified, log #47).
* **Is it mandatory for dwarf2json?** No (log #48). `--elf` alone produced a complete, banner-carrying
  ISF in testing. `System.map` is *situational*: use it when the ELF has no usable `.symtab`, or when
  you have reason to trust the map over the ELF (its addresses take precedence — log #49).

### 4.4 `/proc/kallsyms`

* Runtime view of the kernel's symbol table, including symbols for loaded modules — which a build-time
  `System.map` does not have.
* **Not a drop-in `System.map`:**
  * addresses are **runtime** addresses, i.e. already KASLR-shifted, whereas an ISF is expected to carry
    link-time addresses;
  * `kernel.kptr_restrict` censors them. On the workstation used here (`kptr_restrict = 1`, non-root)
    every address read as `0000000000000000` while the names remained visible (log #93);
  * it only exists on a live system.
* It is nonetheless *the* fallback for symbol addresses on systems with no `System.map` (Android
  workflows do exactly this, §18) — captured as root, with `kptr_restrict` set to 0, and with the KASLR
  offset understood.

### 4.5 The kernel's exported BTF file (`/sys/kernel/btf/vmlinux`)

* **A raw BTF blob, not an ELF.** Verified on the live workstation: 7,114,296 bytes, mode 0444,
  `file` reports `data`, the first eight bytes are `9f eb 01 00 18 00 00 00` (BTF magic `0xeb9f`,
  version 1, header length 0x18), and `readelf -h` fails with "Not an ELF file — it has the wrong magic
  bytes at the start" (log #92).
* Per-module BTF is exposed alongside it as `/sys/kernel/btf/<module>` (split BTF, log #88).
* **It exists only while the system is running.** It cannot be recovered from a powered-off disk image,
  and it is not present in a memory image in a form any released Volatility tool extracts (btf2json's
  `--image` option is an unimplemented stub, log #59).

### 4.6 Separate debug files

Distributions ship the debug `vmlinux` in a separate package (§15). Two flavours exist in the wild:

* a **full debug `vmlinux`** (Ubuntu `/usr/lib/debug/boot/vmlinux-<release>`, Debian
  `/usr/lib/debug/boot/vmlinux-<release>`, Fedora/RHEL family
  `/usr/lib/debug/lib/modules/<release>/vmlinux`) — this is what you want (log #76, #77, #79);
* **split DWARF (`.dwo`)** from `CONFIG_DEBUG_INFO_SPLIT=y` — note that this option is *incompatible*
  with `CONFIG_DEBUG_INFO_BTF` (log #87, #88). No mainstream distribution ships kernels this way, and
  `dwarf2json` reads DWARF from the ELF it is given; it has no `.dwo` handling (log #45).

### 4.7 `.ko` kernel modules

* ELF relocatable objects. Distribution modules are commonly compressed (`.ko.zst` on Ubuntu).
* Verified content of a shipped Ubuntu module (log #100): `.modinfo`, `.symtab`, `.BTF` (split BTF
  against the kernel's base BTF), a GNU build ID — and **no `.debug_*`**. The matching debug modules
  live in the debug package under `/usr/lib/debug/lib/modules/<release>/…`.
* See §20 for what Volatility does and does not do with module metadata.

---

## 5. DWARF

### 5.1 What it is and where it comes from

DWARF is the compiler-emitted description of the program: types, structure members and their byte
offsets, unions, enumerations, typedefs, variables, functions, and line-number tables. For the kernel it
is produced by compiling with `-g` under the `CONFIG_DEBUG_INFO` family of options.

Relevant kernel configuration (from `lib/Kconfig.debug`, torvalds/linux master, fetched 2026-08-24,
log #87):

| Option | Effect |
|---|---|
| `DEBUG_INFO_NONE` | No debug info at all (the default for many vendor builds) |
| `DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT` | Whatever DWARF version the toolchain emits |
| `DEBUG_INFO_DWARF4` | DWARF v4 |
| `DEBUG_INFO_DWARF5` | DWARF v5; "around 15-18% savings in resulting image and debug info section sizes as compared to DWARF Version 4"; the config text explicitly warns "Users may not want to select this config if they rely on tooling that has not yet been updated to support DWARF Version 5" |
| `DEBUG_INFO_REDUCED` | Less type information: "tools that need full debugging information … won't be happy". Bad for Volatility, and it blocks BTF |
| `DEBUG_INFO_SPLIT` | DWARF into separate `.dwo` files; blocks BTF |
| `DEBUG_INFO_COMPRESSED_{NONE,ZLIB,ZSTD}` | Compressed `.debug_*` sections |

The workstation's own distribution kernel is a good example of the modern normal case:
`CONFIG_DEBUG_INFO=y`, `CONFIG_DEBUG_INFO_DWARF5=y`, `CONFIG_DEBUG_INFO_BTF=y`,
`CONFIG_DEBUG_INFO_COMPRESSED_NONE=y` (log #95) — meaning the *build* had DWARF, but the DWARF was
split out into the `-dbgsym` package and is not in `/boot/vmlinuz`.

### 5.2 The sections that matter

`.debug_info`, `.debug_abbrev`, `.debug_str`, `.debug_line`, `.debug_line_str`, `.debug_aranges`,
`.debug_ranges`/`.debug_rnglists`, `.debug_loc`/`.debug_loclists`. `dwarf2json` uses Go's
`debug/dwarf`, driven from `elfFile.DWARF()` (log #46) — so what matters to you is simply whether those
sections are present and parseable.

### 5.3 Verified inspection commands

```bash
VMLINUX="/path/to/vmlinux"

# 1. Is it even an ELF, and is it stripped?
file "$VMLINUX"

# 2. Section inventory - the cheapest, most informative check.
readelf -S "$VMLINUX" | awk '{print $2}' | grep -E '^\.(debug|BTF|symtab)' | sort -u

# 3. Symbol table present?  (prints "no symbols" when absent)
nm "$VMLINUX" 2>&1 | head -3

# 4. Build ID (strong corroborating evidence; see §11.5 and §12.3)
readelf -n "$VMLINUX" | grep -A1 -i 'build id'
```

Expected output on a distribution **debug** `vmlinux`: a list including `.debug_info`, `.debug_abbrev`,
`.debug_str`, plus `.symtab` and usually `.BTF`.
Expected output on a `vmlinux` extracted from `/boot/vmlinuz`: `.BTF` and `.BTF_ids` only, with `nm`
reporting "no symbols" (log #97).

> **Warning — enormous output.** `readelf --debug-dump=info "$VMLINUX"` and
> `objdump --dwarf=info "$VMLINUX"` on a kernel produce **gigabytes** of text and can run for many
> minutes. If you must look, bound it hard:
>
> ```bash
> readelf --debug-dump=info "$VMLINUX" 2>/dev/null | head -n 100
> ```
>
> (`head` closing the pipe will terminate the producer.) Never redirect the unbounded form into a file
> on a small filesystem.

A cheap size check that tells you almost as much:

```bash
readelf -S -W "$VMLINUX" | awk '/\.debug_info/ {print "debug_info size (hex bytes):", $7}'
```

### 5.4 Deciding "is there usable DWARF here?"

The only answer that matters is the converter's answer. It costs one command:

```bash
VMLINUX="/path/to/vmlinux"
if dwarf2json linux --elf-types "$VMLINUX" > /dev/null; then
    echo "DWARF is present and parseable by dwarf2json"
else
    echo "No usable DWARF (see the error above)"
fi
```

The failure text you will see for a DWARF-less ELF is, verbatim (log #52):

```text
Failed linux processing: could not get DWARF from <file>: decoding dwarf section info at offset 0x0: too short
```

---

## 6. BTF

### 6.1 What BTF is for

Kernel documentation (kernel.org, `bpf/btf.html`, read 2026-08-24): "BTF (BPF Type Format) is the
metadata format which encodes the debug info related to BPF program/map"; the information "is used for
map pretty print, function signature, etc." It encodes integers, floats, structs, unions, enums (32-
and 64-bit), typedefs, qualifiers (const/volatile/restrict), pointers, arrays, function prototypes,
functions, variables with linkage, data sections, and forward declarations. It exists so that BPF
programs can be **CO-RE** (Compile Once – Run Everywhere): a program compiled against one kernel's types
is relocated at load time against the running kernel's BTF.

### 6.2 How BTF is produced

Not by the compiler — by `pahole`, from the DWARF, during the link stage. From
`scripts/gen-btf.sh` (torvalds/linux master, log #89), which states the pipeline in its own header
comment:

```text
#   1. pahole generates BTF from DWARF data
#   2. resolve_btfids applies kernel-specific btf2btf
#      transformations and computes data for .BTF_ids section
#   3. the result gets linked/objcopied into the target binary
```

and implements it as:

```sh
${PAHOLE} -J ${PAHOLE_FLAGS} ${BTF_BASE:+--btf_base ${BTF_BASE}} \
          --btf_encode_detached=${btf1} "${ELF_FILE}"
${RESOLVE_BTFIDS} ... --btf ${btf1} "${ELF_FILE}"
...
${OBJCOPY} --add-section .BTF=${ELF_FILE}.BTF --set-section-flags .BTF=alloc,readonly ${btf_data}
```

Consequences worth internalising:

* **BTF is derived from DWARF.** If the build had no DWARF, it has no BTF either.
* `CONFIG_DEBUG_INFO_BTF` requires `PAHOLE_VERSION >= 122` (pahole v1.22+), `BPF_SYSCALL`, and is
  incompatible with `DEBUG_INFO_SPLIT` and `DEBUG_INFO_REDUCED` (log #88).
* Modules get **split BTF** (`CONFIG_DEBUG_INFO_BTF_MODULES`, default y): `pahole --btf_base vmlinux`
  produces a delta that is `objcopy --add-section`-ed **into the module file itself** (log #89). Note
  that this in-tree script edits the module in place — see the objcopy warning in §26.6.

### 6.3 Where BTF lives

| Form | Where | How to recognise it |
|---|---|---|
| ELF section | `.BTF` (+ `.BTF_ids`) inside `vmlinux` or an extracted boot image | `readelf -S` shows `.BTF` |
| Split ELF section | `.BTF` (+ `.BTF.base` on newer kernels) inside a `.ko` | `readelf -S` on the decompressed module (log #100) |
| Raw blob | `/sys/kernel/btf/vmlinux` on a live system | magic `9f eb 01 00`, `file` says `data` (log #92) |
| Raw blob (module) | `/sys/kernel/btf/<module>` | same magic; only meaningful with `-B/--base-btf` |

### 6.4 The five questions, answered with evidence

**Is BTF required for Volatility 3?**
**No.** Nothing in 2.28.0 reads BTF, requires BTF, or mentions BTF. The upstream path is DWARF →
`dwarf2json` → ISF (log #39, #50, #70).

**Does stock Volatility 3 consume BTF directly as a Linux symbol table?**
**No.** Volatility loads ISF JSON only (log #5, #13). There is no BTF loader.

**Can BTF be converted into an ISF, and by whom?**
Yes, partially, by **third-party** tools: `vobst/btf2json` and the `eurecom-s3/lemon-btf2json`
derivative (log #56–#64). Neither is a Volatility Foundation project.

**Do those tools require changes to Volatility for the checked release?**
**Yes.** Their output declares `metadata.linux.types[].kind = "btf"` and
`metadata.linux.symbols[].kind = "symdb"`, neither of which the stock 6.2.0/6.3.0 schema permits
(log #6, #7, #64, #65). The schema pattern must be relaxed. The AArch64 variant additionally requires a
forked Volatility that understands its injected `__lemon_va_bits` symbol (log #66, #67).

**What must still come from elsewhere?**
Symbol **addresses** (a `System.map` or a `/proc/kallsyms` capture) and the **banner** text — BTF
contains neither. And, less obviously, the *type of each symbol* also does not come from BTF in
btf2json: `add_types_from_btf()` is a stub that logs "Types from BTF not implemented"; symbol types come
from a 239,380-line database compiled into the binary (log #62, #63).

### 6.5 BTF is not "compressed DWARF"

They are different in kind, not merely in size:

* **Purpose.** DWARF describes a program for a debugger — including where variables live at each
  instruction, how to unwind, and which source line produced which byte. BTF describes *types* so the
  BPF verifier and CO-RE relocation can work.
* **Deduplication.** BTF is deduplicated by construction; DWARF is per-compilation-unit and repeats
  types constantly.
* **What BTF structurally does not express:** source line/file information for ordinary kernel code
  (BTF line info exists only for BPF programs), location expressions, lexical scopes and block
  structure, call-frame/unwind information, macro information, and per-CU compiler provenance. It also
  does not carry the *values* of read-only data — so it cannot give you `linux_banner`'s bytes.
* **What BTF does express well, and is sufficient for:** struct/union layouts, member offsets and
  bitfields, enum constants, typedef chains, function prototypes, and global variable types.

That last line is why a BTF→ISF converter is possible at all — and the preceding line is why it needs a
`System.map` and a banner from somewhere else.

---

## 7. DWARF vs BTF for memory forensics

| Dimension | DWARF | BTF |
|---|---|---|
| Primary purpose | General debugging (types, locations, lines, unwind) | BPF verification and CO-RE type relocation |
| Typical size in a kernel build | Hundreds of MB in the debug `vmlinux`; the Ubuntu `-dbgsym` **package** for one flavour is ≈290 MB compressed and the Rocky `kernel-debuginfo` RPM ≈739 MB (log #74, #80) | ≈4.3 MB `.BTF` in the extracted Debian 6.1 kernel; 7.1 MB for the running Ubuntu 7.0 kernel (log #92, #98) |
| Kernel type coverage | Complete for the build, including types never referenced by BPF | Very good but *deduplicated and BPF-oriented*; some constructs are normalised away |
| Structure layouts / member offsets | Yes | Yes |
| Enumerations | Yes | Yes |
| Function and variable **type** information | Yes | Yes |
| Global symbol **addresses** | Yes — but from the ELF `.symtab`, not from DWARF itself | **No.** Must come from `System.map` or kallsyms |
| Read-only data values (e.g. `linux_banner` bytes) | Not from DWARF either — `dwarf2json` reads them out of the ELF `.rodata` (log #47) | **No.** Must be supplied with `--banner` or read from an ELF that still has a symbol table |
| Runtime exposure | None. DWARF is never in RAM | `/sys/kernel/btf/vmlinux` on a live system (log #92) |
| Generation path | Compiler (`-g`, `CONFIG_DEBUG_INFO`) | `pahole -J` from DWARF at link time (log #89) |
| `pahole` involvement | None | Mandatory; kernel requires ≥ v1.22 (log #88) |
| Upstream Volatility tooling | **Yes — `dwarf2json`, by the Volatility Foundation** (log #42) | **No — none** (log #50, #70) |
| Converter compatibility with stock Volatility 2.28.0 | Passes schema validation as produced (log #7 baseline, #108) | Fails schema validation; requires patching the schema (log #65) |
| Build/runtime requirements to convert | Go 1.18 to build, or a published binary; large RAM for big kernels (log #51, #53) | Rust toolchain (no published binaries verified for the fork), plus a `System.map` and a banner |
| Major limitations | Debug package must exist and still be downloadable | Symbol types come from the converter's embedded database, not from BTF (log #63); third-party, low activity |

**Do not read this table as "two comparable options."** For the versions checked, one is the supported
upstream workflow and the other is a third-party workaround for the case where the supported one is
impossible.

---

## 8. `dwarf2json` — the upstream converter

### 8.1 Identity

| Property | Value | Evidence |
|---|---|---|
| Repository | `https://github.com/volatilityfoundation/dwarf2json` | log #42 |
| Owner | Volatility Foundation (**this is the upstream, first-party tool**) | log #42 |
| Licence | Volatility Software License 1.0 (`LICENSE.txt`) | log #42 |
| Latest release | **v0.9.0**, published 2024-10-09 | log #43 |
| Default branch / HEAD examined | `master`, commit `9f14607e0d` (2024-10-08); repo last pushed 2024-10-09 | log #42 |
| Prebuilt binaries | `dwarf2json-linux-amd64`, `dwarf2json-darwin-amd64`, `dwarf2json-windows-amd64.exe` attached to the release. **No arm64 builds** | log #43 |
| Build requirements | Go ≥ 1.18, one dependency (`github.com/spf13/pflag v1.0.5`); a single 1,206-line `main.go` | log #51 |
| Tool/schema version | `dwarf2json 0.9.0`, `output schema 6.2.0` | log #44 |
| OS modes | `linux`, `mac` (no Windows mode — Windows ISFs come from PDBs via Volatility's own `pdbconv.py`) | log #45 |

Note the dates: as of 2026-08-24 the converter has had **no commits for ~22 months**. It works, it is
still the supported path, but do not expect fixes for new kernel/toolchain constructs to appear quickly
(open issues #65 and #67, log #55).

### 8.2 Build and install

```bash
# Option A - use the published binary (linux/amd64)
curl -fsSLO https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64
sha256sum dwarf2json-linux-amd64      # record this in your case notes
chmod +x dwarf2json-linux-amd64
sudo install -m 0755 dwarf2json-linux-amd64 /usr/local/bin/dwarf2json   # sudo: writing to /usr/local/bin

# Option B - build from source (needs Go >= 1.18)
git clone https://github.com/volatilityfoundation/dwarf2json
cd dwarf2json
git rev-parse HEAD                    # record the commit in your case notes
go build                              # produces ./dwarf2json
./dwarf2json --version
```

The binary used for every measurement in this guide reported:

```text
dwarf2json 0.9.0
output schema 6.2.0
```

### 8.3 Every verified option

`dwarf2json linux --help` (v0.9.0, log #45) — this is the complete list; there are no hidden Linux
options:

```text
Usage: dwarf2json linux [OPTIONS]

      --elf PATH           ELF file PATH to extract symbol and type information
      --elf-symbols PATH   ELF file PATH to extract only symbol information
      --elf-types PATH     ELF file PATH to extract only type information
      --system-map PATH    System.Map file PATH to extract symbol information
```

Top level: `-h/--help`, `-v/--version`. The `mac` subcommand takes `--arch`, `--macho`,
`--macho-symbols`, `--macho-types`.

What each one actually does, from the `Extract` bit flags in `main.go` and confirmed by running the
tool (log #46, #47):

| Option | Reads | Contributes types? | Contributes symbols? | Contributes constant data (banner)? | `metadata.linux` entry | When it is the right choice |
|---|---|---|---|---|---|---|
| `--elf PATH` | DWARF **and** ELF `.symtab` **and** `.rodata` | **Yes** | **Yes** | **Yes** | `types:[dwarf]`, `symbols:[dwarf, symtab]` | The default. A distribution debug `vmlinux` |
| `--elf-types PATH` | DWARF only | **Yes** | No (0 symbols in testing) | No | `types:[dwarf]` | Types from one file, symbols from another (e.g. types from a debug ELF, addresses from a `System.map`) |
| `--elf-symbols PATH` | ELF `.symtab` + `.rodata` only | No | **Yes** | **Yes** | `symbols:[symtab]` | Getting addresses *and the banner* out of a non-debug ELF that still has a symbol table |
| `--system-map PATH` | Text map | No | **Yes** (typed `void` when the symbol is otherwise unknown) | **No** | `symbols:[system-map]` | Repairing/overriding addresses; never sufficient alone |

All four options are repeatable (`pflag.StringArray`), so several inputs of the same kind can be merged.

**Precedence.** Inputs are queued in the fixed order `--elf-types`, `--elf`, `--elf-symbols`,
`--system-map`, and later writes overwrite `sym.Address`. Therefore **`--system-map` addresses win over
ELF symtab addresses** for any symbol present in both. Verified: with a doctored map in which every
symbol was `ffffffff81000000`, the combined `--elf --system-map` run produced
`18446744071578845184` (= `0xffffffff81000000`) for every symbol (log #49).

### 8.4 How `linux_banner` reaches the ISF (and why it can be missing)

In `main.go`, `constantLinuxDataSymbols = []string{"linux_banner"}` — exactly one symbol is treated as
constant data. `readELFSymbol()` locates the symbol inside an **allocated `.rodata` section** and copies
`symbol.Size` bytes from it. That code runs only under `processElfSymTab()`, which is reached only from
`--elf` and `--elf-symbols` (log #47).

Consequences:

* An ISF built from `--elf-types` + `--system-map` has **no banner** and will never be selected
  automatically by Volatility (log #21, #47). It can still be loaded manually by a developer, but the
  normal workflow will silently ignore it.
* The stored bytes include the trailing newline and the terminating NUL, because those are inside the
  symbol's size. A real dwarf2json 0.9.0 ISF for Ubuntu `6.8.0-101-generic` stores 260 bytes ending
  `…6.8.12)\n\x00` (log #109); a locally built test ELF produced
  `b'Linux version 9.9.9-test … #1 SMP TEST\n\x00'` (log #46/#47 test).
* **A one-byte failure, reproduced and explained.** Two ISFs on this workstation, both claiming the
  same `5.4.0-1133-kvm` `vmlinux`, stored different banners: 187 bytes ending `\n\x00` and 186 bytes
  ending `)\x00`. Re-running dwarf2json 0.9.0 against the genuine debug package settled it (log #117,
  #118): the tool emits **187 bytes ending `\n\x00`**, and the regenerated file was byte-identical to
  one of the two. The other differed **only** in `symbols.linux_banner.constant_data` — every one of
  its 109,467 symbols, 7,515 `user_types`, enums, base types and metadata hashes matched exactly — so
  it had been post-processed after generation, with the trailing newline stripped.
  That single missing byte is fatal, and it was demonstrated end to end (log #119): against a synthetic
  image containing the true in-memory bytes, the correct ISF produced
  `Identified banner: b'…5.4.291)\n\x00'` followed by `Using symbol library: …`, while the
  newline-stripped ISF produced only `No suitable linux banner could be matched` — despite being valid
  JSON, schema-valid, and listed by `isfinfo`.
  The lesson is operational: **verify the banner bytes in your ISF against the banner found in the
  image**, byte for byte (§23.7).

### 8.5 Typical invocations

```bash
VMLINUX="/path/to/usr/lib/debug/boot/vmlinux-6.8.0-101-generic"
SYSTEM_MAP="/path/to/boot/System.map-6.8.0-101-generic"
ISF_OUT="/path/to/case/symbols/6.8.0-101-generic.json"

# (a) The normal case: everything from one debug vmlinux.
if dwarf2json linux --elf "$VMLINUX" > "$ISF_OUT"; then
    jq empty "$ISF_OUT" || { echo "invalid JSON produced" >&2; exit 1; }
else
    echo "dwarf2json failed" >&2
    rm -f "$ISF_OUT"          # a failed run leaves a 0-byte file behind
    exit 1
fi

# (b) Belt and braces: DWARF from the debug ELF, addresses re-asserted from the System.map.
if dwarf2json linux --elf "$VMLINUX" --system-map "$SYSTEM_MAP" > "$ISF_OUT"; then
    jq empty "$ISF_OUT" || { echo "invalid JSON produced" >&2; exit 1; }
else
    echo "dwarf2json failed" >&2; rm -f "$ISF_OUT"; exit 1
fi

# (c) Types from a debug ELF, symbols+banner from the non-debug ELF that shipped on the box.
#     (Only valid if both came from the SAME build - see section 12.)
DEBUG_ELF="/path/to/vmlinux-with-dwarf"
SHIPPED_ELF="/path/to/vmlinux-from-the-target"
if dwarf2json linux --elf-types "$DEBUG_ELF" --elf-symbols "$SHIPPED_ELF" > "$ISF_OUT"; then
    jq empty "$ISF_OUT" || { echo "invalid JSON produced" >&2; exit 1; }
else
    echo "dwarf2json failed" >&2; rm -f "$ISF_OUT"; exit 1
fi
```

> **Why the exit-status check is not optional.** A failed `dwarf2json` run still creates the redirect
> target. In testing, both the "no DWARF" and the "no symbol section" failures left a **0-byte**
> `.json` file behind (log #52). Without the check you would go on to install an empty ISF and spend an
> hour debugging Volatility.

### 8.6 Practical cost — measured on real debug kernels

All figures below are from this research environment on 2026-08-24 (8 cores, 31 GB RAM), using the
dwarf2json 0.9.0 release binary and `/usr/bin/time -v`. Inputs are genuine distribution debug kernels
(log #113–#116).

| Input | `.debug_info` | Command | Wall | Peak RSS | Output JSON | user_types / symbols |
|---|---|---|---|---|---|---|
| `vmlinux-5.4.0-1133-kvm` (529,409,144 B) | 162,931,047 B | `linux --elf` | **18.97 s** | **2,836,656 KB ≈ 2.71 GiB** | 26,874,845 B | 7,515 / 109,467 |
| same | same | `linux --elf-types` | 15.75 s | 2,520,320 KB ≈ 2.40 GiB | 8,938,347 B | 7,515 / 0 |
| same | same | `linux --elf-symbols` | **1.10 s** | **78,136 KB ≈ 76 MiB** | 12,119,796 B | 0 / 82,134 |
| same | same | `linux --elf --system-map` | 18.42 s | 2,840,220 KB ≈ 2.71 GiB | 26,875,069 B | 7,515 / 109,467 |
| `vmlinux-7.0.0-30-generic` (505,823,072 B) | 314,673,188 B | `linux --elf` | **36.38 s** | **5,514,468 KB ≈ 5.26 GiB** | 77,485,996 B | 15,517 / 360,733 |
| `vmlinux-2.6.32-754.24.2.el6` (139,554,079 B, fetched via debuginfod) | — | `linux --elf` | 8.23 s | 1,464,096 KB ≈ 1.40 GiB | 19,606,273 B | 4,678 / 82,268 |

Read the shape of it, not just the numbers:

* **Peak RSS scales with DWARF size, roughly 16–18× `.debug_info`.** 163 MB of `.debug_info` cost
  2.7 GiB; 315 MB cost 5.3 GiB. A modern "everything enabled" distribution kernel with 600 MB+ of
  DWARF should be budgeted at **8–12 GiB**, and the reports of failures on small machines (issue #60,
  log #53) are entirely consistent with that.
* **`--elf-symbols` is nearly free** — 1.1 s and 76 MiB, because it never touches DWARF. If all you
  need is addresses plus the banner, do not pay for a DWARF parse.
* **`--system-map` costs nothing extra** (18.42 s vs 18.97 s): the map is parsed after the ELF.
* Output is 5–15 % of the debug `vmlinux`'s size and compresses ≈19:1 with `xz` (log #54).

**Reproducibility.** The `--elf` run against `vmlinux-5.4.0-1133-kvm` produced a file **byte-identical**
(sha256 `4e723b6db49faf7ac69516410b720d6e19cd528783e05b753f7a14e0ff3c243a`) to an ISF for that kernel
that already existed on this workstation, generated independently earlier by the same tool version
(log #117). dwarf2json 0.9.0 is deterministic for a given input, which is what makes an ISF hash a
meaningful thing to record in case notes.

**What resource exhaustion looks like.** No run here was OOM-killed (24 GiB was free), so the failure
mode below remains as reported upstream rather than measured (log #53):

* the process disappears with no message and exit status 137 → the OOM killer; check
  `dmesg | tail` or `journalctl -k | grep -i 'killed process'`;
* a Go runtime panic mentioning `runtime: out of memory` or `cannot allocate memory`;
* in every case the redirect target is left **truncated**, and `jq empty` on it fails.

Mitigations: convert on a machine with RAM ≥ 20× the `.debug_info` size, add swap, and convert one
kernel at a time. Do not pass several huge debug kernels to a single invocation.

### 8.7 Known correctness caveats for the checked version

* **Rust/C type confusion (dwarf2json #63, Volatility #1305).** Volatility 2.28.0 actively checks for
  it: if an ISF's producer is `dwarf2json` with version < 0.9.0 **and** the symbol `rust_helper_BUG` is
  present, it logs "This ISF was generated by dwarf2json < 0.9.0, which is known to produce inaccurate
  results". 0.9.0 skips Rust compilation units. Prefer 0.9.0 or later for any kernel with Rust
  components (log #55).
* **Multi-compilation-unit handling (#65)** and **typedef resolution (#67)** remain open upstream as of
  2026-08-24.
* **BTF: not supported, in any form.** No flag, no code, no string in the binary, and not a single
  issue or PR proposing it in the repository's history (log #50).

---

## 9. Third-party BTF-to-ISF tooling

> **THIRD-PARTY / EXPERIMENTAL.** Nothing in this section is produced, endorsed, or supported by the
> Volatility Foundation, and none of it works against an unmodified Volatility 3 2.28.0 (log #56, #65,
> #70). Read §9.5 before using any of it on a real case.

Three projects exist as of 2026-08-24. There is no fourth: a GitHub search for `btf2json` returns
exactly two repositories, and a search for BTF+Volatility returns those plus one wrapper (log #56).

### 9.1 `vobst/btf2json`

| Property | Value | Evidence |
|---|---|---|
| Repository / author | `https://github.com/vobst/btf2json` — Valentin Obst | log #57 |
| Licence | MIT | log #57 |
| Release model | One release, `v0.1.0` (2024-11-10), with pre-compiled x64 Linux binaries; also packaged in the AUR per its README | log #57 |
| Last activity | `master` HEAD `4a64f9c322`, 2024-12-21 (documentation-only). 35 stars, 0 open issues | log #57 |
| Maintenance status | Effectively dormant (~20 months without a code change at the time of checking) | log #57 |
| Language / build | Rust; `cargo install --locked --path .` or `cargo build --release --locked` | README |

**Complete option list** — taken from `src/cli.rs` at `4a64f9c322`, not from prose (log #58):

| Option | Type | Purpose | Status |
|---|---|---|---|
| `--btf <PATH>` | optional path | BTF source: a raw BTF blob **or** an ELF containing `.BTF` | Implemented (log #61) |
| `--map <PATH>` | optional path | `System.map` — symbol names and addresses | Implemented; in practice **mandatory** (log #60) |
| `--banner <STRING>` | optional string | The Linux banner. Help text: "Mandatory if using a BTF file for type information. Takes precedence over all other possible sources" | Implemented |
| `--version` | flag | Print version | Implemented |
| `--verbose` | flag | Debug output | Implemented |
| `--debug` | flag | More debug output | Implemented |
| `--image <PATH>` | optional path | "Memory image to extract type and/or symbol information from **(not implemented)**" | **NOT SUPPORTED** — both code paths `bail!()` (log #59) |

**Required vs optional in practice.** `SymbolsBuilder::try_from(&Cli)` accepts `--map`, or `--image`
(which immediately fails), or nothing (which fails). So: `--btf` **and** `--map` are required, and
`--banner` is required whenever the `--btf` input cannot yield a banner — which is the usual case,
because banner extraction reads the ELF **symbol table** (`elf::get_banner` iterates `elf.syms` looking
for `linux_banner`), and a `vmlinux` extracted from a boot image has no symbol table at all (log #60,
#61, #97). A raw `/sys/kernel/btf/vmlinux` blob obviously has no symbols either.

**The embedded symbol-to-type database (`symdb`).** This is the part most write-ups omit:

* **The problem it solves.** An ISF entry for a symbol carries an optional `type`. BTF describes types
  but does not tell you that the global named `init_task` is a `task_struct`; a `System.map` gives the
  name and address but no type. Something has to join them.
* **How btf2json solves it.** It ships `symdb/dummy.symdb`, a 239,380-line file of
  `"<symbol_name> <ISF type descriptor JSON>"` pairs, embedded into the executable at compile time via
  `rust-embed`. At run time it walks the database and attaches a type to every symbol whose name it
  recognises (log #62).
* **It is compiled in, not user-supplied.** There is no flag to point at a different symdb; changing it
  means editing the file and rebuilding.
* **How its coverage goes stale.** The database was generated once, from some set of kernels. Symbols
  that did not exist then, were renamed since, or whose type changed name will either get no type or
  the wrong type for your kernel. This degrades silently: the ISF still loads, and a plugin that
  depends on the affected symbol either fails oddly or produces wrong values.
* **BTF is not used for symbol types at all.** `add_types_from_btf()` is a stub whose entire body is
  `log::info!("Types from BTF not implemented."); self` (log #63). BTF supplies `user_types`, `enums`
  and `base_types`; the symdb supplies per-symbol types; the `System.map` supplies addresses; `--banner`
  supplies the identity.

**Output.** `metadata.format = "6.2.0"`, `producer.name = "btf2json"`, and source kinds `btf`, `symdb`,
`system-map` (log #64).

### 9.2 `eurecom-s3/lemon-btf2json`

| Property | Value | Evidence |
|---|---|---|
| Repository | `https://github.com/eurecom-s3/lemon-btf2json` | log #66 |
| Relationship | Source-level derivative of `vobst/btf2json` (not registered as a GitHub fork); identical `symdb` | log #66 |
| Licence | MIT | log #66 |
| Last activity | `main` HEAD `211005007e`, 2026-05-07; no releases | log #66 |
| Purpose | Android/AArch64 memory forensics together with `eurecom-s3/lemon` (an eBPF-based memory dump tool, GPL-2.0) | README |

Differences from upstream `btf2json`, verified by diffing the source trees (log #66):

* adds a **required** `--vabits <u8>` option ("The VA Bits number (obtainable from lemon output)") — it
  is a non-optional field, so every invocation must pass it;
* injects a synthetic symbol `__lemon_va_bits` into the ISF carrying that value;
* adds `"long unsigned int"` to the base types it synthesises.

`__lemon_va_bits` has **no consumer in upstream Volatility**. The project's README directs users to
`eurecom-s3/volatility3-arm64` ("Volatility 3.0 with ARM64 support"), i.e. this workflow requires a
**forked framework**, not merely a relaxed schema (log #67).

Documentation defect worth knowing before you follow the README: it tells you an expected error
"is normal and corrected by `patch_profile.py`", but no such script exists in the repository (log #68).

### 9.3 `Nissen96/vol3-profiler`

| Property | Value | Evidence |
|---|---|---|
| Repository | `https://github.com/Nissen96/vol3-profiler` | log #69 |
| Licence | `pyproject.toml` says MIT; **no LICENSE file** and GitHub reports no licence | log #69 |
| Last activity | `main` HEAD `7364750c39`, 2026-08-03 | log #69 |
| What it is | A Python (≥3.12) orchestration wrapper. It is **not** a converter | log #69 |

Verified behaviour from `main.py` (log #69): scan the dump for the banner → map the banner to an
Ubuntu/Debian APT suite → download `linux-image` and `System.map`-carrying packages → find the
compressed payload inside `vmlinuz` by scanning for gzip/xz/lz4/zstd magic and decompress it → run
`btf2json --btf <vmlinux> --map <System.map> --banner "<banner>"` → `xz` the result.

Its CLI, from its own `--help` text: `vol3-profiler [-h] [-o OUT_DIR] [-c CACHE_DIR] [-f] [--no-cache] dump`.
Supported targets per its README: Ubuntu 24.04/22.04/20.04/18.04-HWE and Debian 10/11/12, kernels ≥ 4.18.

**It does not patch Volatility's schema**, so its output has exactly the same acceptance problem as
plain btf2json output on a stock 2.28.0 (log #65, #69).

### 9.4 What these tools do *not* prove

The presence of a flag is not the presence of a feature. Specifically:

* `--image` exists in both Rust tools and does nothing but abort (log #59).
* `add_types_from_btf()` exists and does nothing (log #63).
* A successful conversion run is not evidence that Volatility will accept, select, or correctly use the
  result. Those are four separate facts (§23.8).

### 9.5 Making the output loadable — and the chain-of-custody consequences

**Historical workaround (as published by the tool, 2024-11-10).** `vobst/btf2json`'s README ships this
patch:

```diff
--- a/volatility3/schemas/schema-6.2.0.json
+++ b/volatility3/schemas/schema-6.2.0.json
@@ -105,7 +105,7 @@
       "properties": {
         "kind": {
           "type": "string",
-          "pattern": "^(dwarf|symtab|system-map)$"
+          "pattern": "^(btf|symdb|dwarf|symtab|system-map)$"
```

`eurecom-s3/lemon-btf2json` ships the identical change as `utilities/btf_support.patch` (log #66).

**Behaviour verified for the checked release (Volatility 3 2.28.0, jsonschema 4.10.3, 2026-08-24).**

* Unpatched, an ISF declaring `kind: "btf"`/`"symdb"` fails validation with
  `jsonschema.exceptions.ValidationError: {...} is not valid under any of the given schemas` (log #7).
* Under `isfinfo --validate` that exception is **not caught** and you get a traceback (log #25).
* Under a plugin run it is raised inside the stacker, caught by `automagic/stacker.py`, and logged only
  at `-vvv`/`-vvvv`; the user-visible result is the generic "A symbol table requirement was not
  fulfilled" message (log #25, #36, #37).
* The tools emit `format: "6.2.0"`, so the file to patch in 2.28.0 is
  `volatility3/schemas/schema-6.2.0.json`. The same `kind` restriction exists in `schema-6.3.0.json`
  (log #6), so a future converter emitting 6.3.0 would need the same change there.
* **Caching subtlety:** successful validations are cached in
  `~/.cache/volatility3/valid_isf.hashcache`, keyed by a hash of *(document, schema)*. Changing the
  schema changes the key, so patching or un-patching does not leave stale "valid" results behind
  (log #18) — but `--clear-cache` after any schema edit is still the safe habit.

**Recommended procedure.** In order of preference:

1. Find or generate a DWARF-based ISF instead (§8, §14, §15). This is the supported path.
2. If BTF is genuinely the only option, do the work in a **dedicated, disposable analysis environment**
   with a pinned, recorded Volatility checkout — never in the tooling you use for other cases.
3. Record, in the case notes, at minimum: the Volatility version and commit, the exact patch applied
   (the diff above), the converter's repository URL and commit, the `symdb` name and hash as recorded
   in the ISF's own metadata, the `System.map` and BTF hashes, and the full command lines with their
   stdout/stderr.
4. Treat every result as corroborating rather than primary, and cross-check anything that matters
   against a second, independent source (in-image kallsyms via `linux.kallsyms`, VMCOREINFO, strings).

> **Modifying analysis tooling is itself a documentable event.** Relaxing a validation rule inside a
> forensic tool changes what that tool accepts, and therefore changes the meaning of "Volatility loaded
> this symbol table". It must be recorded alongside the tool version in the case notes, and disclosed
> in any report that relies on the output. Patching the schema does not make the ISF *correct* — it only
> stops the framework from rejecting it (§23.8).

---

## 10. Tool comparison

| Dimension | `dwarf2json` | `vobst/btf2json` | `eurecom-s3/lemon-btf2json` | `Nissen96/vol3-profiler` |
|---|---|---|---|---|
| Project owner | **Volatility Foundation** | Valentin Obst (individual) | EURECOM S3 group | Individual |
| Upstream status | **Upstream, referenced by Volatility's own documentation** | Third party | Third party (derivative) | Third party (wrapper) |
| Licence | Volatility Software License 1.0 | MIT | MIT | MIT claimed in `pyproject.toml`; no LICENSE file |
| Latest / HEAD checked | v0.9.0 · `9f14607e0d` (2024-10-08) | v0.1.0 · `4a64f9c322` (2024-12-21) | `211005007e` (2026-05-07) | `7364750c39` (2026-08-03) |
| Input metadata: DWARF | **Yes** | No | No | via btf2json |
| Input metadata: BTF | **No** (log #50) | Yes (`.BTF` section or raw blob) | Yes | via btf2json |
| ELF `.symtab` handling | Yes (`--elf`, `--elf-symbols`) | Only to find the banner; not for addresses | Same | — |
| `System.map` handling | Optional (`--system-map`), overrides ELF addresses | **Required in practice** (`--map`) | Required (`--map`; Android workflow feeds it `/proc/kallsyms`) | Downloads it for you |
| Constant data (`linux_banner` bytes) | Extracted from ELF `.rodata` automatically | Only via `--banner`, unless the input ELF still has a symtab | Same | Passes `--banner` |
| Per-symbol types | From DWARF | From a compiled-in 239,380-line `symdb` | Same | — |
| Output format | ISF 6.2.0 | ISF 6.2.0 with `kind: btf/symdb` | Same, plus `__lemon_va_bits` | Same as btf2json, `.json.xz` |
| Accepted by stock Volatility 2.28.0? | **Yes** | **No — schema patch required** | **No — schema patch *and* a forked framework** | **No — same as btf2json** |
| Architectures | Whatever the DWARF describes; Volatility itself is x86-only for Linux (log #34) | x86-64 in practice | Targets AArch64/Android via the ARM64 fork | amd64 Ubuntu/Debian |
| Maturity | Mature, in production use since 2019 | Proof-of-concept quality, explicit stubs | Research prototype | Convenience automation |
| Maintenance | Slow but alive (Foundation-owned) | Dormant | Sporadic | Active but young |
| Patching Volatility required? | No | Yes | Yes (more than schema) | Yes (inherited) |
| Main limitations | Needs a debug `vmlinux`; heavy RAM use | Symbol types from a stale-able DB; no banner without help; `--image` inert | All of the above plus fork dependency | Distro coverage; no schema handling |
| Recommended use | **Default for every case where a debug kernel can be obtained** | Last resort when no DWARF exists anywhere, with explicit caveats in the report | Android/ARM64 research | Speeding up the Ubuntu/Debian BTF route |

These are **not** peers. One is the supported upstream workflow; the others are third-party workarounds
whose output the stock framework rejects by design.

---

## 11. Identifying the kernel behind the image

### 11.1 First: what *is* this file?

```bash
MEMORY="/path/to/memory.raw"

file "$MEMORY"
ls -l "$MEMORY"
sha256sum "$MEMORY"        # slow on large images - see section 31
```

What the answer means for Volatility 2.28.0 (log #31, #32):

| `file` says | Layer | Notes |
|---|---|---|
| `data` (and the size is ~RAM-sized) | raw/padded | The common case for `dd`, LiME `format=raw`, VM `.vmem` |
| `data`, but starts with `EMiL` | LiME | `lime.py`, magic `0x4C694D45` |
| `data`, but starts with `AVML` | AVML | version **2** only |
| `ELF 64-bit LSB core file` | ELF64 core | kdump/`virsh dump`/VirtualBox core; may carry VMCOREINFO |
| `Zstandard`/`lzo`-compressed, or "Kdump compressed dump" | **not supported** | `makedumpfile`-compressed dumps are not handled at all; re-acquire or convert with `makedumpfile --dump-dmesg`/`-R` outside Volatility |
| VMware `.vmss`/`.vmsn` beside a `.vmem` | VMware | point `-f` at the `.vmss`/`.vmsn` |

### 11.2 Architecture

There is no architecture-detection plugin. Determine it from context and content:

* the banner text itself (`x86_64-linux-gnu-gcc`, `aarch64`, `armv7l`, the flavour name);
* VMCOREINFO (`OSRELEASE`, and on x86 `NUMBER(phys_base)`, `CONFIG_X86_PAE`);
* the acquisition notes.

Then apply the hard constraint: **Volatility 3 2.28.0 supports Linux on Intel32 and Intel64 only**
(`LINUX_ARCHS = ["Intel32", "Intel64"]`, no ARM layer in the tree, on the release *and* on `develop` at
2026-08-19 — log #34). If the image is AArch64, no amount of ISF work will make upstream plugins run;
you are in third-party-fork territory (§9.2, §18).

### 11.3 The banner — the only identity that matters

**Preferred: the built-in plugin.** It is `banners.Banners` (top level; there is no `linux.banners`)
and it needs no symbols:

```bash
python3 vol.py -f "$MEMORY" banners
```

Real output from a controlled test (log #22):

```text
Volatility 3 Framework 2.28.0

Offset	Banner

0x100000	Linux version 9.9.9-test (builder@example) (gcc 13.3.0) #1 SMP TEST
```

The plugin scans the *physical* layer with the regular expression
`(Linux version|Darwin Kernel Version) [0-9]+\.[0-9]+\.[0-9]+`, reads up to 0xFFF bytes, truncates at
the first NUL, and rejects candidates containing characters outside a printable whitelist (log #22).
Two consequences: the printed banner has trailing whitespace stripped, and a banner containing an
unusual character (some vendor kernels) may be filtered out even though it is there.

**Fallback: `strings`.** Useful when the image is not in a supported format, or when the plugin finds
nothing:

```bash
strings -a -t d "$MEMORY" | grep -m 20 'Linux version ' 
```

> **Warning — output size.** Without `-m`/`head`, `strings` over a multi-gigabyte image produces
> enormous output. Always bound it.

Expect duplicates and near-duplicates. On the extracted Debian kernel used for testing there were two
banner-like strings that differ only in the build-number field: one containing `# SMP …` at offset
18,874,784 and the real one containing `#1 SMP …` at 20,321,248 (log #110). The one that matters is the
one that matches the running kernel's `uname -v` form — and ultimately, the one whose bytes equal the
`linux_banner` bytes in your ISF.

**Structured images: VMCOREINFO.** For ELF/kdump-style images, Volatility can read the VMCOREINFO note
without any symbols at all (log #33):

```bash
python3 vol.py -f "$MEMORY" linux.vmcoreinfo
```

It prints key/value pairs including `OSRELEASE` (the exact `uname -r`), `KERNELOFFSET` (the KASLR
offset), `NUMBER(phys_base)`, `SYMBOL(swapper_pg_dir)`, `CONFIG_X86_PAE`, and more. This is gold: it
gives you the release string, tells you the image is x86, and hands the KASLR slide to the framework
(§13.3).

### 11.4 Reading the banner

```text
Linux version 6.8.0-101-generic (buildd@lcy02-amd64-051) (x86_64-linux-gnu-gcc-13 (Ubuntu 13.3.0-6ubuntu2~24.04) 13.3.0, GNU ld (GNU Binutils for Ubuntu) 2.42) #101-Ubuntu SMP PREEMPT_DYNAMIC Mon Feb  9 10:15:05 UTC 2026 (Ubuntu 6.8.0-101.101-generic 6.8.12)
```

(This is a real banner, from an ISF on the workstation — log #109.)

| Fragment | Meaning | Use |
|---|---|---|
| `6.8.0-101-generic` | release (`uname -r`): version + ABI number + **flavour** | Names the package you need, including the flavour (`generic`, `kvm`, `aws`, `lowlatency`, `generic-64k`…) |
| `buildd@lcy02-amd64-051` | build host | Distinguishes distribution builds from local rebuilds |
| `x86_64-linux-gnu-gcc-13 … 13.3.0, GNU ld … 2.42` | toolchain | Architecture confirmation, and a strong hint that a self-rebuild will not match |
| `#101-Ubuntu SMP PREEMPT_DYNAMIC …` | build number + date (`uname -v`) | Together with the release this pins the exact build |
| `(Ubuntu 6.8.0-101.101-generic 6.8.12)` | **package version** and upstream base | This is what you type into the archive URL |

> **`uname` is about the source machine, never the analyst's machine.**
> `uname -r` on the analysis workstation says nothing about the kernel in someone else's memory image.
> `uname -r`, `uname -a`, `uname -v` are useful only when run **on the target** at acquisition time
> (§30), or when reasoning about artifacts you collected from that target.

### 11.5 Build ID as corroboration

A `vmlinux` normally carries a GNU build ID:

```bash
readelf -n "$VMLINUX" | grep -A1 -i 'build id'
```

Fedora's kernel packaging even records it into the package as `vmlinux.id` (log #79). Debian's `-dbg`
package exposes `/usr/lib/debug/.build-id/xx/yyyy….debug` (log #77), and `debuginfod` is keyed on build
IDs entirely (log #86).

But be precise about its role: **Volatility 3 2.28.0 does not match ISFs by build ID.** Its identity
mechanism is the `linux_banner` bytes and nothing else (log #21). Build ID is excellent *corroborating*
evidence that two artifacts came from the same build — use it when checking that a downloaded debug
`vmlinux` corresponds to the kernel in the image, not as the thing Volatility keys on.

---

## 12. Exact-match requirements

### 12.1 What must correspond

| Property | Must match | Why |
|---|---|---|
| Architecture and word size | Yes | Determines layer class, pointer size, base type sizes |
| Endianness | Yes | Encoded per base type in the ISF |
| Kernel release (`uname -r`) | Yes | Necessary, nowhere near sufficient |
| Distribution patch set | Yes | Distribution patches add and reorder structure members |
| Package revision (e.g. `6.8.0-101.101`) | Yes | A rebuild of the same release changes layout and addresses |
| `.config` | Yes | Options add/remove members (`CONFIG_SCHED_INFO`, `CONFIG_SMP`, LSM stacking, …) |
| Compiler and linker versions | Yes in practice | Alignment/padding and layout decisions can differ; the banner encodes them for a reason |
| `CONFIG_LOCALVERSION` / local version suffix | Yes | Part of the release string and the banner |
| Generated headers (`autoconf.h`, `bounds.h`, `asm-offsets`) | Yes | Derived directly from the `.config` |
| Structure layouts and member offsets | Yes | This *is* the thing being matched |
| Symbol addresses | Yes (link-time) | KASLR arithmetic starts from them |
| Hardening/layout options: `RANDSTRUCT`, LTO, CFI, `CONFIG_DEBUG_INFO_REDUCED` | Yes | `RANDSTRUCT` randomises member order per build seed; LTO/CFI change symbol sets and addresses |
| Build ID | Should match | Strong corroboration; not the framework's matching key (§11.5) |

### 12.2 Why identical `uname -r` is evidence, not proof

Two independently produced binaries with the same release string are the same only if every input to
the build was the same. The distribution guarantees that for *its* published build; nothing guarantees
it for a rebuild, a vendor respin, a `-rt`/`-cloud`/`-64k` flavour variant, or a kernel built with a
different `pahole`/gcc. The banner captures enough of those inputs (compiler, linker, build number,
build date, package version) that byte-equality of the banner is a far stronger statement than equality
of `uname -r` — which is precisely why Volatility matches on it (log #41).

### 12.3 Proving that your artifacts belong to the image

Do all four, in this order — each is cheap:

1. **Banner equality.** Extract the banner from the image (§11.3) and from the candidate ISF, and
   compare bytes (§23.7).
2. **Package identity.** The banner's parenthesised package version tells you which archive file to
   fetch; keep the URL and the file's own hash.
3. **Build ID.** Compare the build ID of the debug `vmlinux` against the kernel package it came from
   (Fedora's `vmlinux.id`, Debian's `.build-id` path, or the `linux-image` package's own `vmlinux`).
4. **Behaviour.** Run the staged tests in §24. Two independent structure families agreeing is worth
   more than any single check.

### 12.4 A note on `RANDSTRUCT`

If the target kernel was built with GCC `RANDSTRUCT` (or the clang equivalent), member order in many
structures is randomised using a per-build seed. Two builds from identical source with different seeds
produce incompatible layouts, and there is no way to recover the seed from a memory image. In that
situation only the original build's `vmlinux` will do; note that `CONFIG_DEBUG_INFO_BTF` is
incompatible with the GCC randstruct plugin unless `COMPILE_TEST` is set (log #88), which is a useful
tell.

---

## 13. KASLR and addressing

### 13.1 Link-time vs runtime addresses

An ISF carries **link-time** addresses — the values `nm`/`System.map` show, e.g.
`ffffffff81000000`. At boot, with `CONFIG_RANDOMIZE_BASE=y` (the default everywhere; verified on the
workstation kernel, log #95), the kernel is relocated by a random slide. Volatility must therefore learn
two numbers:

* **`aslr_shift`** — the *virtual* slide to add to ISF addresses;
* **`kaslr_shift`** — the *physical* offset used to convert a virtual kernel address into a file offset.

Both are set on the constructed layer as `kernel_virtual_offset` / `page_map_offset` (log #33, #35).

### 13.2 The scanning route (`LinuxIntelStacker`)

With no structured metadata, Volatility derives the slide from the image itself (log #35):

1. scan for the byte pattern `swapper(/0|\x00\x00)\x00\x00\x00\x00\x00\x00` — the `comm` field of the
   idle task;
2. back up by the ISF's offset of `task_struct.comm` to land on a candidate `init_task`;
3. reject candidates whose `pid != 0`, whose `state != 0`, or that fail the
   `active_mm == init_mm && tasks.next == tasks.prev` test — this specifically catches "a fragment of
   the unrelocated ELF file instead of the running kernel" (comment in the source);
4. compute `aslr_shift` from `init_task.files` minus the ISF address of `init_files`, and
   `kaslr_shift` from where the structure was found;
5. reject either value if it is not page-aligned;
6. if nothing survives: "Scanners could not determine any ASLR shifts, using 0 for both".

Notice how much of this depends on the ISF being *right*: the offset of `comm`, the offsets of `pid`,
`state`, `active_mm`, `tasks`, `files`, and the addresses of `init_mm`/`init_files`. A wrong ISF
usually fails here — which is why a wrong ISF and a KASLR problem look alike from the outside.

### 13.3 The VMCOREINFO route (`LinuxIntelVMCOREINFOStacker`)

If the image carries a VMCOREINFO ELF note, Volatility prefers it (stack order 34, ahead of 35) and
computes (log #33):

* `aslr_shift = KERNELOFFSET`
* `kaslr_shift = NUMBER(phys_base) + KERNELOFFSET`
* DTB from `SYMBOL(swapper_pg_dir)` converted to physical
* 32-bit/PAE selection from `CONFIG_X86_PAE` and the size of the `swapper_pg_dir` address
* it then narrows the banner candidate list using `Linux version <OSRELEASE> (`, and still requires a
  banner match against a known ISF.

Requirements and limits, from the source: `NUMBER(phys_base)` must be present (absent in kernels
< 4.10, where a `SYMBOL(phys_base)` exists but "isn't useful"), and `KERNELOFFSET` must be present
(added in kernel 3.14). If either is missing the stacker declines and the scanning route is tried.

### 13.4 Telling "correct ISF + KASLR handled" apart from "wrong ISF"

| Observation | Correct ISF, KASLR resolved | Wrong ISF |
|---|---|---|
| `-vvv` log line `Linux ASLR shift values determined: physical … virtual …` | Present, both page-aligned, non-trivial values | Usually absent, or "using 0 for both" |
| Banner match log line `Identified banner: b'Linux version …'` | Present and equals the image's banner | May still be present (the banner matched) while everything downstream is wrong — only possible if you installed an ISF whose banner is right but whose types are not (e.g. a hand-edited or mismatched file) |
| `linux.pslist` | A plausible process tree with `systemd`/`init` at PID 1 | Empty, or garbage names, or absurd PIDs |
| `linux.lsmod`/`linux.check_modules` | Coherent module names | Empty or nonsense |
| Repeated runs | Deterministic | Often deterministic too — determinism proves nothing |

The practical rule: **a banner match tells you the ISF claims to be the right build; only structure
agreement tells you it is.**

---

## 14. Where to look for an existing ISF before generating anything

### 14.1 Your own symbol directories

```bash
# What does this installation already know about?
python3 vol.py isfinfo

# Everything on disk, not just what the cache knows, filtered, with schema validation:
python3 vol.py isfinfo --live --validate --filter linux
```

Search paths for 2.28.0, in order (log #11, #12):

1. every directory given to `-s/--symbol-dirs` (semicolon-separated), then
2. `<volatility3 package>/symbols`,
3. `<volatility3 package>/framework/symbols`,
4. `$XDG_CACHE_HOME/volatility3/symbols` (or `~/.cache/volatility3/symbols`) — described in the source
   as the "user cache fallback for automatically downloaded temporary symbols".

There is **no environment variable** for symbol directories (the only environment variables the
framework reads are `XDG_CACHE_HOME` and, on Windows, `APPDATA`) (log #12, #18).

### 14.2 Official Volatility Foundation packs

The README of 2.28.0 links three packs hosted in `volatilityfoundation/volatility3-test-data` at tag
`v0.0.1`, with `SHA256SUMS`/`SHA1SUMS`/`MD5SUMS` alongside (log #38, #39).

Measured reality for Linux (log #38):

```text
linux.zip = 2,980,184 bytes
sha256      58bb7da2ed1e491ce922d04a59881d201e233b5605c9fd5a7f0c08ee528253c6
contents    linux/Centos_2.6.18-8.1.15.el5_2.6.18-8.1.15.el5_x64.json.xz
            linux/Debian_2.6.32-48squeeze6_2.6.32-5-amd64_x64.json.xz
            linux/Debian_3.2.57-3+deb7u2_3.2.0-4-amd64_x64.json.xz
            linux/Debian_4.9.30-2+deb9u2_4.9.0-3-amd64_x64.json.xz
```

Four ISFs, all from 2024-11-07, all ancient kernels. **This is a test-data pack, not a symbol
service.** The README says so in its own words: "Due to the ease of compiling Linux kernels and the
inability to uniquely distinguish them, an exhaustive set of Linux symbol tables cannot easily be
supplied" (log #39).

### 14.3 Remote ISF retrieval (`--remote-isf-url`)

Volatility 3 2.28.0 *does* have a remote mechanism, and it is off by default: `REMOTE_ISF_URL = None`,
i.e. **no server is configured out of the box** (log #27). You point it at a JSON index:

```json
{ "version": 1,
  "linux": { "<base64 of banner bytes>": [ "https://…/Some_Kernel.json.xz" ] },
  "mac":   { "…": [ "…" ] },
  "additional": [ "https://…/another-index.json" ] }
```

Usage, and the one index known to exist publicly (log #28, #71):

```bash
python3 vol.py \
  --remote-isf-url 'https://github.com/Abyss-W4tcher/volatility3-symbols/raw/master/banners/banners.json' \
  -f "$MEMORY" banners
```

Verified end to end on 2026-08-24: that index loaded and inserted **10,988** remote identifiers into
`~/.cache/volatility3/identifier.cache` with `local = 0` (log #28). Remote entries are refreshed subject
to the same three-day cache period as local ones, and `--offline` disables all network access (they are
mutually exclusive options).

Two caveats:

* The shipped generator for such an index, `development/banner_server.py`, is **broken in 2.28.0**: it
  references `automagic.linux.LinuxBannerCache`, which no longer exists, and dies with an
  `AttributeError` (log #30). If you want to host an index for a lab, generate the JSON yourself from
  each ISF's `symbols.linux_banner.constant_data`.
* Remote identifiers are normalised (trailing whitespace stripped, then a trailing `\x00` removed)
  before being cached — a detail that matters if you build your own index (log #29).

### 14.4 Community collections, and how to trust them

| Collection | Coverage | Activity | Licence | Notes |
|---|---|---|---|---|
| `Abyss-W4tcher/volatility3-symbols` | Ubuntu (amd64/i386), Debian, Kali, AlmaLinux, RockyLinux (x86_64), macOS; badge claims 10,988 ISFs; ~29 GB | pushed 2026-07-29 | **None declared** | Recommended by Volatility's own Linux tutorial (log #40). Provides `banners/banners.json` (remote-ISF index) and `banners/banners_plain.json` (plain-text banner → path map). Carries an author warning (2025-03-27) that many macOS ISFs above 11.0 are incorrect (log #71) |
| `leludo84/vol3-linux-profiles` | Linux profiles, ~16 GB | pushed 2026-08-18 | **None declared** | Less documented; 84 stars (log #72) |

Practical lookup without downloading the whole repository:

```bash
BANNER='Linux version 3.2.0-4-amd64 (debian-kernel@lists.debian.org) (gcc version 4.6.3 (Debian 4.6.3-14) ) #1 SMP Debian 3.2.57-3+deb7u2'
curl -fsSLO https://raw.githubusercontent.com/Abyss-W4tcher/volatility3-symbols/master/banners/banners_plain.json
grep -F -A 2 "$BANNER" banners_plain.json
```

**"No licence declared" is not a detail to skip.** Absent an explicit licence, redistribution rights are
undefined; using such files in a case is usually fine, but shipping them onward inside a product or a
report package is a decision to take deliberately.

**Verifying a downloaded ISF is more than "it loads".** Minimum checks, in order:

```bash
ISF="/path/to/case/symbols/candidate.json.xz"

# 1. It is intact and is JSON.
xz -t "$ISF" && xz -dc "$ISF" | jq empty && echo "structurally OK"

# 2. Its banner equals the image's banner, byte for byte.
xz -dc "$ISF" | jq -r '.symbols.linux_banner.constant_data' | base64 -d | xxd | tail -3

# 3. Its provenance says what produced it and from what.
xz -dc "$ISF" | jq '.metadata'

# 4. Record where it came from and what it hashes to.
sha256sum "$ISF"
```

Then run the staged tests in §24 before believing anything it tells you.

---

## 15. Obtaining debug artifacts by distribution

All package names, repository paths and sizes below were checked on **2026-08-24**; see the referenced
log rows. Sizes are for orientation: kernel debug packages are **hundreds of megabytes**.

### 15.1 Ubuntu

Debug symbols live in a separate archive (`ddebs`), not in the normal pools (log #73, #75).

```bash
# Ubuntu: enable the ddebs archive (once).
echo "deb http://ddebs.ubuntu.com $(lsb_release -cs) main restricted universe multiverse
deb http://ddebs.ubuntu.com $(lsb_release -cs)-updates main restricted universe multiverse
deb http://ddebs.ubuntu.com $(lsb_release -cs)-proposed main restricted universe multiverse" |
  sudo tee /etc/apt/sources.list.d/ddebs.list          # sudo: writes to /etc/apt
sudo apt install ubuntu-dbgsym-keyring                  # 18.04 and newer
sudo apt update
```

Package naming (verified in the pool listing, log #73):

```text
linux-image-unsigned-<release>-<flavour>-dbgsym_<pkgversion>_<arch>.ddeb
# e.g. linux-image-unsigned-7.0.0-30-generic-dbgsym_7.0.0-30.30_amd64.ddeb   (304,349,184 bytes)
```

HWE kernels live under their own source package, e.g. `pool/main/l/linux-hwe-6.8/` (log #73). Flavours
seen in the archive include `generic`, `generic-64k`, `lowlatency`, `kvm`, `aws`, `azure`, `gcp`, `oracle`.

Direct download without configuring apt (useful on an offline analysis host):

```bash
DDEB_URL="http://ddebs.ubuntu.com/pool/main/l/linux/linux-image-unsigned-7.0.0-30-generic-dbgsym_7.0.0-30.30_amd64.ddeb"
curl -fsSLO "$DDEB_URL"
sha256sum linux-image-unsigned-7.0.0-30-generic-dbgsym_7.0.0-30.30_amd64.ddeb

# Unpack without installing (dpkg-deb is in dpkg; ar/tar also work)
mkdir -p extracted && dpkg-deb -x linux-image-unsigned-*.ddeb extracted
find extracted -path '*/usr/lib/debug/boot/vmlinux-*'
```

Installed paths — from Ubuntu's kernel packaging rules (log #76) and **confirmed by unpacking the real
packages** (log #120):

```text
# linux-image-unsigned-<release>-<flavour>-dbgsym  (the ddeb)
/usr/lib/debug/boot/vmlinux-<abi_release>-<flavour>      # the debug vmlinux  (VERIFIED)
/usr/lib/debug/lib/modules/<release>/…                   # debug modules

# linux-image-unsigned-<release>-<flavour>  (the ordinary image package)
/boot/vmlinuz-<abi_release>-<flavour>                    # mode 0600, and NOTHING else useful

# linux-modules-<release>-<flavour>   <-- this is where the map and config live
/boot/System.map-<abi_release>-<flavour>                 # mode 0600  (VERIFIED)
/boot/config-<abi_release>-<flavour>                     # mode 0644  (VERIFIED)
/lib/modules/<release>/kernel/…
```

That last point catches people out: on Ubuntu the **`-dbgsym` package does not contain a `System.map`,
and neither does `linux-image`** — it is in `linux-modules`. Verified by unpacking all three packages
for `5.4.0-1133-kvm` (log #120).

**Retention is not guaranteed.** For `6.8.0-101` the pool on 2026-08-24 still had `arm64` and `s390x`
dbgsyms, while the **amd64** one returned HTTP 404 — the exact kernel one of the local ISFs was built
from (log #74). Launchpad's per-source publishing history sometimes still lists removed binaries, but
recovering the file itself is not guaranteed and was not verified here (`UNVERIFIED`).

### 15.2 Debian

Debug symbols for the kernel are a normal package in the main archive (log #77):

```bash
# Debian
sudo apt install linux-image-$(uname -r)-dbg     # only meaningful ON the target machine
```

Package name: `linux-image-<release>-<flavour>-dbg`, e.g.
`linux-image-5.10.0-30-686-dbg_5.10.218-1_i386.deb` in
`https://deb.debian.org/debian/pool/main/l/linux/` (log #77).

Contents (from the package file list, log #77):

```text
/usr/lib/debug/boot/vmlinux-<release>
/usr/lib/debug/boot/System.map-<release>        # Debian ships the map in the dbg package too
/usr/lib/debug/lib/modules/<release>/…
/usr/lib/debug/.build-id/xx/yyyy….debug
```

For kernels no longer in the current suites, `https://snapshot.debian.org/` archives historic pool
contents by date and by package version. Debian also runs `https://debuginfod.debian.net/` (log #86).

> Note: `https://debug.mirrors.debian.org/` could not be reached from the environment used for this
> research (TLS interception produced a self-signed-certificate error). That is a property of this
> sandbox, not of Debian (log #78).

### 15.3 Fedora

```bash
# Fedora (on a Fedora host, for the running kernel)
sudo dnf install dnf-plugins-core
sudo dnf debuginfo-install kernel-core-$(uname -r)
```

Package: `kernel-debuginfo` (plus `kernel-debuginfo-common-<arch>`). Archive location for a released
version, verified for Fedora 43 (log #79):

```text
https://dl.fedoraproject.org/pub/fedora/linux/releases/43/Everything/x86_64/debug/tree/Packages/k/
  kernel-debuginfo-6.17.1-300.fc43.x86_64.rpm
```

Installed path, from Fedora's `kernel.spec` (rawhide, log #79):

```text
/usr/lib/debug/lib/modules/<kernelver>/vmlinux
/lib/modules/<kernelver>/build/vmlinux.id       # the build ID, recorded by the spec
```

Old releases move to `https://archives.fedoraproject.org/pub/archive/fedora/linux/…` — the Volatility
repository's own `development/fedora-kernels.txt` uses exactly those URLs (2.28.0).

Extracting an RPM without installing:

```bash
RPM="/path/to/kernel-debuginfo-6.17.1-300.fc43.x86_64.rpm"
mkdir -p extract && cd extract
rpm2cpio "$RPM" | cpio -idmv        # WARNING: expands to several GB
find . -path '*/usr/lib/debug/lib/modules/*/vmlinux'
```

### 15.4 RHEL, CentOS Stream, Rocky, AlmaLinux

They share Fedora's packaging lineage, so the in-package path is the same:
`/usr/lib/debug/lib/modules/<kernelver>/vmlinux`.

* **RHEL** — `kernel-debuginfo` from the `rhel-*-debug-rpms` repositories, which require an active
  subscription. Not verifiable anonymously; treat the exact repository id as `UNVERIFIED` here and take
  it from `subscription-manager repos --list` on the box.
* **Rocky Linux** — verified (log #80):
  `https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/debug/tree/Packages/k/` contains
  `kernel-debuginfo-5.14.0-687.*.el9_8.x86_64.rpm`; example size 775,461,069 bytes.
* **AlmaLinux** — verified (log #81): the *current* `9/BaseOS/x86_64/` tree exposes only `kickstart/`
  and `os/`; the debug packages are in the vault, e.g.
  `https://vault.almalinux.org/9.5/BaseOS/debug/x86_64/Packages/` with
  `kernel-debuginfo-5.14.0-503.*.el9_5.x86_64.rpm`.
* **CentOS** — `https://debuginfo.centos.org/` timed out from this environment and could not be checked
  (log #82). CentOS Stream debug content is otherwise mirrored alongside the release trees.

```bash
# Rocky/Alma/CentOS on the host itself
sudo dnf --enablerepo='*debug*' install kernel-debuginfo-$(uname -r)
```

### 15.5 Arch Linux

**Arch does not publish kernel debug symbols.** Verified on 2026-08-24: the `core-debug` and
`extra-debug` repositories exist (172 packages in `core-debug`) but contain **no `linux-debug`
package** — matches such as `util-linux-debug` are unrelated (log #83). Arch does operate
`https://debuginfod.archlinux.org/` (log #86), but with no kernel debug package there is nothing for it
to serve for the kernel itself.

Practical consequence for an Arch target: you are in the BTF branch (§9) or the rebuild branch (§16),
unless the investigator preserved the build.

### 15.6 openSUSE / SLES

Verified (log #84):

```text
https://download.opensuse.org/debug/distribution/leap/15.6/repo/oss/x86_64/
  kernel-default-debuginfo-6.4.0-150600.21.3.x86_64.rpm
https://download.opensuse.org/debug/tumbleweed/repo/oss/x86_64/
  kernel-default-debuginfo-7.1.8-1.1.x86_64.rpm
```

```bash
# openSUSE, on the host
sudo zypper modifyrepo --enable $(zypper lr | awk -F'|' '/[Dd]ebug/ {print $2}' | tr -d ' ')
sudo zypper install kernel-default-debuginfo
```

Flavour matters here too (`default`, `preempt`, `rt`, `azure`, …). **SLES** debug repositories require a
subscription and were not tested (`UNVERIFIED`, log #85).

### 15.7 `debuginfod` as a cross-distribution route

`debuginfod` serves debug artifacts keyed by **build ID** over HTTP. Public servers documented by
elfutils include Debian, Fedora, openSUSE, Ubuntu, Arch, CentOS, plus a federating
`https://debuginfod.elfutils.org/` (log #86).

```bash
export DEBUGINFOD_URLS="https://debuginfod.elfutils.org/"
export DEBUGINFOD_PROGRESS=1
BUILDID="<40 hex characters from readelf -n>"
debuginfod-find debuginfo "$BUILDID"     # prints a path in the local debuginfod cache
```

**Kernel coverage: verified, and it works.** On 2026-08-24 the federating server
`https://debuginfod.elfutils.org/` returned **1,164** metadata hits for the glob
`/usr/lib/debug/lib/modules/*/vmlinux`, each with a build ID and the source RPM, and a live fetch of one
of them delivered a 139,554,079-byte ELF that `file` describes as "with debug_info, not stripped" —
which `dwarf2json linux --elf` then converted successfully into a 19,606,273-byte ISF with 4,678
`user_types` and a valid banner (log #121, #116). So debuginfod **can** deliver kernel debug
information, and the result is usable by the upstream converter.

Without the `debuginfod-find` client installed you can drive the protocol with `curl`, which is also the
easiest way to see what a server actually holds:

```bash
BUILDID="1e817974660c2d2a2ff72c063fc03c38a567338f"      # from readelf -n on the stripped kernel
SRV="https://debuginfod.elfutils.org"

# Metadata search (elfutils >= 0.192 servers): which kernels does this server index?
curl -sS -G --data-urlencode "key=glob" \
     --data-urlencode "value=/usr/lib/debug/lib/modules/*/vmlinux" "$SRV/metadata" | jq '.results | length'

# Fetch the debug ELF for one build ID (headers name the source package)
curl -fSL "$SRV/buildid/$BUILDID/debuginfo" -o vmlinux-from-debuginfod
file vmlinux-from-debuginfod
```

Limits that remain:

* You need the **build ID first** — which normally means you already have the stripped `vmlinux`.
* Coverage is per-server and uneven. The same federating server returned **HTTP 404** for the build IDs
  of the two current Ubuntu kernels used in this research, and `https://debuginfod.ubuntu.com/` was
  unreachable from this environment altogether (log #121) — so "debuginfod serves kernels" does not
  mean "debuginfod serves *your* kernel".
* Downloads are large (139 MB in the example above; modern kernels are several hundred MB).
* Retention mirrors the distribution's own archive policy: what has been deleted from the archive is
  not served.

### 15.8 The uncomfortable truth about availability

Distributions do not keep debug packages forever, and some never publish them at all. Volatility's own
documentation states it: "the distribution may not keep all old versions of the debugging symbols, and
therefore **it may not be possible to find the right symbols to analyze a Linux memory image with
Volatility**" (2.28.0 `symbol-tables.rst`, log #41). The 6.8.0-101 amd64 case in §15.1 is a live example
of exactly that (log #74). This is why §30 (collect at acquisition time) matters more than any other
section in this guide.

---

## 16. Custom and self-built kernels

### 16.1 What has to be identical

To reproduce a build closely enough that its DWARF describes the kernel in the image, you need all of:

* the **exact source tree** — upstream tag *plus* every vendor patch, in the same order;
* the exact `.config` (ideally the target's `/proc/config.gz` or `/boot/config-<release>`);
* `CONFIG_LOCALVERSION` and `LOCALVERSION_AUTO` (the latter appends git information — a moving target);
* the **compiler and binutils versions** named in the banner;
* `pahole` version if `CONFIG_DEBUG_INFO_BTF=y` (kernel requires ≥ 1.22, log #88) — a different pahole
  changes the emitted BTF, though not the DWARF;
* build environment details that leak into the image: build host name and user (they appear in the
  banner), `KBUILD_BUILD_TIMESTAMP`, and any `-ffile-prefix-map` settings;
* debug-info configuration: `CONFIG_DEBUG_INFO=y` and a DWARF version choice, **not**
  `CONFIG_DEBUG_INFO_REDUCED`, **not** `CONFIG_DEBUG_INFO_SPLIT` (log #87).

### 16.2 Why "same version" rebuilds still fail

Even with the same source and `.config`, differences appear in: structure member order (with
`RANDSTRUCT`), padding and alignment (compiler version), inlining and symbol sets (LTO, CFI),
generated headers (`asm-offsets`, `bounds.h`), section placement and therefore every link-time address,
and the banner string itself (build host, user, date, build number). Any one of these is enough to make
the resulting ISF wrong — and a *plausible-looking* wrong, because the banner will not match and
Volatility will simply refuse to use it, or (if you force it) the layouts will be subtly off.

### 16.3 If you control the build (or the investigation is prospective)

Keep, hash and store next to the image:

```text
vmlinux                 (the UNSTRIPPED one, before any packaging step)
System.map
.config
include/generated/autoconf.h
Module.symvers
modules/                (unstripped .ko files)
build log with full compiler command lines
gcc --version, ld --version, pahole --version output
git describe --always --dirty  (or the source tarball hash + patch series)
```

The unstripped `vmlinux` is the one artifact that cannot be reconstructed later. Everything else is a
convenience by comparison.

### 16.4 Generating the ISF from your own build

```bash
VMLINUX="/path/to/build/vmlinux"          # unstripped, from the build tree
SYSTEM_MAP="/path/to/build/System.map"
ISF_OUT="/path/to/case/symbols/custom-kernel.json"

readelf -S "$VMLINUX" | awk '{print $2}' | grep -E '^\.(debug_info|symtab|BTF)$'   # sanity first

if dwarf2json linux --elf "$VMLINUX" --system-map "$SYSTEM_MAP" > "$ISF_OUT"; then
    jq empty "$ISF_OUT" || { echo "invalid JSON produced" >&2; exit 1; }
else
    echo "dwarf2json failed" >&2; rm -f "$ISF_OUT"; exit 1
fi
```

---

## 17. Containers, Kubernetes, and WSL

### 17.1 Containers

A container is not a machine. Docker/Podman/LXC workloads share the **host kernel**; there is one
kernel in the memory image regardless of how many userlands are running on it. Therefore:

* The ISF is determined by the **host** kernel — `uname -r` *inside* a container reports the host
  kernel, but the container's `/etc/os-release`, its package manager and its libc tell you nothing
  about which debug kernel to fetch.
* An Alpine container on an Ubuntu host needs the **Ubuntu** debug kernel.
* Kata Containers, Firecracker microVMs, and gVisor are different: each has its own kernel (or, for
  gVisor, a userspace kernel that Volatility's Linux plugins do not model at all).

### 17.2 Kubernetes

Three distinct layers, and only one of them determines the ISF:

| Layer | What it tells you | Relevance to symbols |
|---|---|---|
| Pod/container filesystem | Application, base image, libraries | None |
| Node OS | Distribution and kernel of the node | **This is the kernel** |
| Node substrate (bare metal, cloud VM, or a VM-based runtime) | How you can acquire memory at all | Determines the acquisition format |

Managed node images (GKE COS, EKS AL2/AL2023, AKS Ubuntu, Bottlerocket, Talos, Flatcar) are minimal,
frequently immutable, and often have **no package manager and no debug kernel available**. For those,
collect kernel artifacts from the node while it is alive (§30) or plan on the BTF route (§9) — most
such images enable `CONFIG_DEBUG_INFO_BTF`, but that is a per-image fact you must verify, not assume.

### 17.3 WSL

* **WSL1** has no Linux kernel at all — it is a syscall translation layer over the NT kernel. There is
  nothing for Volatility's Linux plugins to analyse; a memory image of that machine is a **Windows**
  image.
* **WSL2** runs a real Linux kernel inside a lightweight Hyper-V VM. Two sub-cases:
  * **Microsoft's shipped kernel** — built from `microsoft/WSL2-Linux-Kernel`. Verified against
    `Microsoft/config-wsl` at `master` on 2026-08-24: `CONFIG_KALLSYMS=y`,
    `CONFIG_KALLSYMS_BASE_RELATIVE=y`, `CONFIG_RANDOMIZE_BASE=y`, and **no `CONFIG_DEBUG_INFO*` and no
    `CONFIG_DEBUG_INFO_BTF`** (log #105). That is the difficult combination: no DWARF *and* no BTF in
    the shipped image. Your realistic options are to rebuild that exact tag with `CONFIG_DEBUG_INFO=y`
    (accepting the caveats of §16.2), or to work from kallsyms-derived symbols with no type
    information — which is not enough for Volatility.
  * **A user-supplied kernel** (`.wslconfig` → `kernel=`) — whatever the user built. Ask for the build
    tree.
* Acquiring memory from a WSL2 VM is itself non-trivial and outside this guide's scope; do not assume
  that a Windows-side capture contains the WSL2 guest's RAM in an analysable layout.

Do not claim universal WSL support. State in the report which sub-case you are in.

---

## 18. Android

> Upstream Volatility 3 2.28.0 supports Linux on **Intel32/Intel64 only** (log #34). Practically every
> Android device is AArch64. That single fact governs this section.

* **Android Common Kernel (ACK) / GKI.** Since Android 12, devices shipping kernel 5.10+ must use the
  Generic Kernel Image: a single core kernel binary per architecture per LTS release, with SoC and
  board support pushed out into loadable **vendor modules** behind a stable KMI. Branches are named
  `<android-release>-<kernel-version>`, e.g. `android14-6.1` (log #106).
* **Where the kernel lives.** In the `boot` partition (GKI) with vendor modules in
  `vendor_boot`/`vendor_dlkm`. There is no `/boot/vmlinuz` and usually no `System.map` on the device.
* **What you can usually get from a rooted device or emulator:**
  * `/proc/kallsyms` (with `kptr_restrict` set to 0 as root) — names and **runtime** addresses;
  * `/sys/kernel/btf/vmlinux` — the raw BTF blob (log #92 is the general form of this file);
  * the banner, from the acquisition tool's output or from `/proc/version`.
* **What you usually cannot get:** DWARF. Vendor kernels are shipped stripped, and vendor sources —
  when published at all — rarely reproduce the shipped binary.
* **Published GKI build artifacts — better news than expected.** The per-branch release-build pages
  (e.g. `gki-android16-6_12-release-builds`) do enumerate artifact files, and the list includes
  `Image`, `boot.img`, **`System.map`** ("a lookup table between symbols and their addresses in
  memory"), **`vmlinux`** ("a statically-linked executable containing the Linux kernel") and
  `vmlinux.symvers`, with separate *debug kernel* artifacts (`…-allsyms.img` variants); artifacts are
  obtained from ci.android.com (log #123). Two caveats before you rely on it: the pages describe the
  **GKI/ACK** builds, not the vendor's shipped kernel, and whether that published `vmlinux` carries
  DWARF for *your* device's build was not verified here. Check ci.android.com for the exact build.

**Existing work relies on modified tooling.** The one documented end-to-end Android workflow found in
this research (EURECOM's `lemon` + `lemon-btf2json`) requires (log #66, #67):

1. `lemon` (GPL-2.0, eBPF-based) to dump memory on the device and report the banner and VA-bits value;
2. `cat /proc/kallsyms > kallsyms` and `cat /sys/kernel/btf/vmlinux > btf_symb` as root, after
   `echo 0 > /proc/sys/kernel/kptr_restrict`;
3. `btf2json --map kallsyms --btf btf_symb --vabits <N> --banner "<banner>"`;
4. a **forked Volatility** (`eurecom-s3/volatility3-arm64`) with the schema patch applied.

That is four third-party components deep. It is legitimate research tooling; it is not an
upstream-supported workflow, and any report using it must say so, name every commit, and treat results
as needing corroboration.

---

## 19. Embedded Linux

The pattern here is that *nothing is published*: no debug packages, often no `System.map`, sometimes no
source that corresponds to the shipped binary.

| Ecosystem | Where the artifacts are, if they exist |
|---|---|
| **Buildroot** | `output/build/linux-*/vmlinux` (unstripped), `output/build/linux-*/System.map`, `output/build/linux-*/.config`, plus the toolchain in `output/host/` |
| **Yocto / OpenEmbedded** | `tmp/work/<machine>/linux-*/…/build/vmlinux` and the `-dbg` packages in `tmp/deploy/`; `bitbake -e virtual/kernel` recovers the exact configuration |
| **OpenWrt** | `build_dir/target-*/linux-*/vmlinux` in the build tree; images on the device are stripped and heavily size-optimised |
| **Vendor BSPs / forks** | Whatever the vendor kept. Ask for `vmlinux`, `System.map`, `.config`, and the toolchain by name and version |

Additional realities:

* Production kernels are stripped and frequently built with `CONFIG_DEBUG_INFO_NONE` — and, on older or
  size-constrained builds, without BTF as well. That removes both conversion routes.
* Unusual architectures (ARM, AArch64, MIPS, RISC-V, PowerPC) are **outside upstream Volatility 3's
  Linux support** entirely (log #34), so even a perfect ISF would not help with the stock framework.
* Toolchains are often vendor-specific and unavailable, so "just rebuild it" is frequently impossible.

Because of that, retaining build artifacts matters disproportionately here: `vmlinux`, `System.map`,
`.config`, build logs, toolchain versions, separate debug files, and the module tree. If you own the
device fleet, archive these per firmware release; if you do not, request them contractually before an
incident, not during one.

---

## 20. Kernel modules

### 20.1 What a module file contains

Verified on a shipped Ubuntu module (log #100): an ELF **relocatable** object with `.modinfo`
(license, description, srcversion, depends, retpoline, vermagic), `.symtab`, `.BTF` (split BTF against
the kernel's base BTF), a GNU build ID — and **no `.debug_*`**. Distribution modules are compressed
(`.ko.zst` on Ubuntu, `.ko.xz` elsewhere).

```bash
MODULE="/lib/modules/$(uname -r)/kernel/fs/binfmt_misc.ko.zst"   # on a live system only
zstd -dc "$MODULE" > /tmp/mod.ko
file /tmp/mod.ko
readelf -S /tmp/mod.ko | awk '{print $2}' | grep -E '^\.(BTF|symtab|debug|modinfo)'
readelf -p .modinfo /tmp/mod.ko
readelf -n /tmp/mod.ko | grep -A1 -i 'build id'
```

Debug versions of the modules are in the distribution's debug package under
`/usr/lib/debug/lib/modules/<release>/…` (log #76, #77).

### 20.2 What Volatility 3 2.28.0 actually does with modules

Verified against the source (log #101), because this is a common source of wrong expectations:

* **Module symbols are recovered from memory, not from module ISFs.** `linux.kallsyms.Kallsyms`
  (plugin version 1.0.0) walks the kernel's in-memory kallsyms structures and, for each loaded module,
  its `module.syms` / `module.num_syms` exported-symbol table, plus ftrace and BPF symbols. Everything
  it needs — `kernel_symbol`, `module`, the kallsyms globals — comes from the **kernel** ISF.
* **There is no per-module ISF mechanism.** Nothing loads a `.ko`-derived ISF, and module *types* do not
  appear in the kernel ISF unless they are built into the kernel.
* **Consequence:** analysis of a loadable module's private data structures is not supported by stock
  plugins. You get module presence, names, addresses, sections and exported symbols — not the layout of
  a proprietary driver's private structs.
* Plugins that depend on module metadata include `linux.lsmod`, `linux.modxview`,
  `linux.check_modules`, `linux.hidden_modules`, `linux.module_extract`, and `linux.kallsyms` (log #102
  lists 59 `linux.*` plugins in total).

```bash
# Module-related checks once you have a working ISF
python3 vol.py -f "$MEMORY" linux.lsmod
python3 vol.py -f "$MEMORY" linux.check_modules
python3 vol.py -f "$MEMORY" linux.kallsyms --modules
```

---

## 21. Installing ISFs

### 21.1 Where to put the file

Any of the search paths in §14.1 works. In practice:

```bash
# Preferred for casework: a case-local directory, passed explicitly.
mkdir -p /path/to/case/symbols
cp kernel.json.xz /path/to/case/symbols/
python3 vol.py -s /path/to/case/symbols -f "$MEMORY" banners

# Multiple directories: SEMICOLON separated (not colon).
python3 vol.py -s "/path/to/case/symbols;/srv/shared/isfs" -f "$MEMORY" linux.pslist

# Installation-wide: drop it under the package's symbols directory.
cp kernel.json.xz "$(python3 -c 'import volatility3, os; print(os.path.join(os.path.dirname(volatility3.__file__), "symbols"))')/linux/"
```

Verified behaviour (log #13, #14, #15, #16):

* Discovery is **recursive** under every base path, so `symbols/linux/ubuntu/2404/kernel.json.xz` is
  found just as well as `symbols/kernel.json`.
* The `linux/` subdirectory is **conventional, not required** — a Linux ISF placed at the top level was
  discovered and used in testing. (Windows ISFs *are* looked up under a `windows` sub-path by the PDB
  machinery, which is where the convention comes from.)
* **File names are irrelevant to matching.** Naming is for humans; use something meaningful, e.g.
  `Ubuntu_6.8.0-101-generic_6.8.0-101.101_amd64.json.xz`.
* A case-local directory passed with `-s` is the better forensic habit: it keeps case material out of
  the tool installation and makes the working set explicit in your command lines.

### 21.2 After installing: refresh the cache

The identifier cache is updated automatically when a plugin runs, but the first run after adding many
files can be slow, and a stale cache is a classic cause of "my ISF is ignored":

```bash
python3 vol.py --clear-cache          # drops *.cache in the cache dir, then rebuilds on next run
python3 vol.py isfinfo | head         # forces the rebuild and shows what is now known
```

---

## 22. Compression and archive containers — two different mechanisms

### 22.1 Direct compression (loaded natively)

`constants.ISF_EXTENSIONS = [".json", ".json.xz", ".json.gz", ".json.bz2"]` (log #15). All four are
discovered and opened directly; the framework decompresses on the fly, sniffing the content with
`magic` when that module is importable and otherwise falling back to the extension chain (log #17).

```bash
ISF="/path/to/case/symbols/Ubuntu_6.8.0-101-generic.json"

xz  -9 -T0 -c "$ISF" > "${ISF}.xz"     # -> Ubuntu_6.8.0-101-generic.json.xz
gzip -9   -c "$ISF" > "${ISF}.gz"      # -> Ubuntu_6.8.0-101-generic.json.gz
bzip2 -9  -c "$ISF" > "${ISF}.bz2"     # -> Ubuntu_6.8.0-101-generic.json.bz2

# Keep exactly one of them installed - see 22.3.
```

Measured (log #54): a 64,295,265-byte ISF compressed to 3,325,452 bytes with `xz -6 -T0` in 3.0 s —
about 19:1. `xz` is the community convention for exactly this reason.

### 22.2 ZIP containers (a separate code path)

ZIP handling is **not** the same mechanism. `file_symbol_url()` runs a second, independent pass looking
for `*.zip` under each symbol path, opens each archive, and yields any member whose name ends with one
of the ISF extensions, addressed as `jar:file:/path/to/pack.zip!linux/kernel.json` (log #16).

```bash
# Build a pack the way the official ones are built (members under a linux/ prefix)
mkdir -p staging/linux && cp *.json.xz staging/linux/
( cd staging && zip -r ../linux-pack.zip linux )
cp linux-pack.zip /path/to/case/symbols/
python3 vol.py -s /path/to/case/symbols isfinfo --live --filter linux-pack
```

Verified in testing: a plain `.json` member inside a ZIP was discovered and listed exactly like the
loose files (log #14, #16). Note that ZIP members may themselves be `.json.xz` — the two mechanisms
compose.

### 22.3 Storage and cache implications

* **Do not install the same ISF twice in different forms.** All copies share one identifier; the cache
  keeps one location and logs a duplicate-entry debug message. Which one wins is not something to rely
  on (log #24).
* Compressed ISFs are decompressed on **every** load; for a 64 MB JSON that is a small but repeated CPU
  cost. For interactive work on one image, an uncompressed ISF is marginally faster; for a library of
  thousands, `.json.xz` is the only sane choice.
* Files fetched over HTTP (remote ISF URLs) are cached as `data_<sha512(url)>.cache` in the cache
  directory (log #18) — that directory can grow large and is safe to clear.
* Successful schema validations are memoised in `valid_isf.hashcache`, keyed on the hash of the
  document *and* the schema (log #18) — so editing a schema invalidates prior results automatically.

---

## 23. Validating an ISF

Work from cheapest to most meaningful. Each step answers a different question, and passing one says
nothing about the next.

### 23.1 Does the JSON parse?

```bash
ISF="/path/to/case/symbols/kernel.json"
jq empty "$ISF" && echo "valid JSON"

# For a compressed ISF, test the container first, then the JSON.
xz -t "$ISF.xz" && xz -dc "$ISF.xz" | jq empty && echo "valid container + valid JSON"
```

This catches the single most common self-inflicted failure: a truncated file from a failed
`dwarf2json` run or an OOM kill (§8.5, §8.6).

### 23.2 Metadata inspection

```bash
jq '.metadata' "$ISF"
```

Expect, for a dwarf2json 0.9.0 ISF (real shape, log #108):

```json
{
  "linux": {
    "symbols": [ {"kind":"dwarf","name":"vmlinux-…","hash_type":"sha256","hash_value":"…"},
                 {"kind":"symtab","name":"vmlinux-…","hash_type":"sha256","hash_value":"…"} ],
    "types":   [ {"kind":"dwarf","name":"vmlinux-…","hash_type":"sha256","hash_value":"…"} ]
  },
  "producer": { "name": "dwarf2json", "version": "0.9.0" },
  "format": "6.2.0"
}
```

Read it as provenance: which files, hashed how, contributed types and which contributed symbols. If
`types` is missing you have a symbols-only ISF; if `symbols` lists only `system-map` you have no banner.

### 23.3 Symbol inspection

```bash
# Count symbols and check the ones the stacker needs.
jq '.symbols | length' "$ISF"
for s in init_task init_mm init_files swapper_pg_dir init_top_pgt init_level4_pgt linux_banner; do
    printf '%-18s %s\n' "$s" "$(jq -r --arg s "$s" '.symbols[$s].address // "ABSENT"' "$ISF")"
done
```

`init_task`, `init_mm`, `init_files` and one page-global-directory symbol are what
`LinuxIntelStacker` needs to build a layer at all (log #35).

### 23.4 Type inspection — with the right field names

```bash
jq '.user_types | length' "$ISF"
jq '.enums      | length' "$ISF"
jq '.base_types | length' "$ISF"

# A structure's size and one member's offset:
jq '.user_types.task_struct.size' "$ISF"
jq '.user_types.task_struct.fields.comm' "$ISF"
jq '.user_types.task_struct.fields | keys | length' "$ISF"
```

Note the names: **`user_types`** (not "types" or "structs"), each with `kind`/`size`/`fields`, and each
field carrying `offset` and `type` (log #10). For orientation, two real ISFs on this workstation had
13,948 and 7,515 `user_types` and 294,116 and 109,467 symbols (log #54). An ISF with a few hundred
`user_types` is not a kernel ISF.

### 23.5 The banner, decoded

```bash
jq -r '.symbols.linux_banner.constant_data' "$ISF" | base64 -d | xxd | tail -2
```

You are checking three things: that it exists at all; that it ends with `0a 00` (newline, NUL) as the
kernel's `linux_banner` does; and that the text equals the banner the image reports (§23.7).

### 23.6 Schema validation the way the framework does it

```bash
# Exactly the code path Volatility uses.
python3 - "$ISF" <<'PY'
import json, sys
from volatility3 import schemas
with open(sys.argv[1]) as fh:
    obj = json.load(fh)
try:
    print("schema validation:", schemas.validate(obj, use_cache=False))
except Exception as exc:                      # jsonschema raises ValidationError here
    print("schema validation FAILED:", type(exc).__name__, str(exc).splitlines()[0][:200])
PY
```

Two things to know about this in 2.28.0 (log #7, #25, #26):

* An instance failure raises `jsonschema.exceptions.ValidationError` out of `schemas.valid()` — the
  `try` above is not decoration.
* If `jsonschema` is **not installed**, `schemas.validate()` returns `True` for anything. Confirm your
  environment actually has it before trusting a "valid" result:

```bash
python3 -c "import jsonschema, importlib.metadata as m; print('jsonschema', m.version('jsonschema'))"
```

`isfinfo --validate` does the same job for many files at once, with the caveat that an invalid file
produces a traceback rather than a `False` row (log #25).

### 23.7 Correspondence to the captured kernel

```bash
MEMORY="/path/to/memory.raw"
ISF="/path/to/case/symbols/kernel.json"

IMAGE_BANNER="$(python3 vol.py -q -r csv -f "$MEMORY" banners 2>/dev/null | tail -n +2 | head -1 | cut -d, -f3-)"
ISF_BANNER="$(jq -r '.symbols.linux_banner.constant_data' "$ISF" | base64 -d | tr -d '\000' | sed -e 's/[[:space:]]*$//')"

printf 'image: %s\n' "$IMAGE_BANNER"
printf 'ISF  : %s\n' "$ISF_BANNER"
[ "$IMAGE_BANNER" = "$ISF_BANNER" ] && echo "banners agree" || echo "BANNERS DIFFER - do not proceed"
```

(The trailing-whitespace and NUL handling matters: the plugin strips trailing whitespace when printing,
while the ISF stores the raw bytes including `\n\0` — see log #22, #109.)

### 23.8 Five statements that are *not* the same

| Statement | What proves it | What it does **not** prove |
|---|---|---|
| 1. "The JSON parses" | `jq empty` | Nothing about content |
| 2. "The JSON matches the schema" | `schemas.validate()` / `isfinfo --validate` | Nothing about which kernel it describes |
| 3. "Volatility loads it" | It appears in `isfinfo`, with an identifier | Not that it will be chosen for *your* image |
| 4. "Volatility selects it" | `-vvv` shows `Identified banner: …` and `Using symbol library: …` | Not that the types are right — only that the banner bytes matched |
| 5. "It matches the captured kernel" | Multiple independent structure families produce coherent results (§24) | — |

Only (5) is a forensic claim. (1)–(4) are plumbing.

---

## 24. Testing against the memory image

### 24.1 Staged methodology

Run these in order and stop at the first that misbehaves.

```bash
MEMORY="/path/to/memory.raw"
SYMS="/path/to/case/symbols"
V="python3 vol.py -s $SYMS -f $MEMORY"

# Stage 0 - no symbols needed. Confirms format, banner, and (for ELF images) metadata.
$V banners
$V linux.vmcoreinfo                     # ELF/kdump images only

# Stage 1 - does a kernel get identified at all?  Watch for "Identified banner" / "Using symbol library".
python3 vol.py -vvv -s "$SYMS" -f "$MEMORY" banners 2>&1 | grep -Ei 'identified banner|symbol library|aslr'

# Stage 2 - the cheapest structure-dependent plugins.
$V linux.pslist
$V linux.pstree

# Stage 3 - independent structure families (do not rely on one).
$V linux.lsmod                  # module list
$V linux.lsof                   # files/fds
$V linux.mountinfo              # VFS
$V linux.ip.Addr 2>/dev/null || $V linux.sockstat   # network
$V linux.kmsg                   # ring buffer - content is human-checkable
$V linux.kallsyms --core        # in-memory symbol table vs your ISF

# Stage 4 - layout-sensitive/heavy plugins.
$V linux.proc.Maps
$V linux.elfs
$V linux.malware.check_syscall
```

Both plugin name forms work: the CLI accepts the short `linux.pslist` and the fully qualified
`linux.pslist.PsList` (verified on 2.28.0 — both print the same `usage: vol.py linux.pslist.PsList`
line). The short form is used below for readability.

Why this order: stage 0 needs no ISF; stage 1 proves selection; stage 2 exercises `task_struct` only;
stage 3 exercises `module`, `file`/`files_struct`, `mount`/`vfsmount`, `sock`, and the kallsyms
structures — different subsystems, so agreement between them is real evidence; stage 4 depends on the
most intricate layouts.

`linux.kmsg` deserves special mention: it prints kernel log text, which a human can sanity-check
directly ("does this read like a real boot?"), making it an unusually good cross-check.

### 24.2 Symptom table

| Situation | What you see |
|---|---|
| **Correct ISF** | `banners` matches; `-vvv` shows a banner match and page-aligned ASLR shifts; `pslist` shows PID 1 = `systemd`/`init` with plausible names, ascending PIDs, sane start times; `lsmod` names look like real modules; `kmsg` reads like a boot log |
| **Wrong build (right version)** | Banner does not match → nothing is selected → "A symbol table requirement was not fulfilled". If you force a mismatched ISF: empty or nonsensical output everywhere |
| **Wrong addresses** | Stacker logs "Scanners could not determine any ASLR shifts, using 0 for both"; plugins return nothing or raise invalid-address errors |
| **Wrong offsets (subtly wrong types)** | *Plausible-looking but wrong* values: process names shifted or garbled, absurd PIDs/UIDs, timestamps in 1970 or the far future, `lsof` paths that are fragments |
| **Incomplete ISF (types-only)** | The ISF is never selected (no banner) — indistinguishable from "no ISF" until you inspect the file (§23.3) |
| **Missing constants (no banner)** | Same as above; `isfinfo` shows the file with an empty "Identifying information" column |
| **Banner mismatch (byte-level)** | `banners` prints a banner, `isfinfo` shows a nearly identical one, nothing is selected. Compare bytes, not text (§23.7) |
| **Architecture mismatch** | For an AArch64 image: no Linux stacker applies at all; plugins report an unsatisfied translation-layer requirement |
| **KASLR unresolved** | As "wrong addresses"; distinguish by checking whether a banner match happened at all (§13.4) |
| **Corrupt/partial acquisition** | `banners` finds a banner (it is early in physical memory) but page-table walks fail; plugins fail intermittently; large regions read as zeros |
| **Unsupported kernel feature** | One plugin raises a missing-member/type error while others work — typically a structure that changed in that kernel version |

### 24.3 One plugin proves very little

`linux.pslist` succeeding proves that `task_struct`'s `comm`, `pid`, `tasks` and a couple of neighbours
are right. It says nothing about `file`, `mount`, `sock`, `module` or the hundreds of other structures a
report may depend on. Always corroborate across at least three independent families (stage 3), and
prefer checks whose output a human can validate independently (`kmsg`, mount table, network state
against known facts of the case).

---

## 25. Troubleshooting matrix

| Symptom | Likely cause | How to verify | Corrective action |
|---|---|---|---|
| `Unable to validate the plugin requirements: [...]` | A requirement (layer or symbols) could not be met | Re-run with `-vvv` | Follow the specific requirement named |
| `Unsatisfied requirement plugins.<X>.kernel: Linux kernel` + "A symbol table requirement was not fulfilled" | No ISF whose banner matches the image | `vol -f IMG banners`, then `vol isfinfo` and compare (§23.7) | Obtain/generate the right ISF (§8, §14, §15) |
| Banner found, but still no kernel | Byte-level banner difference; or the ISF failed schema validation inside the stacker | `-vvv`/`-vvvv` shows either "Symbol library path not found for: b'…'" or a `ValidationError` traceback in the debug log (log #37) | Fix the banner bytes, or fix the schema/metadata problem (§9.5) |
| `jsonschema.exceptions.ValidationError … is not valid under any of the given schemas` | Metadata that the schema forbids — classically `kind: "btf"`/`"symdb"` from btf2json (log #7, #64) | `jq '.metadata' "$ISF"` | Use a DWARF-derived ISF, or knowingly patch the schema and document it (§9.5) |
| `File does not pass version validation: <url>` | `schemas.validate()` returned False — usually a missing/unknown `metadata.format` | `jq '.metadata.format' "$ISF"` | Regenerate with a current converter; formats below 4.0.0 are deprecated and below 2.0.0 unsupported (`ISF_MINIMUM_SUPPORTED = (2,0,0)`) |
| `ISF version X has been deprecated` (warning) | Old but still supported format | `jq '.metadata.format'` | Regenerate |
| ISF is listed by `isfinfo` but "Identifying information" is empty | No `linux_banner.constant_data` | §23.5 | Regenerate including an ELF input (`--elf` or `--elf-symbols`), not `--system-map` alone (log #47) |
| Two ISFs, only one is ever used | Identical identifiers; the cache stores one location per identifier | `-vvv` shows "Duplicate entry for identifier…" (log #24) | Keep exactly one copy installed |
| Newly added ISF ignored | Stale identifier cache, or the file is outside every symbol path | `python3 vol.py isfinfo --live \| grep <name>` | `--clear-cache`, or pass the directory with `-s` |
| `FileNotFoundError: … identifier.cache` | `--cache-path` pointed at a directory that does not exist (log #20) | Check the path | `mkdir -p` it first |
| Plugins return nothing; log says "using 0 for both" | KASLR shifts not found — wrong ISF, corrupt image, or an unusual layout | `-vvv` for the ASLR line; `linux.vmcoreinfo` if the image is ELF | Re-check the ISF; prefer an ELF/kdump image that carries VMCOREINFO |
| Garbled process names / absurd PIDs | Right banner, wrong types (hand-edited ISF, mismatched inputs) | Compare `user_types.task_struct.size` against a known-good ISF for that kernel | Regenerate from a single, consistent set of inputs |
| `dwarf2json`: "could not get DWARF from …: too short" | The ELF has no DWARF (stripped, or an extracted `vmlinuz`) | `readelf -S "$VMLINUX" \| grep debug_info` | Fetch the debug package (§15); or take the BTF route (§9) |
| `dwarf2json`: "could not get symbols: no symbol section" | Fully stripped ELF passed to `--elf-symbols` | `nm "$VMLINUX"` → "no symbols" | Use `--system-map` for addresses; get the banner from an ELF that has `.rodata` + symtab |
| ISF file exists but is 0 bytes | The converter failed and the shell still created the redirect target (log #52) | `ls -l`, `jq empty` | Always use the exit-status pattern in §8.5 |
| Converter dies silently, exit 137 | OOM kill (log #53) | `dmesg \| tail`, `journalctl -k \| grep -i killed` | More RAM/swap; convert one kernel at a time |
| Wrong architecture | AArch64/other image against upstream Volatility | Banner text; `file` on the image | Not supported upstream (log #34); third-party fork or a different toolchain |
| Wrong endianness | Big-endian target (MIPS/PPC) | `readelf -h` on the kernel ELF; `base_types[].endian` in the ISF | Not supported for Linux upstream |
| Right version, wrong distribution build | Two vendors ship "5.15.0-97" | Compare full banners, build IDs, package versions | Get the exact vendor's debug package |
| Separate debug file not supplied | Passing the stripped kernel to `--elf` | The DWARF error above | Pass the debug `vmlinux` from the debug package |
| Wrong `System.map` | Map from a different build silently overrides good addresses (log #49) | Compare `System.map` sha256 to the one shipped with that exact kernel package | Drop `--system-map` or use the correct one |
| Raw BTF passed where an ELF was expected | e.g. `dwarf2json --elf /sys/kernel/btf/vmlinux` | `file` says `data`; first bytes `9f eb 01 00` (log #92) | Use an ELF, or a BTF-aware tool (§9) |
| ELF passed where a raw blob was expected | Tool complains about magic | `readelf -h` succeeds → it is an ELF | `objcopy --dump-section .BTF=… <elf> /dev/null` first (§26) |
| Unrecognised compression | `.json.zst`, `.json.lz4`, `.tar.gz` of ISFs | Only `.json`, `.json.xz`, `.json.gz`, `.json.bz2` (+ `.zip`) are handled (log #15, #16) | Recompress with xz/gzip/bzip2, or use a ZIP |
| Permission denied reading `/boot/vmlinuz*` or `/boot/System.map*` | Those files are mode 0600 root-only on Ubuntu (log #94) | `ls -l /boot` | Copy them as root at acquisition time (§30) |
| `/proc/kallsyms` all zeros | `kptr_restrict` and/or non-root (log #93) | `cat /proc/sys/kernel/kptr_restrict` | Capture as root with `kptr_restrict=0`, at acquisition time |
| Everything works, results still look wrong | Corrupt or partial acquisition | Cross-check `kmsg`, mounts, network against case facts | Re-acquire if possible; state the limitation |

---

## 26. Diagnostic command reference

Every command below was run in the verification environment unless explicitly marked. Versions:
binutils 2.42, file 5.45, jq 1.7, xz 5.4.5, gzip 1.12, bzip2 1.0.8, bpftool v7.7.0, coreutils 9.4.

### 26.1 `uname` — about the **source** machine only

```bash
uname -r      # 7.0.0-28-generic          release: version + ABI + flavour
uname -v      # #28~24.04.1-Ubuntu SMP …  build number and date
uname -a      # everything, including architecture
uname -m      # x86_64
```

> Run these **on the target at acquisition time**. On your workstation they describe your workstation
> and nothing else.

### 26.2 `file`

```bash
file /path/to/memory.raw
file /path/to/vmlinux
file /sys/kernel/btf/vmlinux        # -> "data"  (it is a raw BTF blob, not an ELF)  [log #92]
```

`file` on a boot image is unusually informative — it prints the version string:

```text
vmlinuz-deb: Linux kernel x86 boot executable bzImage, version 6.1.0-50-amd64
  (debian-kernel@lists.debian.org) #1 SMP PREEMPT_DYNAMIC Debian 6.1.176-1 (2026-07-02), RO-rootFS, …
```

(Real output, log #97.)

### 26.3 `readelf`

```bash
readelf -h "$VMLINUX"                     # class, endianness, type, machine
readelf -S "$VMLINUX"                     # section headers: .debug_*, .BTF, .symtab
readelf -n "$VMLINUX"                     # notes, including the GNU build ID
readelf -s "$VMLINUX" | head              # symbol table (silent/empty if stripped)
readelf -p .modinfo module.ko             # module metadata strings
readelf -S -W "$VMLINUX" | grep -E 'debug_info|BTF'
```

Tells you: whether it is an ELF at all, its architecture, and — the decisive question — which of DWARF,
BTF and a symbol table are actually present.

> **Warning:** `readelf --debug-dump=info` produces gigabytes on a kernel. Always pipe to `head`.

### 26.4 `nm`

```bash
nm "$VMLINUX" | head                       # "nm: <file>: no symbols" when stripped   [log #97]
nm -n "$VMLINUX" | grep -E ' (init_task|linux_banner|swapper_pg_dir)$'
```

`nm -n vmlinux` output is, in form, exactly a `System.map`.

### 26.5 `objdump`

```bash
objdump -h "$VMLINUX" | head -30           # section headers (alternative to readelf -S)
objdump --dwarf=info "$VMLINUX" | head -40 # WARNING: unbounded output; keep the head
```

### 26.6 `objcopy` — **the one that rewrites its input**

```bash
# CORRECT: explicit output operand -> the input is not touched.
objcopy --dump-section .BTF=kernel.btf "$VMLINUX" /dev/null

# ALSO CORRECT: write a copy you do not care about.
objcopy --dump-section .BTF=kernel.btf "$VMLINUX" /tmp/scratch-copy.elf
```

> **DANGER — omitting the output operand rewrites the input file.** Verified with binutils 2.42
> (log #99):
>
> * `objcopy --dump-section .text=out.bin t.elf` → content identical but the file was **rewritten**
>   (mtime changed);
> * `objcopy --add-section .foo=payload.bin t.elf` → the file's **contents changed**;
> * with an explicit output operand (`… t.elf /dev/null`) both hash and mtime were unchanged.
>
> On evidence this matters twice over: it invalidates your hashes, and it is a modification of an
> original. Work on copies, and always pass an output operand.
>
> The kernel's own `scripts/gen-btf.sh` uses the in-place form deliberately when embedding BTF into
> modules (log #89) — a reminder that the behaviour is normal, not a bug.

### 26.7 `strings` / `grep`

```bash
strings -a -t d "$MEMORY" | grep -m 20 'Linux version '     # bounded!
strings -a "$VMLINUX" | grep -m 2 '^Linux version'
grep -a -b -m 1 'Linux version ' "$MEMORY"                  # byte offset of the first hit
```

Expect near-duplicates: on the test kernel, two banner-like strings existed and only one matched the
running kernel's `uname -v` form (log #110).

> **Do not confuse a `linux_banner` *symbol* with the banner *text*.** Finding `linux_banner` in a
> `System.map` proves only that the symbol exists at some address; it tells you nothing about the bytes
> stored there. The text can only come from an ELF's `.rodata`, from memory, or from `/proc/version` on
> a live box.

### 26.8 `jq`

```bash
jq empty "$ISF"                                     # parse check
jq '.metadata' "$ISF"                               # provenance
jq '.user_types | length' "$ISF"                    # type count
jq '.symbols | keys | length' "$ISF"                # symbol count
jq -r '.symbols.linux_banner.constant_data' "$ISF" | base64 -d | xxd | tail -2
jq '.user_types.task_struct.fields | to_entries | sort_by(.value.offset) | .[0:5]' "$ISF"
```

### 26.9 Compression tools

```bash
xz -t   file.json.xz            # integrity test
xz -l   file.json.xz            # ratio and sizes
xz -dc  file.json.xz | jq empty
gzip -t file.json.gz
bzip2 -t file.json.bz2
zstd -dc module.ko.zst > module.ko          # kernel modules on Ubuntu
```

### 26.10 `bpftool`

```bash
bpftool version                                   # v7.7.0, using libbpf v1.7
bpftool btf list                                  # BTF objects loaded in the running kernel
bpftool btf dump file /sys/kernel/btf/vmlinux format c | head -40    # C header form
bpftool btf dump file kernel.btf | head           # raw form: [id] KIND 'name' …
bpftool btf dump file /sys/kernel/btf/nf_tables -B /sys/kernel/btf/vmlinux | head   # split BTF
```

The raw form prints one line per BTF type; the extracted 4.35 MB `.BTF` from the test kernel produced
236,994 lines (log #98). `format c` produces the familiar `vmlinux.h` used by CO-RE BPF programs. What
you are looking at is **type information only** — no addresses, no data values.

### 26.11 `pahole`

*(pahole v1.25 was obtained without root — `apt-get download pahole && dpkg-deb -x pahole_*.deb root`
— and the commands below were run against a real debug `vmlinux` (log #122).)*

```bash
pahole --version                                    # kernel requires >= 1.22 for CONFIG_DEBUG_INFO_BTF
pahole -J --btf_encode_detached=out.btf "$VMLINUX"  # encode BTF from DWARF (as the kernel build does)
pahole --btf_base vmlinux -J module.ko              # split BTF for a module
pahole -C task_struct "$VMLINUX"                    # print a structure's layout from DWARF
```

`pahole -C <struct>` is an excellent independent cross-check of an ISF's member offsets when you have
the debug `vmlinux` — and it was used as exactly that here. For `vmlinux-5.4.0-1133-kvm`, pahole 1.25
reported `/* size: 6784, cachelines: 106, members: 192 */` with `tasks` at 728, `pid` at 984 and
`comm[16]` at 1424; the ISF generated by dwarf2json 0.9.0 records `task_struct` size **6784** and the
same three offsets — **728, 984, 1424** (log #122). Two independent DWARF readers agreeing on layout is
the strongest cheap check available before you touch the memory image.

Measured cost of BTF encoding, for comparison with §8.6 (log #122): `pahole -J
--btf_encode_detached=out.btf` on that same 529 MB debug kernel took **4.95 s** at **442 MiB** peak RSS
and produced a 3,313,298-byte BTF blob — against the 3,313,978-byte `.BTF` section the distribution's
own build had already embedded. Note that the **detached** form leaves the input untouched (verified by
hash); plain `pahole -J vmlinux` writes the section back into the file.

### 26.12 Hashing

```bash
sha256sum "$MEMORY" "$VMLINUX" "$SYSTEM_MAP" "$ISF"
```

> On a 64 GB image `sha256sum` takes minutes and is I/O bound. Hash once, record the value, and reuse
> it (§31).

### 26.13 Volatility's own inspection commands

```bash
python3 vol.py --help | grep -E '^\s+(banners|isfinfo|linux\.)' | head -40
python3 vol.py isfinfo                                  # what the cache knows
python3 vol.py isfinfo --live --validate --filter linux # what is on disk, validated
python3 vol.py isfinfo --isf file:///path/to/kernel.json --live
python3 vol.py -f "$MEMORY" banners
python3 vol.py -f "$MEMORY" linux.vmcoreinfo
python3 vol.py -vvv -f "$MEMORY" linux.pslist 2>&1 | grep -Ei 'banner|symbol library|aslr|dtb'
python3 vol.py --clear-cache
```

---

## 27. Artifact identification matrix

Qualified answers only; "depends on build" means exactly that — check, do not assume.

| Artifact | ELF? | Symbol addresses? | Kernel types? | DWARF? | BTF? | Useful to the DWARF converter (`dwarf2json`)? | Useful to a BTF converter (`btf2json`)? |
|---|---|---|---|---|---|---|---|
| `vmlinux` (distribution **debug** build) | Yes | Yes — link-time, in `.symtab` | Yes | **Yes** | Usually (modern builds) | **Yes — this is the intended input (`--elf`)** | Yes (as the `--btf` source), but pointless when DWARF is present |
| `vmlinux` (shipped/stripped, e.g. inside `linux-image`) | Yes | Depends on build; often absent | Only via BTF | No | Usually yes | Only for `--elf-symbols` if a `.symtab` survives | Yes — `--btf` source; banner recoverable only if `.symtab` survives |
| `vmlinux` extracted from `vmlinuz`/bzImage | Yes | **No** (verified: "no symbols") | Only via BTF | **No** | Usually yes (`.BTF` + `.BTF_ids`) | **No** | Yes — but you must supply `--map` and `--banner` |
| `vmlinuz` / bzImage (as shipped) | No (self-extracting container) | No | No | No | Only after extraction | No | No — extract first |
| `System.map` | No (text) | **Yes — link-time** | **No** | No | No | Yes (`--system-map`; addresses only, no banner) | **Yes — the primary symbol source (`--map`)** |
| `/proc/kallsyms` (captured live) | No (text) | Yes — **runtime**, subject to `kptr_restrict` | No | No | No | Possible as a `--system-map` substitute, with the KASLR caveat | Yes — used exactly this way in Android workflows |
| `/sys/kernel/btf/vmlinux` | **No — raw BTF blob** | No | Yes (types only) | No | **Yes (raw, not a section)** | **No** | **Yes — `--btf` source**; needs `--map` and `--banner` |
| Separate debug ELF from a `-dbg`/`-dbgsym`/`-debuginfo` package | Yes | Yes | Yes | **Yes** | Usually | **Yes** | Yes, but unnecessary |
| `.ko` module (as shipped) | Yes (relocatable) | Yes — but relocatable/section-relative | Only via split BTF | No | Usually (split) | No (module DWARF is in the debug package; Volatility has no per-module ISF path) | Not for kernel ISFs |
| `.ko` module (from the debug package) | Yes | Yes | Yes | Yes | Usually | Not used by Volatility for module types (log #101) | No |

---

## 28. Decision matrix

Labels: **required** = you must have it; **recommended** = have it if you can; **optional** = nice to
have; **insufficient alone**; **experimental** = third-party, document it.

| What I have | Recommended approach | Label | Limitations / next step |
|---|---|---|---|
| An ISF whose banner byte-matches the image | Install it, validate it (§23), then test in stages (§24) | required (this is the goal) | Loading ≠ correct; corroborate across structure families |
| Debug `vmlinux` with DWARF (from the exact build) | `dwarf2json linux --elf "$VMLINUX" > out.json` | **required path** | Needs RAM; verify the produced banner |
| Debug `vmlinux` **plus** matching `System.map` | `dwarf2json linux --elf "$VMLINUX" --system-map "$SYSTEM_MAP"` | recommended | Map addresses override ELF ones (log #49) — make sure the map is from the same build |
| Types only (`--elf-types` output, or a types-only ISF) | Add symbols and a banner from an ELF or a map | insufficient alone | No banner → never auto-selected (log #21, #47) |
| Shipped ELF `vmlinux` with `.BTF` **plus** `System.map` | Third-party `btf2json --btf … --map … --banner …` | **experimental** | Requires schema patch; symbol types come from the embedded symdb (log #62, #63) |
| Exported `/sys/kernel/btf/vmlinux` **plus** `System.map`/kallsyms | Same as above, plus `--banner` (raw blob has no banner) | **experimental** | Only obtainable from a live system |
| `System.map` only | Nothing usable on its own | insufficient alone | No types, no banner. Find DWARF or BTF for that build |
| `/proc/kallsyms` only | Treat as a `System.map` substitute, note runtime addresses | insufficient alone | Still no types; `kptr_restrict`/privilege caveats (log #93) |
| `vmlinuz`/bzImage only | `extract-vmlinux`, then inspect: expect `.BTF`, no `.symtab`, no DWARF (log #97) | recommended first step | Then the BTF route with an external map + banner, or find the debug package |
| Matching distribution debug package (not yet downloaded) | Download, verify hash, unpack, then the DWARF route (§15) | **required path** | Availability is time-limited (log #74) |
| Custom kernel source + exact `.config` + toolchain | Rebuild with `CONFIG_DEBUG_INFO=y`, then the DWARF route | recommended, with caveats | Reproducing byte-identical layout is hard (§16.2) |
| Memory image only | Banner first (§11.3), then hunt that exact build | required starting point | If the build's artifacts are gone, say so in the report rather than forcing a near-match |

---

## 29. Common misconceptions

| Claim | Verdict | Explanation | Version context |
|---|---|---|---|
| "The DWARF converter has a BTF option that generates a Volatility profile." | **False, twice over** | `dwarf2json` has no BTF option — no flag, no code, no string in the binary, no issue or PR ever opened about BTF. And Volatility 3 does not use "profiles"; it uses ISFs | dwarf2json v0.9.0 / master `9f14607e0d`, checked 2026-08-24 (log #50) |
| "BTF is mandatory for Volatility 3." | **False** | Nothing in the framework reads BTF. The upstream path is DWARF → dwarf2json → ISF | Volatility 3 2.28.0 (log #50, #70) |
| "Volatility 3 reads `/sys/kernel/btf/vmlinux` directly as its Linux symbol table." | **False** | Volatility loads ISF JSON from its symbol paths. There is no BTF loader | Volatility 3 2.28.0 (log #5, #13) |
| "`System.map` contains kernel structure definitions." | **False** | It is three columns of `address type name`. No sizes, no members, no offsets | Always true; verified with dwarf2json output (log #47) |
| "`System.map` is always mandatory for the DWARF converter." | **False** | `--elf` alone produced a complete ISF including the banner. It is situational | dwarf2json v0.9.0 (log #48) |
| "`vmlinuz` and `vmlinux` are interchangeable." | **False** | `vmlinuz` is a self-extracting boot container; `vmlinux` is the ELF | Always |
| "Extracting `vmlinuz` gives you a debug `vmlinux`." | **False** | The extracted ELF in testing had `.BTF`/`.BTF_ids` but **no** `.symtab` and no `.debug_*`; `dwarf2json --elf` failed on it | Debian 6.1.0-50-amd64, 2026-08-24 (log #97) |
| "The same `uname -r` guarantees the ISF will match." | **False** | Release is an ABI label. Config, compiler, patches and build number all change layout; the banner encodes them, which is why it is the identity | Volatility docs, 2.28.0 (log #41) |
| "If Volatility accepts the JSON, the symbols are correct." | **False** | Schema-valid ≠ selected ≠ correct. And if `jsonschema` is missing, *everything* validates | Volatility 3 2.28.0 (log #26, §23.8) |
| "The exported BTF file is the ordinary uncompressed ELF kernel." | **False** | `/sys/kernel/btf/vmlinux` is a raw BTF blob: `file` says `data`, magic `9f eb 01 00`, `readelf` refuses it | Verified live (log #92) |
| "BTF contains everything DWARF contains." | **False** | No location expressions, no unwind/CFI, no lexical scopes, no line info for ordinary kernel code, no read-only *data values* (so no banner text) | kernel.org BTF docs; §6.5 |
| "BTF from the analysis machine works if the kernel family is similar." | **False, and dangerous** | Structure layouts differ between builds of the same family. Using local BTF produces a plausible ISF describing the **wrong** kernel | Always |
| "`/proc/kallsyms` is equivalent to `System.map`." | **False** | Runtime (KASLR-shifted) addresses, censored by `kptr_restrict` (all zeros for non-root here), and only available live | Verified live (log #93) |
| "A working plugin proves the whole symbol table is correct." | **False** | `pslist` exercises a handful of `task_struct` members. Corroborate across independent structure families | §24.3 |
| "Volatility 3 supports Linux on ARM64." | **False for upstream** | `LINUX_ARCHS = ["Intel32","Intel64"]`; no ARM layer exists in 2.28.0 or on `develop`. AArch64 work today means a third-party fork | Checked 2026-08-24 (log #34, #67) |
| "The banner plugin is `linux.banners`." | **False** | It is the top-level `banners.Banners`; it also finds Windows and macOS identifiers | Volatility 3 2.28.0 (log #22) |
| "ISFs must be named after the kernel and live in `symbols/linux/`." | **False** | Matching is by banner bytes; discovery is recursive; names are for humans | Verified (log #13, #14) |

---

## 30. Acquisition-time collection checklist

**This is the highest-value section in the guide.** Almost every "I cannot find symbols" case is a case
where nobody collected the kernel artifacts while the machine was still running. Debug packages get
deleted from archives (log #74); `/sys/kernel/btf/vmlinux` and `/proc/kallsyms` cease to exist the
moment the machine powers off.

Run these on the **target**, as root, at the same time as the memory capture, and hash everything
together.

```bash
# ── 0. Where to put it (a removable/remote path, not the target's own disk if avoidable)
OUT="/mnt/evidence/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT" && cd "$OUT"

# ── 1. Kernel identity  (cheap, tiny, indispensable)
uname -a                        > uname-a.txt
uname -r                        > uname-r.txt
cat /proc/version               > proc-version.txt      # the banner, verbatim
cat /etc/os-release             > os-release.txt
cat /proc/cmdline               > proc-cmdline.txt

# ── 2. Kernel configuration
cp /boot/config-"$(uname -r)"   ./config-"$(uname -r)"   2>/dev/null || \
  { zcat /proc/config.gz        > ./config-"$(uname -r)"; }

# ── 3. Symbol/address sources
cp /boot/System.map-"$(uname -r)" .            2>/dev/null   # root-only on many distros (log #94)
sysctl -n kernel.kptr_restrict  > kptr_restrict.txt
sysctl -w kernel.kptr_restrict=0                             # MODIFIES RUNNING STATE - record it
cat /proc/kallsyms              > kallsyms.txt               # runtime addresses (log #93)
sysctl -w kernel.kptr_restrict="$(cat kptr_restrict.txt)"    # restore

# ── 4. The boot image and, if present, BTF
cp /boot/vmlinuz-"$(uname -r)"  .              2>/dev/null   # root-only (log #94)
[ -r /sys/kernel/btf/vmlinux ] && cat /sys/kernel/btf/vmlinux > vmlinux.btf   # raw BTF (log #92)
mkdir -p btf-modules && for f in /sys/kernel/btf/*; do
    [ "$(basename "$f")" = vmlinux ] || cat "$f" > "btf-modules/$(basename "$f").btf"
done

# ── 5. Modules
lsmod                                   > lsmod.txt
cat /proc/modules                       > proc-modules.txt
tar czf modules-"$(uname -r)".tar.gz /lib/modules/"$(uname -r)"/kernel 2>/dev/null

# ── 6. Anything else that dies with the machine
cat /proc/iomem   > proc-iomem.txt
cat /proc/mounts  > proc-mounts.txt
dmesg             > dmesg.txt 2>/dev/null

# ── 7. Hash EVERYTHING, including the memory image, in one pass
sha256sum ./* ./btf-modules/* /path/to/memory.raw > SHA256SUMS 2>/dev/null
```

Why each item matters later:

| Item | Changes the workflow how | Survives power-off? |
|---|---|---|
| `/proc/version` (the banner) | Lets you find or verify an ISF without touching the image | Only as a copy |
| `uname -a`, `os-release` | Names the exact distribution package to fetch | Only as a copy |
| `/boot/config-<rel>` | Tells you whether DWARF/BTF exist at all, and whether hardening options will bite | Yes, on disk |
| `System.map` | Supplies link-time addresses; repairs a stripped ELF | Yes, on disk (root-only) |
| `/proc/kallsyms` | The only address source when there is no `System.map`; runtime addresses | **No** |
| `/boot/vmlinuz` | Extractable to an ELF with `.BTF` — the entry point to the BTF route | Yes, on disk (root-only) |
| `/sys/kernel/btf/vmlinux` | The BTF route's type source, with no extraction step | **No** |
| Per-module BTF | Module type information; needed for any split-BTF work | **No** |
| `lsmod` / module files | Tells you which modules to expect; `.ko` files carry split BTF and symbols | Files yes, list no |
| Hashes taken at capture time | Ties the artifacts to the image and to the moment | — |

Two operational notes:

* Setting `kernel.kptr_restrict` **modifies the running system.** Record the original value, the exact
  command, and the timestamp in the case notes. If policy forbids it, capture `/proc/kallsyms` anyway —
  the names alone still have value.
* If you can only take one extra thing beyond the memory image, take `/proc/version`. If you can take
  two, add `/boot/config-$(uname -r)`. If three, add the `System.map`.

---

## 31. Forensic integrity

### 31.1 Principles that apply specifically to symbol work

* **Symbol artifacts are evidence too.** The ISF you used determines what every plugin printed. Hash
  it, keep it, and cite it in the report alongside the image.
* **Work on copies.** Never point tooling at the original image or the original kernel artifacts —
  particularly not `objcopy` (§26.6, log #99).
* **Record provenance for anything downloaded**: URL, HTTP date, file size, hash, and (for packages)
  the exact package version. Archives change and disappear (log #74).
* **Record tool identity**: `vol.py --help` prints the framework version; `dwarf2json --version` prints
  tool and schema version; for git checkouts record `git rev-parse HEAD`.
* **Record modifications to tooling.** If you patched the schema to accept a third-party ISF (§9.5),
  that patch is part of the method and must be disclosed.
* **Capture stdout and stderr**, not just the interesting lines. `tee` is enough.

### 31.2 Case layout

```text
case/
├── memory/
│   └── memory.raw
├── kernel/
│   ├── vmlinux                 # debug vmlinux (copy), or the extracted one
│   ├── System.map
│   ├── config-<release>
│   ├── kernel.btf              # only if captured live
│   └── packages/               # the .ddeb/.rpm/.deb as downloaded, unmodified
├── symbols/
│   └── kernel.json.xz
├── hashes/
│   └── SHA256SUMS
└── notes/
    ├── symbol-generation.md    # every command, with output
    └── tool-versions.txt
```

### 31.3 Hashing

```bash
cd case
sha256sum \
    memory/memory.raw \
    kernel/vmlinux \
    kernel/System.map \
    symbols/kernel.json.xz \
    > hashes/SHA256SUMS

# Later, to verify:
sha256sum -c hashes/SHA256SUMS
```

> Hashing a large memory image is slow (I/O bound; expect minutes for tens of gigabytes and longer over
> a network share). Do it once, in parallel with other work if possible, and never re-hash "just to be
> sure" instead of using `sha256sum -c`.

### 31.4 A reproducible generation record

```bash
NOTES="case/notes/symbol-generation.md"
{
  echo "# Symbol generation record"
  echo
  echo "## Environment"
  date -u +"generated: %Y-%m-%dT%H:%M:%SZ"
  echo "host: $(uname -a)"          # the ANALYSIS host - labelled as such
  echo "dwarf2json: $(dwarf2json --version | tr '\n' ' ')"
  echo "volatility: $(python3 vol.py --help 2>/dev/null | grep -m1 'Volatility 3 Framework' || echo 'see vol.py banner line')"
  echo "python: $(python3 --version), jsonschema: $(python3 -c 'import importlib.metadata as m; print(m.version("jsonschema"))' 2>/dev/null || echo absent)"
  echo
  echo "## Inputs"
  sha256sum case/kernel/vmlinux case/kernel/System.map
  echo "source: <URL of the debug package>  (downloaded: <UTC timestamp>)"
  echo
  echo "## Command"
  echo '```'
  echo 'dwarf2json linux --elf case/kernel/vmlinux --system-map case/kernel/System.map > case/symbols/kernel.json'
  echo '```'
} > "$NOTES"
```

Run the conversion itself under `tee` so both streams are preserved:

```bash
( dwarf2json linux --elf case/kernel/vmlinux > case/symbols/kernel.json ) 2>&1 | tee -a case/notes/dwarf2json.stderr.txt
```

(Note that `tee` sees stderr here; the exit status of the pipeline is `tee`'s — so keep the explicit
`if …; then` form of §8.5 for the decision, and use `tee` only to capture the diagnostics.)

---

## 32. End-to-end workflows

### 32.1 Workflow A — the upstream DWARF route (**supported**)

This is the workflow to use whenever the debug kernel can be obtained. Every command has been shown to
work in the verification environment except where a distribution debug package was needed (marked).

```bash
# ─────────────────────────────────────────────────────────────────────────────
# 0. Set up the case and the variables used throughout
CASE="/path/to/case"
MEMORY="$CASE/memory/memory.raw"
SYMS="$CASE/symbols"
KERN="$CASE/kernel"
mkdir -p "$SYMS" "$KERN" "$CASE/hashes" "$CASE/notes"

# ─────────────────────────────────────────────────────────────────────────────
# 1. Identify the image and record its hash
file "$MEMORY"
sha256sum "$MEMORY" | tee -a "$CASE/hashes/SHA256SUMS"      # slow on large images

# ─────────────────────────────────────────────────────────────────────────────
# 2. Extract the banner (no symbols required)
python3 vol.py -f "$MEMORY" banners | tee "$CASE/notes/banners.txt"
# If the image is an ELF/kdump capture, this is worth running too:
python3 vol.py -f "$MEMORY" linux.vmcoreinfo | tee "$CASE/notes/vmcoreinfo.txt"

# Example banner (real, from an ISF on the research workstation):
#   Linux version 6.8.0-101-generic (buildd@lcy02-amd64-051)
#   (x86_64-linux-gnu-gcc-13 (Ubuntu 13.3.0-6ubuntu2~24.04) 13.3.0, GNU ld … 2.42)
#   #101-Ubuntu SMP PREEMPT_DYNAMIC Mon Feb  9 10:15:05 UTC 2026 (Ubuntu 6.8.0-101.101-generic 6.8.12)
# From it:  release  = 6.8.0-101-generic   (flavour: generic)
#           package  = 6.8.0-101.101
#           arch     = x86_64  -> supported by Volatility 3 2.28.0

# ─────────────────────────────────────────────────────────────────────────────
# 3. Check whether an ISF already exists locally or remotely
python3 vol.py -s "$SYMS" isfinfo --live | grep -i '6.8.0-101' || echo "not local"
curl -fsSLO https://raw.githubusercontent.com/Abyss-W4tcher/volatility3-symbols/master/banners/banners_plain.json
grep -F -A2 '6.8.0-101-generic' banners_plain.json || echo "not in that collection"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Fetch the debug kernel for that EXACT package version   [Ubuntu]
#    (this step needs network access to the distribution archive)
DDEB="linux-image-unsigned-6.8.0-101-generic-dbgsym_6.8.0-101.101_amd64.ddeb"
curl -fsSLO "http://ddebs.ubuntu.com/pool/main/l/linux/$DDEB" \
  || echo "NOT AVAILABLE - see section 15.1 on archive retention"
# NOTE: this exact package returned HTTP 404 when checked on 2026-08-24, while the arm64 and
# s390x dbgsyms for the same kernel were still present (log #74). That is not a typo in this
# guide - it is the retention problem, in the flagship workflow, on a kernel less than a year old.
sha256sum "$DDEB" | tee -a "$CASE/hashes/SHA256SUMS"
dpkg-deb -x "$DDEB" "$KERN/ddeb"
VMLINUX="$(find "$KERN/ddeb" -path '*/usr/lib/debug/boot/vmlinux-*' | head -1)"
echo "vmlinux: $VMLINUX"

# ─────────────────────────────────────────────────────────────────────────────
# 5. Verify the artifact really is a DWARF-bearing ELF for the right kernel
file "$VMLINUX"
readelf -S "$VMLINUX" | awk '{print $2}' | grep -E '^\.(debug_info|symtab|BTF)$'
readelf -n "$VMLINUX" | grep -A1 -i 'build id'
strings -a "$VMLINUX" | grep -m1 '^Linux version'      # must equal the image's banner
sha256sum "$VMLINUX" | tee -a "$CASE/hashes/SHA256SUMS"

# ─────────────────────────────────────────────────────────────────────────────
# 6. (Optional) locate the matching System.map
SYSTEM_MAP="$(find "$KERN" -name 'System.map-6.8.0-101-generic' | head -1)"
[ -n "$SYSTEM_MAP" ] && sha256sum "$SYSTEM_MAP" | tee -a "$CASE/hashes/SHA256SUMS"

# ─────────────────────────────────────────────────────────────────────────────
# 7. Convert, with exit-status checking (a failed run leaves a 0-byte file)
ISF_OUT="$SYMS/Ubuntu_6.8.0-101-generic_6.8.0-101.101_amd64.json"
if dwarf2json linux --elf "$VMLINUX" ${SYSTEM_MAP:+--system-map "$SYSTEM_MAP"} > "$ISF_OUT"; then
    jq empty "$ISF_OUT" || { echo "invalid JSON produced" >&2; exit 1; }
else
    echo "dwarf2json failed" >&2
    rm -f "$ISF_OUT"
    exit 1
fi
ls -l "$ISF_OUT"     # expect tens of MB; ~64 MB for this kernel (log #54)

# ─────────────────────────────────────────────────────────────────────────────
# 8. Validate: structure, provenance, banner, schema
jq '.metadata' "$ISF_OUT"
jq '{base_types:(.base_types|length), user_types:(.user_types|length),
     enums:(.enums|length), symbols:(.symbols|length)}' "$ISF_OUT"
jq -r '.symbols.linux_banner.constant_data' "$ISF_OUT" | base64 -d | xxd | tail -2
python3 - "$ISF_OUT" <<'PY'
import json, sys
from volatility3 import schemas
obj = json.load(open(sys.argv[1]))
try:
    print("schema validation:", schemas.validate(obj, use_cache=False))
except Exception as exc:
    print("schema validation FAILED:", type(exc).__name__, str(exc).splitlines()[0][:200])
PY

# ─────────────────────────────────────────────────────────────────────────────
# 9. Compress and install (keep exactly one copy installed)
xz -9 -T0 "$ISF_OUT"                       # -> $ISF_OUT.xz  (~19:1, log #54)
sha256sum "$ISF_OUT.xz" | tee -a "$CASE/hashes/SHA256SUMS"

# ─────────────────────────────────────────────────────────────────────────────
# 10. Make Volatility see it
python3 vol.py --clear-cache
python3 vol.py -s "$SYMS" isfinfo --live --filter 6.8.0-101

# ─────────────────────────────────────────────────────────────────────────────
# 11. Confirm selection, then test in stages
python3 vol.py -vvv -s "$SYMS" -f "$MEMORY" banners 2>&1 | grep -Ei 'identified banner|symbol library|aslr'
python3 vol.py -s "$SYMS" -f "$MEMORY" linux.pslist   | head
python3 vol.py -s "$SYMS" -f "$MEMORY" linux.lsmod    | head
python3 vol.py -s "$SYMS" -f "$MEMORY" linux.mountinfo| head
python3 vol.py -s "$SYMS" -f "$MEMORY" linux.lsof     | head
python3 vol.py -s "$SYMS" -f "$MEMORY" linux.kmsg     | head -30
```

**Confidence assessment for Workflow A.** After step 11 you can state: the ISF's `linux_banner` bytes
equal the bytes in the image; the ISF was produced by a named tool version from a named debug package
whose hash is recorded; and independent structure families (`task_struct`, `module`, `mount`, `file`)
all produce coherent output. That is as strong as Linux symbol correspondence gets without the original
build tree.

### 32.2 Workflow B — the BTF route (**third-party / experimental**)

> **Status for the checked versions:** this workflow **cannot be completed against an unmodified
> Volatility 3 2.28.0.** The converter's output is rejected by the stock schema (log #7, #64, #65), and
> the AArch64 variant additionally requires a forked framework (log #67). What follows documents how far
> it can be taken and what each step actually guarantees. Do not present it as an equivalent to
> Workflow A.

**When it is even worth attempting:** no debug package exists or is still downloadable, no build tree
exists, and the target kernel has `CONFIG_DEBUG_INFO_BTF=y` — plus you can obtain a `System.map` or a
kallsyms capture, and the banner.

```bash
# ── B1. Identify the BTF source and know which form you have
#    (a) raw blob from a live system:
file /sys/kernel/btf/vmlinux            # -> "data";  head -c8 | xxd -> 9f eb 01 00 …   [log #92]
#    (b) ELF section inside a vmlinux (shipped or extracted):
readelf -S "$VMLINUX" | awk '{print $2}' | grep -x '.BTF'

# ── B2. If you only have a boot image, extract it (does not modify the input)
EXTRACT="/usr/src/linux-headers-$(uname -r)/scripts/extract-vmlinux"   # in-tree script
RELEASE="6.1.0-50-amd64"                       # the TARGET's release, from the banner
sh "$EXTRACT" "/path/to/vmlinuz-${RELEASE}" > "$KERN/vmlinux-extracted"
file "$KERN/vmlinux-extracted"
readelf -S "$KERN/vmlinux-extracted" | awk '{print $2}' | grep -E '^\.(BTF|symtab|debug_info)$'
nm "$KERN/vmlinux-extracted" 2>&1 | head -2      # expect: "no symbols"                 [log #97]

# ── B3. (Optional) pull the raw BTF out of that ELF - NOTE the output operand
objcopy --dump-section .BTF="$KERN/kernel.btf" "$KERN/vmlinux-extracted" /dev/null
xxd -l 8 "$KERN/kernel.btf"                       # 9feb 0100 1800 0000
bpftool btf dump file "$KERN/kernel.btf" | head -3

# ── B4. Symbol addresses must come from somewhere else
#     preferred: the target's /boot/System.map-<release>
#     fallback:  a root capture of /proc/kallsyms with kptr_restrict=0  (runtime addresses!)

# ── B5. The banner must come from somewhere else too
#     btf2json reads it from the ELF SYMBOL TABLE, which an extracted vmlinux does not have,
#     and a raw blob certainly does not - so --banner is effectively mandatory.       [log #60]
BANNER="$(python3 vol.py -q -r csv -f "$MEMORY" banners | tail -n +2 | head -1 | cut -d, -f3-)"

# ── B6. Build the third-party converter (records for the case notes)
git clone https://github.com/vobst/btf2json && cd btf2json
git rev-parse HEAD                     # record this
cargo build --release --locked         # requires a Rust toolchain
./target/release/btf2json --version

# ── B7. Convert  (exit-status checked, as always)
ISF_OUT="$SYMS/btf-derived.json"
if ./target/release/btf2json --btf "$KERN/kernel.btf" --map "$KERN/System.map" --banner "$BANNER" > "$ISF_OUT"; then
    jq empty "$ISF_OUT" || { echo "invalid JSON produced" >&2; exit 1; }
else
    echo "btf2json failed" >&2; rm -f "$ISF_OUT"; exit 1
fi
jq '.metadata' "$ISF_OUT"      # expect kinds: btf, symdb, system-map  -> NOT schema-valid
```

**B8. Schema compatibility — the wall.** At this point stock Volatility 3 2.28.0 will refuse the file
(`ValidationError`, log #7). Your options are:

1. **Stop here** and report that no usable symbol table could be produced with supported tooling. This
   is a legitimate and often correct outcome.
2. Apply the converter's schema patch to a **dedicated, recorded** Volatility checkout, and document it
   as described in §9.5 — including the chain-of-custody statement.

**B9. Validation and acceptance** (after patching, if you went that way): run every check in §23, then
the staged tests in §24 — and weight the results *lower* than Workflow A's, because:

* per-symbol types come from btf2json's compiled-in `symdb`, whose coverage may not match your kernel
  (log #62, #63);
* if your addresses came from `/proc/kallsyms`, they are runtime addresses, and Volatility's KASLR
  arithmetic expects link-time ones — verify against `linux.vmcoreinfo`/`linux.kallsyms` before
  trusting any address-derived result;
* the tool is dormant and its `--image` capability does not exist (log #57, #59).

**B10. Explicit limitations to state in the report**, if you use this route: third-party converter with
commit ID; modified Volatility with the exact diff; symbol types sourced from an embedded database;
banner supplied manually; and the fact that results were corroborated (or not) against independent
in-image evidence.

---

## 33. Quick reference

```text
Matching ISF already available?
    -> verify banner bytes + metadata, install, then test in stages (sections 23, 24).

DWARF-capable matching ELF vmlinux?
    -> dwarf2json linux --elf "$VMLINUX" > kernel.json          [UPSTREAM, SUPPORTED]
       always check the exit status; a failure leaves a 0-byte file.

DWARF present but symbol data incomplete/suspect?
    -> add --system-map "$SYSTEM_MAP"  (its addresses take precedence)
    -> or split the job: --elf-types <debug elf> --elf-symbols <shipped elf>

BTF only?
    -> NOT a drop-in DWARF replacement, and no upstream tool consumes it.
    -> third-party btf2json + System.map + --banner; output fails the stock schema;
       patching Volatility is a documentable event.                 [EXPERIMENTAL]

System.map only?
    -> addresses without types and without a banner: insufficient.

vmlinuz only?
    -> extract-vmlinux, then LOOK: expect .BTF present, .symtab absent, DWARF absent.

Exported /sys/kernel/btf/vmlinux only?
    -> raw BTF blob, not an ELF; you still need addresses and the banner, and it only
       exists on a live system.

memory.raw only?
    -> banners plugin first, then chase that exact build (package version is in the banner).

Nothing matches?
    -> say so. A wrong ISF that "loads" is worse than no ISF.
```

Fast checks worth memorising:

```bash
python3 vol.py -f "$MEMORY" banners                     # what kernel is this?
python3 vol.py isfinfo --live --validate                # what do I have, and is it valid?
readelf -S "$VMLINUX" | grep -E 'debug_info|BTF|symtab' # what does this ELF actually contain?
jq -r '.symbols.linux_banner.constant_data' "$ISF" | base64 -d | xxd | tail -2   # ISF banner bytes
python3 vol.py --clear-cache                            # when a new ISF is ignored
python3 vol.py -vvv … 2>&1 | grep -Ei 'banner|symbol library|aslr'   # why it was/wasn't selected
```

---

## Appendix A — Evidence log (Phase 1)

This is the complete Phase 1 research log. It is also shipped as the standalone file
`phase1-evidence-log.md` alongside this guide.


All rows accessed **2026-08-24** from the analysis workstation described below.

**Verification environment**

| Item | Value |
|---|---|
| Workstation OS | Ubuntu 24.04, kernel `7.0.0-28-generic` (`uname -a` output recorded in row 60) |
| Python | 3.12, `jsonschema` 4.10.3 |
| binutils | 2.42 (`objcopy`, `readelf`, `nm`, `strings`) |
| bpftool | v7.7.0, libbpf v1.7 |
| gcc | 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1) |
| `file` | 5.45; `jq` 1.7; `xz` 5.4.5; `gzip` 1.12; `bzip2` 1.0.8 |
| Not installed | `pahole`, `cargo`/`rustc`, `go`, `debuginfod-find` (rows marked accordingly) |
| Volatility 3 examined | release tarball `v2.28.0` (sha256 `e485bd18aa637543919930f1e84812e1b9eafae6cd3fcfc23e05990c78386c3f`), tag commit `d61335e2a54961bfd619fe6cd7f7b27f01481604` |
| dwarf2json examined | official release binary `dwarf2json-linux-amd64` v0.9.0 (sha256 `e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`) + `master` source tarball (HEAD `9f14607e0d`) |

Status values: `VERIFIED`, `UNVERIFIED`, `CONFLICTING`, `NOT SUPPORTED`, `DEPRECATED`.

| # | Claim to verify | Source URL | Version / tag / commit | Date accessed | Finding | Status |
|---|-----------------|-----------|------------------------|---------------|---------|--------|
| 1 | Current Volatility 3 release | https://api.github.com/repos/volatilityfoundation/volatility3/releases/latest | v2.28.0, published 2026-04-30T19:57:16Z | 2026-08-24 | Latest release is `v2.28.0`; release notes mention Linux module-format and VMA changes, Windows banner work | VERIFIED |
| 2 | Commit behind the v2.28.0 tag | https://api.github.com/repos/volatilityfoundation/volatility3/git/ref/tags/v2.28.0 | `d61335e2a54961bfd619fe6cd7f7b27f01481604` | 2026-08-24 | Tag resolves to that commit; source tarball downloaded and hashed | VERIFIED |
| 3 | Default branch and activity | https://api.github.com/repos/volatilityfoundation/volatility3 | branch `develop`, pushed 2026-08-19T14:14:53Z | 2026-08-24 | Development continues on `develop`; `stable` is documented in README as the stable branch | VERIFIED |
| 4 | ISF schema files shipped | `volatility3/schemas/` in v2.28.0 tarball; https://api.github.com/repos/volatilityfoundation/volatility3/git/trees/v2.28.0?recursive=1 | v2.28.0 | 2026-08-24 | Ships `schema-0.1.0`, `2.0.0`, `2.1.0`, `4.0.0`, `4.1.0`, `6.0.0`, `6.1.0`, `6.2.0`, `6.3.0`. Highest = **6.3.0**. `develop` (2026-08-19) ships the identical set | VERIFIED |
| 5 | Top-level ISF keys accepted by schema 6.3.0 | `volatility3/schemas/schema-6.3.0.json` | v2.28.0 | 2026-08-24 | `required`: `metadata`, `base_types`, `user_types`, `enums`, `symbols`; `additionalProperties: false` — no other top-level keys allowed | VERIFIED |
| 6 | Permitted producer/source `kind` values | `schema-6.3.0.json` → `definitions.metadata_nix_item.properties.kind` | v2.28.0 | 2026-08-24 | Pattern `^(dwarf\|symtab\|system-map)$`; `hash_type` pattern `^(sha256)$`; `additionalProperties:false` | VERIFIED |
| 7 | Are BTF-related metadata values accepted by the stock schema? | same as row 6; plus live test with `jsonschema` 4.10.3 | v2.28.0 | 2026-08-24 | `kind: "btf"` and `kind: "symdb"` are rejected. Live test raised `jsonschema.exceptions.ValidationError: ... is not valid under any of the given schemas` | NOT SUPPORTED |
| 8 | Schema 6.2.0 → 6.3.0 delta | diff of both files in tarball | v2.28.0 | 2026-08-24 | 6.3.0 adds `producer.version` pattern `^[0-9]+.[0-9]+.[0-9]+$` and makes `producer` required in every `metadata` variant | VERIFIED |
| 9 | Schema 6.1.0 → 6.2.0 delta | diff of both files in tarball | v2.28.0 | 2026-08-24 | 6.2.0 renames `metadata_linux`→`metadata_nix`, replaces the reserved placeholder with real `symbols`/`types` source arrays, introduces `kind`/`hash_type`/`hash_value`, and adds the `mac` metadata variant | VERIFIED |
| 10 | Element definitions (symbols/user_types/base_types/enums) | `schema-6.3.0.json` definitions | v2.28.0 | 2026-08-24 | `element_symbol`: `address` required; optional `type`, `linkage_name`, `constant_data` (base64). `element_user_type`: `kind ^(struct\|union\|class)$`, `size`, `fields`. `element_base_type`: `size`,`kind ^(void\|int\|float\|char\|bool)$`,`signed`,`endian`. `element_enum`: `size`,`base`,`constants` | VERIFIED |
| 11 | Symbol search paths | `volatility3/framework/constants/__init__.py` (v2.28.0) | v2.28.0 | 2026-08-24 | `SYMBOL_BASEPATHS = [<pkg>/symbols, <pkg>/framework/symbols, $CACHE_PATH/symbols]`; frozen builds prepend the executable directory | VERIFIED |
| 12 | User-configurable symbol directories | `volatility3/cli/__init__.py` lines 156-161, 288-291 | v2.28.0 | 2026-08-24 | `-s/--symbol-dirs` takes a **semicolon-separated** list and *prepends* to `SYMBOL_BASEPATHS`. No environment variable exists for symbol dirs | VERIFIED |
| 13 | Recursion behaviour of symbol discovery | `volatility3/framework/symbols/intermed.py` `file_symbol_url()` | v2.28.0 | 2026-08-24 | Uses `pathlib.Path(...).rglob(filename+extension)` → fully recursive under each base path | VERIFIED |
| 14 | Is a `linux/` subdirectory required? | live test: ISFs placed at top level, in `linux/`, in `deep/nested/`, and inside a ZIP | v2.28.0 | 2026-08-24 | All four were discovered and listed by `isfinfo --live`. `linux/` is **conventional, not required** for Linux kernel ISFs | VERIFIED |
| 15 | Compressed extensions loaded directly | `constants.ISF_EXTENSIONS` + live test | v2.28.0 | 2026-08-24 | `.json`, `.json.xz`, `.json.gz`, `.json.bz2`. `.json.xz` and `.json.gz` confirmed loading in the live test | VERIFIED |
| 16 | Archive (ZIP) handling is a separate mechanism | `intermed.py` `file_symbol_url()` + live test | v2.28.0 | 2026-08-24 | ZIPs are found by a separate `rglob("*.zip")` pass; members are addressed by `jar:file:<zip>!<member>` URLs. Not the same code path as direct compression | VERIFIED |
| 17 | Nested/auto decompression on open | `volatility3/framework/layers/resources.py` lines 225-282 | v2.28.0 | 2026-08-24 | If `magic` is importable, content sniffing selects xz/bz2/gzip (looped, so nested compression works); otherwise the URL's extension chain is used | VERIFIED |
| 18 | Cache location and contents | `constants.CACHE_PATH`, `schemas/__init__.py`, `resources.py`, `symbol_cache.py` | v2.28.0 | 2026-08-24 | `$XDG_CACHE_HOME/volatility3` or `~/.cache/volatility3` (`%APPDATA%\volatility3` on win32). Holds `identifier.cache` (SQLite, schema v1), `valid_isf.hashcache`, and `data_<sha512(url)>.cache` downloads | VERIFIED |
| 19 | Cache invalidation | `cli/__init__.py` `--clear-cache`; `framework.clear_cache()` | v2.28.0 | 2026-08-24 | `--clear-cache` unlinks `*.cache` in `CACHE_PATH` plus `identifier.cache`. Local ISF rows are also refreshed when the file mtime is newer than the cached timestamp, subject to `SQLITE_CACHE_PERIOD = "-3 days"` | VERIFIED |
| 20 | Cache path configurability | `cli/__init__.py` `--cache-path`; doc/source/symbol-tables.rst | v2.28.0 | 2026-08-24 | Source implements `--cache-path`; the shipped documentation states "The cache directory currently cannot be altered". Source wins. Also: pointing `--cache-path` at a **non-existent** directory aborts with `FileNotFoundError` in `symbol_cache.py:183` | CONFLICTING |
| 21 | Linux identifier used for matching | `automagic/symbol_cache.py` `LinuxIdentifier` | v2.28.0 | 2026-08-24 | Identifier = base64-decoded `symbols.linux_banner.constant_data`. No banner → no identifier → the ISF can never be auto-selected | VERIFIED |
| 22 | Banner-scanning plugin name and invocation | `volatility3/framework/plugins/banners.py`; live run | v2.28.0 (plugin `_version = (1,1,0)`) | 2026-08-24 | Plugin is `banners.Banners` (top-level, **not** `linux.banners`). `python3 vol.py -f <image> banners` printed `0x100000  Linux version 9.9.9-test ...` on a synthetic image. Regex: `(Linux version\|Darwin Kernel Version) [0-9]+\.[0-9]+\.[0-9]+` | VERIFIED |
| 23 | ISF-inspection plugin name and options | `volatility3/framework/plugins/isfinfo.py`; live run | v2.28.0 (plugin `_version = (2,0,0)`) | 2026-08-24 | Plugin is `isfinfo.IsfInfo`; options `--filter`, `--isf`, `--validate`, `--live`. Live run listed URI/Valid/base_types/types/symbols/enums/identifier | VERIFIED |
| 24 | isfinfo default mode collapses duplicate identifiers | live run with four copies of one ISF | v2.28.0 | 2026-08-24 | Without `--live` the plugin reads the identifier cache keyed by identifier; four files with the same banner produced one row. `--live` listed all four | VERIFIED |
| 25 | `isfinfo --validate` on a BTF-style ISF | live run | v2.28.0 + jsonschema 4.10.3 | 2026-08-24 | Unhandled `jsonschema.exceptions.ValidationError` traceback (not a `False` result) — `schemas.valid()` only catches `SchemaError` and `ImportError` | VERIFIED |
| 26 | Behaviour when `jsonschema` is not installed | `volatility3/schemas/__init__.py` lines 96-105 | v2.28.0 | 2026-08-24 | `ImportError` is caught, logged at INFO/DEBUG, and validation **returns True** — "All validations will report success, even with malformed input" | VERIFIED |
| 27 | Remote ISF retrieval mechanism | `constants.REMOTE_ISF_URL`, `cli` `-u/--remote-isf-url`, `--offline`, `RemoteIdentifierFormat` | v2.28.0 | 2026-08-24 | Default `REMOTE_ISF_URL = None`; no official server is configured. A `banners.json` of `{"version":1, "<os>": {"<b64 banner>": ["<url>", ...]}, "additional": [...]}` can be supplied with `-u` | VERIFIED |
| 28 | Remote ISF retrieval works end to end | live run: `vol.py -u https://github.com/Abyss-W4tcher/volatility3-symbols/raw/master/banners/banners.json isfinfo` | v2.28.0 | 2026-08-24 | 10,988 remote identifiers were inserted into `identifier.cache` (`local=0`); remote rows listed by `isfinfo` with zero statistics until downloaded | VERIFIED |
| 29 | Remote banner normalisation | `automagic/symbol_cache.py` `update()` | v2.28.0 | 2026-08-24 | Remote identifiers are `rstrip()`ed and a trailing `\x00` removed, with the code comment "Linux banners dumped by dwarf2json end with `\x00\n`". Observed dwarf2json 0.9.0 output actually ends `\n\x00`; the stripping still yields a banner ending in `\n`, which is what the remote list contains | CONFLICTING |
| 30 | Shipped remote-list generator script | `development/banner_server.py`; live run | v2.28.0 | 2026-08-24 | Fails immediately: `AttributeError: module 'volatility3.framework.automagic.linux' has no attribute 'LinuxBannerCache'`. The referenced classes exist nowhere else in the tree | DEPRECATED |
| 31 | Supported image/dump formats | `volatility3/framework/layers/` in v2.28.0 | v2.28.0 | 2026-08-24 | raw/padded, LiME (`lime.py`, magic `0x4C694D45`), AVML (`avml.py`, magic `0x4C4D5641`, **version 2 only**), ELF64 core (`elf.py`), Xen core (`xen.py`), QEMU suspend-to-disk (`qemu.py`), VMware (`vmware.py`), Windows crash dumps (`crash.py`), plus `leechcore` and `cloudstorage` URL handlers | VERIFIED |
| 32 | makedumpfile-compressed kdump support | grep for `KDUMP`/`makedumpfile`/`diskdump` across `volatility3/` | v2.28.0 | 2026-08-24 | No matches. Only **uncompressed ELF** core dumps are handled | NOT SUPPORTED |
| 33 | VMCOREINFO use | `automagic/linux.py` `LinuxIntelVMCOREINFOStacker`; `plugins/linux/vmcoreinfo.py` | v2.28.0 | 2026-08-24 | A VMCOREINFO ELF note is searched for; `NUMBER(phys_base)`, `KERNELOFFSET`, `SYMBOL(swapper_pg_dir)`, `OSRELEASE`, `CONFIG_X86_PAE` are used to derive KASLR shifts and the DTB. `linux.vmcoreinfo.VMCoreInfo` needs only a layer, no ISF | VERIFIED |
| 34 | Architecture support for Linux | `volatility3/framework/constants/architectures.py`; tree listing of v2.28.0 and `develop` | v2.28.0 / develop @2026-08-19 | 2026-08-24 | `LINUX_ARCHS = ["Intel32", "Intel64"]`, `LINUX_ARCHS_LAYERS = [intel.Intel]`. No `arm.py`/AArch64 code exists anywhere in either tree (case-insensitive grep for `aarch64` returns nothing) | VERIFIED |
| 35 | KASLR resolution without VMCOREINFO | `automagic/linux.py` `LinuxIntelStacker.find_aslr` | v2.28.0 | 2026-08-24 | Scans for `swapper(/0\|\x00\x00)\x00…`, validates `init_task` (pid 0, state, `active_mm == init_mm`, `tasks.next == tasks.prev`), derives `aslr_shift` from `init_task.files` vs `init_files`, and `kaslr_shift` from the physical location; both must be page-aligned | VERIFIED |
| 36 | Canonical failure text for missing symbols | `volatility3/cli/__init__.py` lines 674-710; live run | v2.28.0 | 2026-08-24 | `Unsatisfied requirement <path>: <description>` followed by "A symbol table requirement was not fulfilled. Please verify that: …" (4 bullet points). Reproduced live | VERIFIED |
| 37 | Stacking exceptions are hidden by default | `automagic/stacker.py` lines 213-235 | v2.28.0 | 2026-08-24 | Every exception raised inside a stacker (including schema `ValidationError` while loading a candidate ISF) is caught and logged only at `LOGLEVEL_VVV`/`VVVV` — i.e. visible with `-vvv`/`-vvvv` | VERIFIED |
| 38 | Official Volatility symbol packs for Linux | https://github.com/volatilityfoundation/volatility3-test-data/releases/download/v0.0.1/linux.zip ; SHA256SUMS at the same tag | v0.0.1 | 2026-08-24 | `linux.zip` = 2,980,184 bytes, sha256 `58bb7da2ed1e491ce922d04a59881d201e233b5605c9fd5a7f0c08ee528253c6`; contains **4** ISFs (CentOS 2.6.18, Debian 2.6.32/3.2.0/4.9.0), all dated 2024-11-07, under a `linux/` prefix | VERIFIED |
| 39 | Official position on Linux symbol availability | `README.md` of v2.28.0 | v2.28.0 | 2026-08-24 | "Mac and Linux symbol tables must be manually produced by a tool such as dwarf2json" and "an exhaustive set of Linux symbol tables cannot easily be supplied" | VERIFIED |
| 40 | Official docs recommend a community ISF repository | `doc/source/getting-started-linux-tutorial.rst` | v2.28.0 | 2026-08-24 | "It is recommended to first check the repository volatility3-symbols for pre-generated JSON.xz symbol table files" (links Abyss-W4tcher/volatility3-symbols) | VERIFIED |
| 41 | Exact-banner-match requirement | `doc/source/symbol-tables.rst` | v2.28.0 | 2026-08-24 | "Volatility 3 requires that the banners in the JSON file match the banners found in the image *exactly*, not just the version number … can include elements such as the compilation time and even the version of gcc" | VERIFIED |
| 42 | dwarf2json repository, owner, licence | https://api.github.com/repos/volatilityfoundation/dwarf2json ; `LICENSE.txt` | master HEAD `9f14607e0d` (2024-10-08) | 2026-08-24 | Owner: Volatility Foundation. Licence: Volatility Software License 1.0 (GitHub reports `NOASSERTION`). Default branch `master`; repo last pushed 2024-10-09 | VERIFIED |
| 43 | dwarf2json latest release and prebuilt binaries | https://api.github.com/repos/volatilityfoundation/dwarf2json/releases | v0.9.0 published 2024-10-09; v0.8.0 2024-04-18 | 2026-08-24 | Assets: `dwarf2json-linux-amd64`, `dwarf2json-darwin-amd64`, `dwarf2json-windows-amd64.exe`. **No arm64 assets** | VERIFIED |
| 44 | dwarf2json version string and emitted schema version | `dwarf2json --version` (release binary) and `main.go` constants | v0.9.0 | 2026-08-24 | Prints `dwarf2json 0.9.0` / `output schema 6.2.0`. `main.go`: `TOOL_VERSION = "0.9.0"`, `FORMAT_VERSION = "6.2.0"` | VERIFIED |
| 45 | dwarf2json Linux subcommand option list | `dwarf2json linux --help`; `main.go` lines 703-711 | v0.9.0 | 2026-08-24 | `--elf PATH`, `--elf-symbols PATH`, `--elf-types PATH`, `--system-map PATH` — all repeatable (`StringArray`). No other Linux options exist | VERIFIED |
| 46 | Semantics of each ELF-related option | `main.go` lines 755-780 (Extract bit flags) + live tests | v0.9.0 | 2026-08-24 | `--elf-types` → types only. `--elf` → `SymTabSymbols\|DwarfSymbols\|DwarfTypes\|ConstantData`. `--elf-symbols` → `SymTabSymbols\|ConstantData` (no types). `--system-map` → addresses only. Live output: types-only run produced 0 symbols; symbols-only run produced 36 symbols and 0 user types | VERIFIED |
| 47 | Which inputs can supply `linux_banner` | `main.go` `constantLinuxDataSymbols`, `readELFSymbol`, `processElfSymTab`, `processSystemMap` + live tests | v0.9.0 | 2026-08-24 | Constant data is read **only** from an ELF `.rodata` section using the symbol's size, and only for `linux_banner`. `--elf` and `--elf-symbols` can supply it; `--elf-types` and `--system-map` cannot. Live: `--system-map`-only ISF had `linux_banner` with an address but **no** `constant_data` | VERIFIED |
| 48 | Is `System.map` mandatory for dwarf2json? | live tests + `main.go` | v0.9.0 | 2026-08-24 | Not mandatory. `--elf` alone produced a complete ISF (types + symbols + banner). `System.map` is situational: it repairs/overrides addresses when the ELF symtab is absent or wrong | VERIFIED |
| 49 | Precedence when inputs conflict | `main.go` `main()` insertion order + `generateLinux()` loop + live test | v0.9.0 | 2026-08-24 | Files are processed in the order added: `--elf-types`, `--elf`, `--elf-symbols`, `--system-map`. Later writes overwrite `sym.Address`, so **System.map addresses win** over ELF symtab addresses. Verified live (all symbols became `0xffffffff81000000`) | VERIFIED |
| 50 | Does dwarf2json have any BTF input option? | `dwarf2json linux --help`; `grep -rin btf` over master tarball; `strings` over the release binary; repo issue/PR search | master HEAD `9f14607e0d`, v0.9.0 | 2026-08-24 | No BTF option, no BTF string in the binary, no occurrence of "btf" anywhere in the source, and **zero** issues or PRs mentioning BTF in the repository | NOT SUPPORTED |
| 51 | dwarf2json build requirements | `go.mod` | master | 2026-08-24 | `go 1.18`; single dependency `github.com/spf13/pflag v1.0.5`; single source file `main.go` (1206 lines) | VERIFIED |
| 52 | dwarf2json failure modes | live tests | v0.9.0 | 2026-08-24 | ELF without DWARF + `--elf`: `Failed linux processing: could not get DWARF from <f>: decoding dwarf section info at offset 0x0: too short`, exit 1. Fully stripped ELF + `--elf-symbols`: `error processing symtab: could not get symbols: no symbol section`, exit 1. In both cases a redirect left a **0-byte** output file | VERIFIED |
| 53 | dwarf2json memory exhaustion on large kernels | https://github.com/volatilityfoundation/dwarf2json/issues/60 | issue open since 2024-03-28 | 2026-08-24 | "Process Crash Due to Insufficient Memory … creating a large swap file (e.g. 8GB) can mitigate the issue". Peak RSS has now been **measured here** on real debug kernels: 2.71 GiB for 163 MB of `.debug_info`, 5.26 GiB for 315 MB (rows #113–#115). No OOM occurred on a 31 GB machine | VERIFIED |
| 54 | Typical ISF size and compression | local ISFs generated by dwarf2json 0.9.0 | Ubuntu 6.8.0-101-generic; 5.4.0-1133-kvm | 2026-08-24 | 6.8.0-101-generic: 64,295,265 B JSON → 3,325,452 B with `xz -6 -T0` (≈19:1, 3.0 s, 345 MB peak RSS for xz). 5.4.0-1133-kvm: 26,875,065 B JSON. Type/symbol counts: 13,948/294,116 and 7,515/109,467 respectively | VERIFIED |
| 55 | Known dwarf2json correctness issues | issues #63, #57, #65, #67; `volatility3/schemas/__init__.py` | 0.9.0 | 2026-08-24 | Rust/C name collisions (#63) are guarded in Volatility: ISFs produced by dwarf2json < 0.9.0 containing `rust_helper_BUG` trigger a warning. #65 (multi-compile-unit handling) and #67 (typedef not found) remain open | VERIFIED |
| 56 | Existence and identity of BTF→ISF tools | GitHub search `btf2json`, `btf+volatility` | — | 2026-08-24 | Three relevant projects: `vobst/btf2json` (Rust, MIT), `eurecom-s3/lemon-btf2json` (Rust, MIT, derived from vobst), `Nissen96/vol3-profiler` (Python wrapper). **None** belongs to the Volatility Foundation | VERIFIED |
| 57 | btf2json (vobst) metadata | https://api.github.com/repos/vobst/btf2json | v0.1.0 (2024-11-10); master HEAD `4a64f9c322` (2024-12-21) | 2026-08-24 | MIT licence, 35 stars, 0 open issues, no commits since 2024-12-21 (≈20 months before this check) | VERIFIED |
| 58 | btf2json option list | `src/cli.rs` (master) | HEAD `4a64f9c322` | 2026-08-24 | `--btf`, `--map`, `--banner`, `--version`, `--verbose`, `--debug`, `--image`. The `--image` help text itself says "(not implemented)" | VERIFIED |
| 59 | btf2json `--image` is inert | `src/symbols.rs`, `src/btf.rs` | HEAD `4a64f9c322` | 2026-08-24 | Both code paths `bail!()`: "Extraction of symbols from memory image is not implemented." / "BTF extraction from memory image is not implemented!" | NOT SUPPORTED |
| 60 | btf2json required inputs | `src/symbols.rs` `TryFrom<&Cli> for SymbolsBuilder`; `src/btf.rs` | HEAD `4a64f9c322` | 2026-08-24 | `--map` is the only working symbol source → effectively mandatory. `--btf` is mandatory for types. `--banner` is mandatory whenever the `--btf` input cannot yield a banner (raw BTF blob, or an ELF with no symbol table) | VERIFIED |
| 61 | btf2json accepted BTF input forms | `src/btf.rs` `get_btf_section()`, `src/elf.rs` | HEAD `4a64f9c322` | 2026-08-24 | Accepts a raw BTF blob (magic `0xeb9f`, either endianness) or an ELF containing a `.BTF` section. A live kernel's `/sys/kernel/btf/vmlinux` is the raw-blob form | VERIFIED |
| 62 | btf2json embedded symbol-to-type database | `src/symbols.rs` (`RustEmbed`, `symdb/dummy.symdb`) | HEAD `4a64f9c322` | 2026-08-24 | `symdb/dummy.symdb` has 239,380 lines of `"<symbol> <ISF type descriptor JSON>"`, compiled into the binary via `rust-embed`. It supplies the *type of each symbol*; it is not user-supplied and cannot be swapped at run time without rebuilding | VERIFIED |
| 63 | btf2json does not take symbol types from BTF | `src/symbols.rs` `add_types_from_btf()` | HEAD `4a64f9c322` | 2026-08-24 | The function body is `log::info!("Types from BTF not implemented."); self` — a stub. Symbol types therefore come only from the embedded symdb; BTF supplies `user_types`/`enums`/`base_types` | VERIFIED |
| 64 | btf2json output schema version and metadata kinds | `src/metadata.rs` | HEAD `4a64f9c322` | 2026-08-24 | Emits `format: "6.2.0"`, `producer.name = "btf2json"`, and source kinds `btf`, `symdb`, `system-map` | VERIFIED |
| 65 | Does btf2json output validate against stock Volatility? | rows 6, 7, 64; project README | v2.28.0 vs btf2json HEAD | 2026-08-24 | No. The README ships a patch changing the 6.2.0 schema pattern to `^(btf\|symdb\|dwarf\|symtab\|system-map)$`. Without it, validation raises `ValidationError` | VERIFIED |
| 66 | lemon-btf2json identity and delta | https://api.github.com/repos/eurecom-s3/lemon-btf2json ; source diff vs vobst master | main HEAD `211005007e` (2026-05-07) | 2026-08-24 | MIT; not marked as a GitHub fork but is a source-level derivative. Adds a **required** `--vabits <u8>` flag and injects a synthetic `__lemon_va_bits` symbol into the ISF; ships `utilities/btf_support.patch` (same schema relaxation) | VERIFIED |
| 67 | lemon-btf2json requires a forked Volatility | repo README; row 34 | main HEAD `211005007e` | 2026-08-24 | README directs users to `eurecom-s3/volatility3-arm64` ("Volatility 3.0 with ARM64 support", 1 star, pushed 2026-05-07). The `__lemon_va_bits` symbol has no consumer in upstream Volatility | VERIFIED |
| 68 | lemon-btf2json README references a missing script | repo tree; README | main HEAD `211005007e` | 2026-08-24 | README says a warning "is normal and corrected by `patch_profile.py`", but no such file exists in the repository | CONFLICTING |
| 69 | vol3-profiler identity and behaviour | https://api.github.com/repos/Nissen96/vol3-profiler ; `main.py`, `pyproject.toml` | main HEAD `7364750c39` (2026-08-03) | 2026-08-24 | `pyproject.toml` declares MIT but the repository has no LICENSE file (GitHub API reports no licence). Python ≥3.12. Extracts the banner from the dump, resolves Ubuntu/Debian packages over APT, decompresses `vmlinuz` by scanning gzip/xz/lz4/zstd magics, then runs `btf2json --btf <vmlinux> --map <System.map> --banner "<banner>"` and xz-compresses the result. It does **not** patch the Volatility schema | VERIFIED |
| 70 | Upstream Volatility position on BTF | GitHub issue/PR search in `volatilityfoundation/volatility3` and `dwarf2json` | — | 2026-08-24 | No issue or PR with "BTF" in the title in either repository; no BTF-related code in either. Nine incidental matches in volatility3 are about eBPF plugins/Android questions, not ISF generation from BTF | NOT SUPPORTED |
| 71 | Community ISF collection: Abyss-W4tcher | https://github.com/Abyss-W4tcher/volatility3-symbols (README, contents API) | master, pushed 2026-07-29 | 2026-08-24 | 317 stars, ~29 GB, **no licence declared**. Badge claims 10,988 symbols. Covers Ubuntu (amd64/i386), Debian, Kali, AlmaLinux, RockyLinux (x86_64), macOS. Provides `banners/banners.json` (4,710,045 B) for `--remote-isf-url` and `banners/banners_plain.json` (3,305,839 B). Carries a 2025-03-27 warning that many macOS ISFs > 11.0 are incorrect | VERIFIED |
| 72 | Community ISF collection: leludo84 | https://api.github.com/repos/leludo84/vol3-linux-profiles | pushed 2026-08-18 | 2026-08-24 | 84 stars, ~16 GB, no licence declared | VERIFIED |
| 73 | Ubuntu kernel debug packages exist and are named predictably | http://ddebs.ubuntu.com/pool/main/l/linux/ and .../linux-hwe-6.8/ | listing fetched 2026-08-24 | 2026-08-24 | `linux-image-unsigned-<release>-<flavour>-dbgsym_<pkgver>_<arch>.ddeb`; 5,242 `.ddeb` entries in `pool/main/l/linux/`, 2,621 containing `dbgsym`. HWE kernels live under their own source package (e.g. `linux-hwe-6.8`) | VERIFIED |
| 74 | Ubuntu debug packages are pruned over time | same listing | checked 2026-08-24 | 2026-08-24 | For 6.8.0-101 only `arm64` and `s390x` dbgsyms remain; the amd64 dbgsym returns HTTP 404. Newer 7.0.0-* amd64 dbgsyms are present. Example size: `linux-image-unsigned-7.0.0-30-generic-dbgsym_7.0.0-30.30_amd64.ddeb` = 304,349,184 B | VERIFIED |
| 75 | Ubuntu ddeb repository setup | https://wiki.ubuntu.com/Debug%20Symbol%20Packages | wiki, read 2026-08-24 | 2026-08-24 | `deb http://ddebs.ubuntu.com <codename>{,-updates,-proposed} main restricted universe multiverse` plus `sudo apt install ubuntu-dbgsym-keyring` (18.04+) | VERIFIED |
| 76 | Where Ubuntu installs the debug `vmlinux` | https://git.launchpad.net/~ubuntu-kernel/ubuntu/+source/linux/+git/noble/plain/debian/rules.d/2-binary-arch.mk | noble branch, fetched 2026-08-24 | 2026-08-24 | `install -m644 -D $(builddir)/build-$*/vmlinux $(dbgpkgdir)/usr/lib/debug/boot/vmlinux-$(abi_release)-$*`; `System.map` is installed **mode 0600** to `/boot/System.map-<abi>-<flavour>`; debug modules under `/usr/lib/debug/lib/modules/...` | VERIFIED |
| 77 | Debian kernel debug packages and paths | https://deb.debian.org/debian/pool/main/l/linux/ ; https://packages.debian.org/bullseye/i386/linux-image-5.10.0-30-686-dbg/filelist | listing/filelist fetched 2026-08-24 | 2026-08-24 | Package `linux-image-<release>-<flavour>-dbg`. Contains `/usr/lib/debug/boot/vmlinux-<release>`, `/usr/lib/debug/boot/System.map-<release>`, `/usr/lib/debug/lib/modules/<release>/…`, and `/usr/lib/debug/.build-id/…` | VERIFIED |
| 78 | Debian debug mirror reachability from this environment | https://debug.mirrors.debian.org/debian-debug/... | — | 2026-08-24 | `curl` failed with "SSL certificate problem: self-signed certificate in certificate chain" — a property of this sandbox's TLS interception, not of Debian | UNVERIFIED |
| 79 | Fedora kernel debuginfo package and path | https://src.fedoraproject.org/rpms/kernel/raw/rawhide/f/kernel.spec ; https://dl.fedoraproject.org/pub/fedora/linux/releases/43/Everything/x86_64/debug/tree/Packages/k/ | rawhide spec; F43 tree | 2026-08-24 | Spec defines `%define debuginfodir /usr/lib/debug` and moves `vmlinux` to `%{debuginfodir}/lib/modules/$KernelVer/vmlinux`; it also records the build ID in `/lib/modules/$KernelVer/build/vmlinux.id`. F43 tree contains `kernel-debuginfo-6.17.1-300.fc43.x86_64.rpm` | VERIFIED |
| 80 | Rocky Linux debuginfo availability | https://dl.rockylinux.org/pub/rocky/9/BaseOS/x86_64/debug/tree/Packages/k/ | Rocky 9 tree | 2026-08-24 | `kernel-debuginfo-5.14.0-687.*.el9_8.x86_64.rpm` present. Example size 775,461,069 B | VERIFIED |
| 81 | AlmaLinux debuginfo availability | https://repo.almalinux.org/almalinux/9/BaseOS/x86_64/ and https://repo.almalinux.org/vault/9.5/BaseOS/debug/x86_64/Packages/ (redirects to vault.almalinux.org) | Alma 9 / 9.5 vault | 2026-08-24 | The current 9 tree exposes only `kickstart/` and `os/` — no `debug/`. The vault does: `https://vault.almalinux.org/9.5/BaseOS/debug/x86_64/Packages/` lists `kernel-debuginfo-5.14.0-503.*.el9_5.x86_64.rpm` | VERIFIED |
| 82 | CentOS debuginfo host reachability | https://debuginfo.centos.org/ | — | 2026-08-24 | Connection timed out after 30 s from this environment; could not be checked | UNVERIFIED |
| 83 | Arch Linux kernel debug symbols | https://geo.mirror.pkgbuild.com/core-debug/os/x86_64/ and .../extra-debug/os/x86_64/ | listings fetched 2026-08-24 | 2026-08-24 | Debug repositories exist (172 packages in `core-debug`), but there is **no `linux-debug` package**; matches such as `util-linux-debug` are unrelated. Arch does not publish kernel debug symbols as a package | VERIFIED |
| 84 | openSUSE kernel debuginfo | https://download.opensuse.org/debug/distribution/leap/15.6/repo/oss/x86_64/ and https://download.opensuse.org/debug/tumbleweed/repo/oss/x86_64/ | listings fetched 2026-08-24 | 2026-08-24 | `kernel-default-debuginfo-6.4.0-150600.21.3.x86_64.rpm` (Leap 15.6) and `kernel-default-debuginfo-7.1.8-1.1.x86_64.rpm` (Tumbleweed) present | VERIFIED |
| 85 | SLES kernel debuginfo | — | — | 2026-08-24 | Behind a subscription; not reachable anonymously and not tested | UNVERIFIED |
| 86 | debuginfod servers and client | https://sourceware.org/elfutils/Debuginfod.html | page read 2026-08-24 | 2026-08-24 | Public servers listed for Debian, Fedora, openSUSE, Ubuntu, Arch, CentOS, plus a federating `debuginfod.elfutils.org`. Client: `DEBUGINFOD_URLS`, `debuginfod-find debuginfo <buildid>`; `debuginfod-find` is not installed here, so the HTTP API was driven with `curl` instead. Kernel coverage is **now verified** — see row #121 | VERIFIED |
| 87 | Kernel config options controlling DWARF | https://raw.githubusercontent.com/torvalds/linux/master/lib/Kconfig.debug | master, fetched 2026-08-24 | 2026-08-24 | Choice of `DEBUG_INFO_NONE`, `DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT`, `DEBUG_INFO_DWARF4`, `DEBUG_INFO_DWARF5`; modifiers `DEBUG_INFO_REDUCED`, `DEBUG_INFO_SPLIT` (.dwo), `DEBUG_INFO_COMPRESSED_{NONE,ZLIB,ZSTD}` | VERIFIED |
| 88 | Kernel config options controlling BTF | same file | master | 2026-08-24 | `DEBUG_INFO_BTF` depends on `!DEBUG_INFO_SPLIT && !DEBUG_INFO_REDUCED`, `BPF_SYSCALL`, and **`PAHOLE_VERSION >= 122`** ("requires pahole v1.22 or later, which will convert DWARF type info into equivalent deduplicated BTF"). `DEBUG_INFO_BTF_MODULES` (default y) generates *split* BTF for modules; `MODULE_ALLOW_BTF_MISMATCH` relaxes load-time checks | VERIFIED |
| 89 | How the kernel build produces BTF | https://raw.githubusercontent.com/torvalds/linux/master/scripts/gen-btf.sh and scripts/link-vmlinux.sh | master | 2026-08-24 | "1. pahole generates BTF from DWARF data 2. resolve_btfids … 3. the result gets linked/objcopied into the target binary". vmlinux: `pahole -J --btf_encode_detached`, then `objcopy --add-section .BTF=…`. Modules: `--btf_base <vmlinux>` producing split BTF embedded with `objcopy --add-section .BTF` / `.BTF.base` **into the module file itself** | VERIFIED |
| 90 | pahole / dwarves project | https://api.github.com/repos/acmel/dwarves (mirror of git.kernel.org/pub/scm/devel/pahole/pahole.git) | latest tag `v1.31`; pushed 2026-08-03 | 2026-08-24 | GPL-2.0. `pahole` is the BTF encoder used by the kernel build. Not installed on this workstation, so no local invocation was tested | VERIFIED (metadata) / UNVERIFIED (local run) |
| 91 | bpftool BTF syntax | `bpftool btf help` on this workstation | bpftool v7.7.0, libbpf v1.7 | 2026-08-24 | `bpftool btf dump BTF_SRC [format FORMAT]`, `BTF_SRC := { id \| prog \| map \| file FILE }`, `FORMAT := { raw \| c [unsorted] }`, option `-B/--base-btf` for split BTF | VERIFIED |
| 92 | `/sys/kernel/btf/vmlinux` is a raw BTF blob | live workstation | kernel 7.0.0-28-generic | 2026-08-24 | 7,114,296 bytes, mode 0444 root-owned; `file` reports `data`; first bytes `9f eb 01 00 18 00 00 00` (BTF magic 0xeb9f, version 1, hdr_len 0x18); `readelf -h` fails with "Not an ELF file - it has the wrong magic bytes" | VERIFIED |
| 93 | `/proc/kallsyms` under `kptr_restrict` | live workstation | kernel 7.0.0-28-generic | 2026-08-24 | `kptr_restrict = 1`; as a non-root user every address reads `0000000000000000` while symbol names remain visible | VERIFIED |
| 94 | `/boot` artifact permissions | live workstation | Ubuntu 24.04 | 2026-08-24 | `/boot/vmlinuz-7.0.0-28-generic` and `/boot/System.map-7.0.0-28-generic` are mode `0600 root:root`; `/boot/config-*` is world-readable | VERIFIED |
| 95 | Distribution kernels do carry BTF and DWARF5 | `/boot/config-7.0.0-28-generic` | Ubuntu HWE 7.0.0-28 | 2026-08-24 | `CONFIG_DEBUG_INFO=y`, `CONFIG_DEBUG_INFO_DWARF5=y`, `CONFIG_DEBUG_INFO_BTF=y`, `CONFIG_DEBUG_INFO_BTF_MODULES=y`, `CONFIG_RANDOMIZE_BASE=y`, `CONFIG_KALLSYMS_ALL=y`, `CONFIG_LTO_NONE=y`, `CONFIG_DEBUG_INFO_COMPRESSED_NONE=y` | VERIFIED |
| 96 | `extract-vmlinux` behaviour | `/usr/src/linux-headers-7.0.0-28-generic/scripts/extract-vmlinux` (in-tree script, GPL-2.0) | Ubuntu headers 7.0.0-28 | 2026-08-24 | Tries gunzip, unxz, bunzip2, unlzma, lzop, lz4, unzstd, then raw; writes the result to **stdout**; never modifies the input | VERIFIED |
| 97 | What an extracted `vmlinux` actually contains | live test on Debian installer kernel `linux` (bzImage, 8,222,656 B, sha256 `d8808aa4…`) for `6.1.0-50-amd64` | Debian 6.1.176-1 | 2026-08-24 | Extraction succeeded ("Extracted vmlinux using 'unxz' from offset 21197") giving a 65,905,060-byte ELF with a GNU build ID. Sections present: `.BTF`, `.BTF_ids`, `.notes`, `.data`, … **No `.symtab`, no `.debug_*`**. `nm` reports "no symbols". `dwarf2json linux --elf` fails with the DWARF error of row 52 | VERIFIED |
| 98 | Extracting the `.BTF` section non-destructively | live test, binutils 2.42 | — | 2026-08-24 | `objcopy --dump-section .BTF=kernel.btf vmlinux /dev/null` produced a 4,351,479-byte blob starting `9f eb 01 00` and left the input's sha256 unchanged; `bpftool btf dump file kernel.btf` printed 236,994 lines | VERIFIED |
| 99 | `objcopy` rewrites its input when no output operand is given | live test, binutils 2.42 | — | 2026-08-24 | `objcopy --dump-section .text=out.bin t.elf` left the content identical but **changed the mtime** (file rewritten). `objcopy --add-section .foo=payload t.elf` **changed the content**. With an explicit output operand (`… t.elf /dev/null`) both hash and mtime were unchanged | VERIFIED |
| 100 | Kernel modules as shipped by a distribution | `/lib/modules/7.0.0-28-generic/kernel/fs/binfmt_misc.ko.zst` | Ubuntu 7.0.0-28 | 2026-08-24 | Modules are zstd-compressed. Decompressed: ELF relocatable with `.BTF`, `.symtab`, `.modinfo`, GNU build ID — but **no `.debug_*`** | VERIFIED |
| 101 | Volatility recovers module symbols from memory, not from module ISFs | `volatility3/framework/symbols/linux/kallsyms.py`, `plugins/linux/kallsyms.py` | v2.28.0 | 2026-08-24 | `linux.kallsyms.Kallsyms` (v1.0.0) walks in-memory kallsyms/`__ksymtab` structures, including per-module `module.syms`/`module.num_syms`, using types from the **kernel** ISF. There is no separate per-module ISF mechanism | VERIFIED |
| 102 | Number of Linux plugins | `python3 vol.py -h` on v2.28.0 | v2.28.0 | 2026-08-24 | 59 `linux.*` plugin entries listed | VERIFIED |
| 103 | Volatility 2 status and historical Linux workflow | https://api.github.com/repos/volatilityfoundation/volatility ; tools/linux/Makefile | archived; last push 2025-05-16 | 2026-08-24 | Repository is **archived** (read-only), GPL-2.0. `tools/linux/Makefile` builds `module.ko` with `CONFIG_DEBUG_INFO=y` and runs `dwarfdump -di module.ko > module.dwarf` — the Volatility 2 profile workflow. Nothing in it applies to Volatility 3 | DEPRECATED |
| 104 | vmlinux-to-elf capability | https://raw.githubusercontent.com/marin-m/vmlinux-to-elf/master/README.md ; repo metadata | master, pushed 2026-06-05; GPL-3.0; 1,805 stars | 2026-08-24 | Recovers a **symbol table** by parsing the compressed in-image kallsyms blob and emits an analyzable ELF. Commands: `vmlinux-to-elf <in> <out>`, `kallsyms-finder`, `vmlinuz-decompressor`. It does not and cannot restore DWARF type information | VERIFIED |
| 105 | WSL2 kernel debug configuration | https://raw.githubusercontent.com/microsoft/WSL2-Linux-Kernel/master/Microsoft/config-wsl | master, pushed 2026-08-01 | 2026-08-24 | 2,932-line config: `CONFIG_KALLSYMS=y`, `CONFIG_KALLSYMS_BASE_RELATIVE=y`, `CONFIG_RANDOMIZE_BASE=y`; **no `CONFIG_DEBUG_INFO*` and no `CONFIG_DEBUG_INFO_BTF`** — Microsoft's shipped WSL2 kernel is built without DWARF and without BTF | VERIFIED |
| 106 | Android GKI artifact availability | https://source.android.com/docs/core/architecture/kernel/gki-release-builds and the per-branch release-build pages | pages read 2026-08-24 | 2026-08-24 | The overview page does not enumerate artifacts; the per-branch pages do — see row #123. GKI/ACK branches are named `<android-release>-<kernel-version>` (e.g. `android14-6.1`) | VERIFIED (superseded by #123) |
| 107 | Acquisition tools referenced by the docs | https://api.github.com/repos/microsoft/avml ; https://api.github.com/repos/504ensicsLabs/LiME (redirects) | AVML v0.20.0 (2026-07-08), MIT; LiME now `jtsylve/LiME`, GPL-2.0, pushed 2026-04-05 | 2026-08-24 | The Volatility 3 Linux tutorial names AVML. The historic `504ensicsLabs/LiME` URL now redirects to `jtsylve/LiME`. Volatility's AVML layer accepts **version 2** AVML files only | VERIFIED |
| 108 | Real ISF metadata produced by dwarf2json 0.9.0 | ISFs on this workstation for Ubuntu 6.8.0-101-generic and 5.4.0-1133-kvm | dwarf2json 0.9.0, format 6.2.0 | 2026-08-24 | `metadata.linux.symbols` = `[{kind: dwarf}, {kind: symtab}]` (+ `{kind: system-map}` when a map was supplied), `metadata.linux.types` = `[{kind: dwarf}]`, each with `hash_type: sha256` and the input file's hash. Exactly one symbol carries `constant_data`: `linux_banner` | VERIFIED |
| 109 | Banner bytes stored in a real ISF | ISFs on this workstation | — | 2026-08-24 | 6.8.0-101-generic: 260 bytes ending `…6.8.12)\n\x00`. Two ISFs built from the same 5.4.0-1133-kvm `vmlinux` differ: 187 bytes ending `\n\x00` versus 186 bytes ending `)\x00`. **Cause established on 2026-08-24 — see rows #117–#119: dwarf2json emits the 187-byte form; the shorter file was post-processed and can never match** | VERIFIED (resolved) |
| 110 | Banner strings recoverable with `strings` | live test on the extracted Debian 6.1.0-50 vmlinux | — | 2026-08-24 | Two near-identical strings exist: one containing `# SMP …` at offset 18,874,784 and one containing `#1 SMP …` at 20,321,248. Only the latter matches the running-kernel banner (`uname -v` form). Both terminate `\n\0` | VERIFIED |
| 111 | Plugin name forms accepted by the CLI | `python3 vol.py linux.pslist -h` and `… linux.pslist.PsList -h` | v2.28.0 | 2026-08-24 | Both resolve; both print `usage: vol.py linux.pslist.PsList …`. `banners` likewise resolves to `banners.Banners` | VERIFIED |
| 112 | Plugin names used in this guide exist in 2.28.0 | `python3 vol.py -h` plugin list | v2.28.0 | 2026-08-24 | Confirmed present: `linux.pslist.PsList`, `linux.pstree.PsTree`, `linux.lsmod.Lsmod`, `linux.lsof.Lsof`, `linux.mountinfo.MountInfo`, `linux.ip.Addr`, `linux.sockstat.Sockstat`, `linux.kmsg.Kmsg`, `linux.proc.Maps`, `linux.elfs.Elfs`, `linux.malware.check_syscall.Check_syscall`, `linux.kallsyms.Kallsyms`, `linux.vmcoreinfo.VMCoreInfo`, `linux.check_modules.Check_modules`, `linux.modxview.Modxview`, `linux.module_extract.ModuleExtract`. Note some malware plugins exist under both `linux.<name>` and `linux.malware.<name>` | VERIFIED |
| 113 | dwarf2json cost on a real debug vmlinux (5.4) | Ubuntu `linux-image-unsigned-5.4.0-1133-kvm-dbgsym_5.4.0-1133.142_amd64.ddeb` (192,436,808 B, sha256 `29599379…`); vmlinux 529,409,144 B, sha256 `4d515cb4…`, `.debug_info` 162,931,047 B | dwarf2json 0.9.0 | 2026-08-24 | `linux --elf`: 18.97 s wall (33.92 user + 5.92 sys), **peak RSS 2,836,656 KB**, output 26,874,845 B, 7,515 user_types / 1,199 enums / 109,467 symbols, banner 187 B ending `\n\x00` | VERIFIED |
| 114 | Per-option cost on the same input | same | dwarf2json 0.9.0 | 2026-08-24 | `--elf-types`: 15.75 s, RSS 2,520,320 KB, 8,938,347 B, 0 symbols. `--elf-symbols`: **1.10 s, RSS 78,136 KB**, 12,119,796 B, 82,134 symbols, banner present. `--elf --system-map`: 18.42 s, RSS 2,840,220 KB, 26,875,069 B | VERIFIED |
| 115 | dwarf2json cost on a modern DWARF5 kernel | Ubuntu `linux-image-unsigned-7.0.0-30-generic-dbgsym_7.0.0-30.30_amd64.ddeb` (304,349,184 B, sha256 `da33de63…`); vmlinux 505,823,072 B, sha256 `51be07e2…`, `.debug_info` 314,673,188 B, DWARF5 sections (`.debug_loclists`, `.debug_line_str`, `.debug_names`) | dwarf2json 0.9.0 | 2026-08-24 | `linux --elf`: 36.38 s wall (63.90 user + 12.73 sys), **peak RSS 5,514,468 KB**, output 77,485,996 B, 15,517 user_types / 2,825 enums / 360,733 symbols, banner 248 B | VERIFIED |
| 116 | dwarf2json on a debuginfod-delivered kernel | vmlinux for `2.6.32-754.24.2.el6.centos.plus.x86_64`, 139,554,079 B, sha256 `0493b6be…` | dwarf2json 0.9.0 | 2026-08-24 | Sections `.debug_info` + `.symtab`, no `.BTF`. `linux --elf`: 8.23 s, peak RSS 1,464,096 KB, output 19,606,273 B, 4,678 user_types / 82,268 symbols, banner 180 B ending `\n\x00` | VERIFIED |
| 117 | dwarf2json output is deterministic and provenance is exact | regenerated ISF vs `/opt/volatility3/volatility3/symbols/linux-isf.json` | dwarf2json 0.9.0 | 2026-08-24 | The `--elf` run reproduced the pre-existing ISF **byte for byte** (both sha256 `4e723b6db49faf7ac69516410b720d6e19cd528783e05b753f7a14e0ff3c243a`, 26,874,845 B). The vmlinux hash in that ISF's metadata (`4d515cb4…`) equals the hash of the file extracted from the official ddeb, and the System.map hash in the sibling ISF (`d76fc018…`) equals the map extracted from `linux-modules` | VERIFIED |
| 118 | Cause of the 186-byte banner anomaly (resolves row #109) | structural diff of the regenerated `--elf --system-map` ISF against `/opt/volatility3/volatility3/symbols/5.4.0-1133-kvm.json` | dwarf2json 0.9.0 | 2026-08-24 | The two documents differ in **exactly one value**: `symbols.linux_banner.constant_data`. Mine 187 B ending `\n\x00`; the stored one 186 B, identical except the newline is removed (`theirs == mine.replace(b"\n\x00", b"\x00")` → True). `metadata`, `base_types`, `user_types`, `enums` and all 109,467 symbol entries are identical. The stored file was therefore post-processed after generation | VERIFIED |
| 119 | A one-byte banner difference makes an ISF unusable | live run of `linux.pslist` at `-vvv` against a synthetic image containing the true banner bytes, with each ISF alone in its own symbol directory and its own cache | Volatility 3 2.28.0 | 2026-08-24 | Correct ISF → `Identified banner: b'…5.4.291)\n\x00'` from both `automagic.linux` and `automagic.symbol_finder`, then `Using symbol library: …/sym-good/kernel.json`. Newline-stripped ISF → only `No suitable linux banner could be matched`, twice, and no symbol library. Both files are valid JSON and schema-valid | VERIFIED |
| 120 | Which Ubuntu package holds which kernel artifact | unpacking `…-dbgsym…ddeb`, `linux-image-unsigned-5.4.0-1133-kvm_5.4.0-1133.142_amd64.deb` (6,460,660 B) and `linux-modules-5.4.0-1133-kvm_5.4.0-1133.142_amd64.deb` (10,930,124 B) | Ubuntu focal `linux-kvm` 5.4.0-1133.142 | 2026-08-24 | dbgsym → `/usr/lib/debug/boot/vmlinux-<release>` (529,409,144 B) and `/usr/lib/debug/lib/modules/<release>/…`, **no System.map**. linux-image-unsigned → only `/boot/vmlinuz-<release>` (mode 0600). linux-modules → `/boot/System.map-<release>` (3,076,539 B, mode 0600) and `/boot/config-<release>` (87,367 B) | VERIFIED |
| 121 | debuginfod can deliver kernel debug information | `https://debuginfod.elfutils.org/metadata?key=glob&value=/usr/lib/debug/lib/modules/*/vmlinux` and `/buildid/<id>/debuginfo` | elfutils federating server | 2026-08-24 | Metadata query returned **1,164** results, each with `buildid`, `file` and the source `archive` (e.g. `kernel-debuginfo-2.6.32-754.24.2.el6.centos.plus.x86_64.rpm`). Fetching build ID `1e817974…` delivered a 139,554,079-byte ELF, `file` → "with debug_info, not stripped"; response headers `x-debuginfod-size`, `x-debuginfod-archive`, `x-debuginfod-file`. **But**: the same server returned HTTP 404 for the build IDs of both current Ubuntu kernels (`ca1c4a16…`, `f2ce0f4b…`), and `https://debuginfod.ubuntu.com/` timed out from this environment | VERIFIED (works; coverage uneven) |
| 122 | pahole cross-check and BTF encoding cost | `pahole` v1.25 (Ubuntu noble package, unpacked with `dpkg-deb -x`, run without root) against `vmlinux-5.4.0-1133-kvm` | pahole 1.25 | 2026-08-24 | `pahole -C task_struct` reported `/* size: 6784, cachelines: 106, members: 192 */`, `tasks` @728, `pid` @984, `comm[16]` @1424 — identical to the dwarf2json ISF (size 6784; 728 / 984 / 1424). `pahole -J --btf_encode_detached=out.btf`: 4.95 s, peak RSS 452,776 KB, 3,313,298-byte BTF (magic `9feb0100`), input hash unchanged; the distribution's own embedded `.BTF` is 3,313,978 B | VERIFIED |
| 123 | Android GKI release artifacts are enumerated | `https://source.android.com/docs/core/architecture/kernel/gki-android16-6_12-release-builds` | AOSP docs | 2026-08-24 | The artifacts column "displays the list of kernel or debug kernel artifacts in the respective release"; the artifact table names `Image`, `boot.img`, **`System.map`**, **`vmlinux`** ("a statically-linked executable containing the Linux kernel") and `vmlinux.symvers`, with debug-kernel variants such as `boot-6.12-allsyms.img`; artifacts come from ci.android.com. Whether a given published `vmlinux` carries DWARF was not verified | VERIFIED (listing) / UNVERIFIED (DWARF content) |
| 124 | CentOS debuginfo host, second attempt | `https://debuginfo.centos.org/` | — | 2026-08-24 | Timed out again (25 s, no response) from this environment | UNVERIFIED |

---

## Appendix B — Command verification

One row per command that appears in this guide, plus rows for commands readers are likely to have seen
elsewhere. Options are written in their correct, fully scoped form (subcommand-scoped options are shown
with their subcommand). "Run here" means the command was executed in the verification environment on
2026-08-24.

| Command | Tool version / commit | Verified against | Status / notes |
|---|---|---|---|
| `python3 vol.py -f IMAGE banners` | Volatility 3 2.28.0 | Run here on a synthetic image | **VERIFIED** — plugin is `banners.Banners`; output columns Offset, Banner |
| `python3 vol.py -f IMAGE linux.banners` | Volatility 3 2.28.0 | Plugin list (`vol.py -h`) | **NOT SUPPORTED** — no such plugin; use `banners` |
| `python3 vol.py isfinfo` | Volatility 3 2.28.0 | Run here | **VERIFIED** — cache-backed listing; duplicate identifiers collapse to one row |
| `python3 vol.py isfinfo --live` | Volatility 3 2.28.0 | Run here | **VERIFIED** — walks every symbol path instead of the cache |
| `python3 vol.py isfinfo --validate` | Volatility 3 2.28.0 + jsonschema 4.10.3 | Run here | **VERIFIED** — but an invalid ISF produces an unhandled `ValidationError` traceback (log #25) |
| `python3 vol.py isfinfo --filter STRING` | Volatility 3 2.28.0 | Run here | **VERIFIED** — substring match on the URI |
| `python3 vol.py isfinfo --isf URL` | Volatility 3 2.28.0 | Source (`isfinfo.py` `URIRequirement`) | **VERIFIED** (option exists; combine with `--live`) |
| `python3 vol.py -f IMAGE linux.vmcoreinfo` | Volatility 3 2.28.0 | Source; plugin needs only a layer | **VERIFIED** (no ELF/kdump image was available here to run it against) |
| `python3 vol.py -f IMAGE linux.kallsyms --core --modules --ftrace --bpf` | Volatility 3 2.28.0 | Source (`plugins/linux/kallsyms.py`) | **VERIFIED** (options exist; not run — needs a real image + ISF) |
| `python3 vol.py -s DIR1;DIR2 …` | Volatility 3 2.28.0 | Source + run here | **VERIFIED** — **semicolon** separated, prepended to the defaults |
| `python3 vol.py --symbol-dirs DIR` | Volatility 3 2.28.0 | Source (`cli/__init__.py`) | **VERIFIED** — long form of `-s` |
| `python3 vol.py --clear-cache` | Volatility 3 2.28.0 | Source (`framework.clear_cache`) | **VERIFIED** — unlinks `*.cache` and `identifier.cache` |
| `python3 vol.py --cache-path DIR` | Volatility 3 2.28.0 | Run here | **VERIFIED**, with a caveat: the directory must already exist, else `FileNotFoundError` (log #20). The shipped docs say the cache path cannot be altered — outdated |
| `python3 vol.py --remote-isf-url URL` (`-u URL`) | Volatility 3 2.28.0 | Run here against the Abyss-W4tcher index | **VERIFIED** — 10,988 remote identifiers cached (log #28) |
| `python3 vol.py --offline` | Volatility 3 2.28.0 | Source | **VERIFIED** — mutually exclusive with `-u` |
| `VOLATILITY_SYMBOL_DIRS=… vol.py …` | — | Source grep for `os.environ` | **NOT SUPPORTED** — no such environment variable exists; only `XDG_CACHE_HOME`/`APPDATA` are read (log #12, #18) |
| `python3 development/banner_server.py --path DIR --urlprefix URL` | Volatility 3 2.28.0 | Run here | **NOT SUPPORTED (broken)** — `AttributeError: … has no attribute 'LinuxBannerCache'` (log #30) |
| `dwarf2json --version` | dwarf2json 0.9.0 | Run here | **VERIFIED** — prints tool version and `output schema 6.2.0` |
| `dwarf2json linux --elf PATH` | dwarf2json 0.9.0 | Run here | **VERIFIED** — types + symbols + `linux_banner` constant data |
| `dwarf2json linux --elf-types PATH` | dwarf2json 0.9.0 | Run here | **VERIFIED** — types only; 0 symbols |
| `dwarf2json linux --elf-symbols PATH` | dwarf2json 0.9.0 | Run here | **VERIFIED** — symbols + constant data; no types |
| `dwarf2json linux --system-map PATH` | dwarf2json 0.9.0 | Run here | **VERIFIED** — addresses only; no banner; symbols typed `void` |
| `dwarf2json linux --elf A --system-map B` | dwarf2json 0.9.0 | Run here | **VERIFIED** — map addresses override ELF addresses (log #49) |
| `dwarf2json linux --btf PATH` | dwarf2json 0.9.0 / master `9f14607e0d` | `--help`, source grep, `strings` on the binary | **NOT SUPPORTED** — no BTF option exists in any released version |
| `dwarf2json --btf PATH` (as a top-level flag) | dwarf2json 0.9.0 | Same | **NOT SUPPORTED** — and note that all input options are subcommand-scoped (`linux …`/`mac …`) |
| `dwarf2json linux --elf-symbols /sys/kernel/btf/vmlinux` | dwarf2json 0.9.0 | Reasoned from log #92 + #52 | **NOT SUPPORTED** — that file is a raw BTF blob, not an ELF |
| `dwarf2json mac --macho PATH` | dwarf2json 0.9.0 | `--help` | **VERIFIED** (exists; macOS scope, out of scope here) |
| `go build` in the dwarf2json checkout | Go ≥ 1.18 per `go.mod` | Source | **VERIFIED** (declared requirement; Go not installed here, build not run) |
| `btf2json --btf FILE --map FILE --banner "…"` | vobst/btf2json `4a64f9c322` | Source (`src/cli.rs`, `src/symbols.rs`) | **VERIFIED as the correct invocation** (Rust toolchain unavailable here, so not run). Output fails the stock Volatility schema |
| `btf2json --image FILE` | vobst/btf2json `4a64f9c322` | Source | **NOT SUPPORTED** — flag exists, both code paths `bail!()` (log #59) |
| `btf2json --btf FILE --map FILE --banner "…" --vabits N` | eurecom-s3/lemon-btf2json `211005007e` | Source (`src/cli.rs`) | **VERIFIED as the fork's invocation**; `--vabits` is mandatory there. Requires the ARM64 Volatility fork |
| `vol3-profiler DUMP -o OUT -c CACHE -f --no-cache` | Nissen96/vol3-profiler `7364750c39` | Source (`main.py` argparse) | **VERIFIED as documented options** (not run) |
| `sh scripts/extract-vmlinux vmlinuz > vmlinux` | Linux in-tree script (Ubuntu headers 7.0.0-28) | Run here on a Debian bzImage | **VERIFIED** — writes to stdout; input untouched; reported "Extracted vmlinux using 'unxz' from offset 21197" |
| `objcopy --dump-section .BTF=out.btf vmlinux /dev/null` | binutils 2.42 | Run here | **VERIFIED** — extracts 4,351,479 bytes; input hash **and** mtime unchanged |
| `objcopy --dump-section .BTF=out.btf vmlinux` (no output operand) | binutils 2.42 | Run here | **WORKS BUT REWRITES THE INPUT** — mtime changed. Never use on evidence (log #99) |
| `objcopy --add-section .BTF=x.btf vmlinux` | binutils 2.42 | Run here | **MODIFIES THE INPUT FILE'S CONTENTS** — this is how the kernel build embeds module BTF |
| `objcopy --only-section=.BTF in.elf out.elf` | binutils 2.42 | Run here | **VERIFIED** — produces an ELF containing just that section; input untouched (output operand given) |
| `readelf -S vmlinux` | binutils 2.42 | Run here | **VERIFIED** — the primary "what is in this ELF" check |
| `readelf -n vmlinux` | binutils 2.42 | Run here | **VERIFIED** — GNU build ID |
| `readelf -h /sys/kernel/btf/vmlinux` | binutils 2.42 | Run here | **Expected failure** — "Not an ELF file"; useful as a positive test that you have a raw blob |
| `nm vmlinux` | binutils 2.42 | Run here | **VERIFIED** — prints "no symbols" on a stripped image |
| `strings -a -t d IMAGE \| grep -m 20 'Linux version '` | binutils 2.42 | Run here | **VERIFIED** — bounded form; unbounded form produces enormous output |
| `bpftool btf dump file FILE format c` | bpftool v7.7.0 | Run here | **VERIFIED** — C header form |
| `bpftool btf dump file FILE` | bpftool v7.7.0 | Run here | **VERIFIED** — raw form; 236,994 lines for a 4.35 MB blob |
| `bpftool btf dump file MOD.btf -B /sys/kernel/btf/vmlinux` | bpftool v7.7.0 | `bpftool btf help` | **VERIFIED as syntax** (`-B/--base-btf`); split-BTF dump not run |
| `bpftool btf list` | bpftool v7.7.0 | `bpftool btf help` | **VERIFIED as syntax** — requires privileges on a live system |
| `pahole -J --btf_encode_detached=out.btf vmlinux` | dwarves (not installed here) | `scripts/gen-btf.sh`, torvalds/linux master | **VERIFIED from kernel source**; not run locally |
| `pahole -C task_struct vmlinux` | dwarves (not installed here) | Upstream usage | **UNVERIFIED locally** — documented as a cross-check, not measured |
| `debuginfod-find debuginfo BUILDID` | elfutils (client not installed here) | sourceware.org documentation | **VERIFIED from documentation**; kernel coverage untested (log #86) |
| `jq empty FILE` / `jq '.metadata' FILE` | jq 1.7 | Run here | **VERIFIED** |
| `xz -t FILE.xz`, `xz -dc FILE.xz` | xz 5.4.5 | Run here | **VERIFIED** |
| `zstd -dc module.ko.zst > module.ko` | zstd (via system) | Run here | **VERIFIED** on a shipped Ubuntu module |
| `sha256sum FILES > SHA256SUMS` / `sha256sum -c` | coreutils 9.4 | Run here | **VERIFIED** |
| `dpkg-deb -x package.ddeb DIR` | dpkg | Standard usage | **VERIFIED as syntax** (no ddeb downloaded in this environment) |
| `rpm2cpio pkg.rpm \| cpio -idmv` | rpm/cpio | Standard usage | **VERIFIED as syntax**; expands to several GB for kernel-debuginfo |
| `cat /sys/kernel/btf/vmlinux > kernel.btf` | — | Run here (read as non-root; file is mode 0444) | **VERIFIED** |
| `cat /proc/kallsyms > kallsyms.txt` | — | Run here | **VERIFIED**, but all addresses were zero at `kptr_restrict=1` as non-root |
| `zcat /proc/config.gz > config` | — | Not present on this host | **UNVERIFIED here** — depends on `CONFIG_IKCONFIG_PROC`; `/boot/config-$(uname -r)` was used instead |
| `vol.py --info` | Volatility 3 2.28.0 | `vol.py -h` | **NOT SUPPORTED** — that is Volatility 2 syntax; use `vol.py -h` and `vol.py isfinfo` |
| `vol.py --profile=LinuxUbuntu…` | Volatility 3 2.28.0 | `vol.py -h` | **NOT SUPPORTED** — Volatility 2 only; there are no profiles in Volatility 3 |
| `dwarfdump -di module.ko > module.dwarf` | libdwarf (not installed here) | Volatility 2 `tools/linux/Makefile` | **DEPRECATED** — Volatility 2 profile workflow; produces nothing Volatility 3 can read |

---

## Appendix C — Claims requiring caution

```text
Claim
  Volatility 3 does not support Linux on AArch64.
Evidence
  volatility3 2.28.0 constants/architectures.py: LINUX_ARCHS = ["Intel32", "Intel64"];
  full-tree grep for "aarch64" returns nothing in the v2.28.0 tarball; the recursive tree
  listings for tag v2.28.0 and for develop @2026-08-19 contain no arm.py or equivalent (log #34).
Conflicting evidence
  Third-party material exists that runs Volatility against ARM64 Android images
  (eurecom-s3/volatility3-arm64 + lemon-btf2json), which can read as "Volatility supports ARM64".
Best-supported interpretation
  Upstream does not; a third-party fork does. Any ARM64 result must name the fork and its commit.
Affected versions
  2.28.0 and develop as of 2026-08-19. Re-check on any newer release before repeating the claim.
```

```text
Claim
  A btf2json-produced ISF cannot be used with stock Volatility 3.
Evidence
  schema-6.2.0.json/6.3.0.json restrict metadata source kind to ^(dwarf|symtab|system-map)$ (log #6);
  btf2json emits "btf" and "symdb" (log #64); a live validation of such a document raised
  jsonschema.exceptions.ValidationError (log #7); isfinfo --validate produced a traceback (log #25).
Conflicting evidence
  If `jsonschema` is not installed, schemas.valid() returns True for everything (log #26) - so on a
  minimal install such an ISF may load and appear to work.
Best-supported interpretation
  It is rejected wherever validation is actually performed. Do not rely on the absence of a
  dependency as a workaround: that also disables validation for genuinely malformed ISFs.
Affected versions
  Volatility 3 2.28.0 with jsonschema present. Behaviour differs by whether jsonschema is installed.
```

```text
Claim
  dwarf2json requires a System.map.
Evidence
  Live tests: `--elf` alone produced a complete ISF with types, 36 symbols and banner constant data;
  `--system-map` alone produced 4 symbols, no types and no banner (log #46-#48).
Conflicting evidence
  Volatility's own symbol-tables.rst says "The System.map file is recommended for completeness" -
  a recommendation, not a requirement; many third-party tutorials state it as mandatory.
Best-supported interpretation
  Optional and situational. Use it when the ELF symtab is missing or suspect; be aware its addresses
  override the ELF's.
Affected versions
  dwarf2json 0.9.0 (and, by code inspection, 0.8.0 - the option wiring is unchanged in master).
```

```text
Claim
  The banner stored by dwarf2json always ends with newline + NUL.  [RESOLVED 2026-08-24]
Evidence
  dwarf2json 0.9.0 was re-run against the genuine Ubuntu debug package for 5.4.0-1133-kvm (vmlinux
  sha256 4d515cb4..., which is the hash the ISFs themselves record). Output banner: 187 bytes ending
  "\n\x00". The full ISF was byte-identical (sha256 4e723b6d...) to one of the two pre-existing files
  (log #117). The second file differed from a regenerated --elf --system-map run in exactly one place -
  symbols.linux_banner.constant_data - with the trailing newline removed; all 109,467 symbols, 7,515
  user_types, enums, base types and metadata hashes were identical (log #118).
Conflicting evidence
  Volatility's own symbol_cache.py comment says dwarf2json banners end "\x00\n" (log #29); the observed
  order is "\n\x00". The comment is imprecise; the stripping code it guards still produces a banner
  ending "\n", which is what remote indexes contain.
Best-supported interpretation
  dwarf2json 0.9.0 deterministically emits "...\n\x00". An ISF whose banner lacks the newline has been
  modified after generation, and it will silently never match: demonstrated end to end - the correct
  file was "Identified banner ... Using symbol library", the stripped file produced only "No suitable
  linux banner could be matched" (log #119).
Affected versions
  dwarf2json 0.9.0; Volatility 3 2.28.0 (remote-list normalisation is a separate code path).
```

```text
Claim
  Distribution debug packages are available for the kernel you need.
Evidence
  Verified availability for Ubuntu (ddebs pool), Debian (main pool), Fedora (release debug tree),
  Rocky (debug tree), AlmaLinux (vault), openSUSE (debug repos) (log #73-#84).
Conflicting evidence
  On 2026-08-24 the amd64 dbgsym for Ubuntu 6.8.0-101-generic was already gone (HTTP 404) while
  arm64 and s390x remained (log #74). Arch publishes no kernel debug package at all (log #83).
  debuginfo.centos.org was unreachable from this environment (log #82).
Best-supported interpretation
  Availability is per-distribution, per-architecture, per-kernel and time-limited. Check first;
  collect at acquisition time so you never depend on it.
Affected versions
  Checked 2026-08-24. Archive contents change continuously.
```

```text
Claim
  The Volatility cache directory cannot be changed.
Evidence
  doc/source/symbol-tables.rst in 2.28.0 states exactly that.
Conflicting evidence
  cli/__init__.py defines --cache-path and assigns constants.CACHE_PATH from it; running with
  --cache-path visibly used the new directory (log #20).
Best-supported interpretation
  The documentation is stale; the option works, but the target directory must already exist.
Affected versions
  2.28.0.
```

```text
Claim
  btf2json's ISFs describe your kernel's symbol types correctly.
Evidence
  It attaches types by matching symbol names against symdb/dummy.symdb, a 239,380-line file embedded
  at compile time; add_types_from_btf() is an explicit stub (log #62, #63).
Conflicting evidence
  For kernels close to those the symdb was generated from, coverage may be excellent, and the
  resulting ISFs have been reported to work by the tool's author.
Best-supported interpretation
  Symbol typing quality is a function of the embedded database's age, not of your kernel's BTF.
  Coverage gaps are silent. Corroborate before relying on any symbol-typed result.
Affected versions
  vobst/btf2json master 4a64f9c322 and the lemon fork, which ships an identical symdb.
```

```text
Claim
  ELF/kdump images give Volatility the KASLR offset via VMCOREINFO.
Evidence
  LinuxIntelVMCOREINFOStacker reads KERNELOFFSET and NUMBER(phys_base) and derives both shifts
  and the DTB (log #33).
Conflicting evidence
  It bails when NUMBER(phys_base) is absent (kernels < 4.10) or KERNELOFFSET is absent
  (kernels < 3.14), and it still requires a banner-matching ISF to proceed.
Best-supported interpretation
  A powerful shortcut on modern x86 kdump-style captures; not a substitute for the ISF, and not
  available on old kernels or raw captures.
Affected versions
  Volatility 3 2.28.0; kernel-version dependent as above.
```

```text
Claim
  makedumpfile-compressed crash dumps can be analysed.
Evidence
  Nothing in the 2.28.0 tree references KDUMP/makedumpfile/diskdump; only uncompressed ELF cores
  are handled by elf.py (log #31, #32).
Conflicting evidence
  None found.
Best-supported interpretation
  Not supported. Convert or re-acquire outside Volatility before analysis.
Affected versions
  2.28.0.
```

```text
Claim
  Android GKI builds publish usable kernel symbols.
Evidence
  The per-branch release-build pages enumerate artifacts including Image, boot.img, System.map,
  vmlinux and vmlinux.symvers, plus debug-kernel variants, obtained from ci.android.com (log #123).
Conflicting evidence
  The overview page enumerates nothing (log #106); the pages describe GKI/ACK builds rather than the
  vendor kernel actually shipped on a device; and whether a published vmlinux carries DWARF (as
  opposed to symbols only) was NOT verified in this research.
Best-supported interpretation
  For a device running a stock GKI/ACK build, artifacts including vmlinux and System.map are
  published and worth chasing. For a vendor kernel, assume nothing. Verify DWARF presence with
  readelf -S before planning a dwarf2json workflow - and remember upstream Volatility cannot analyse
  AArch64 Linux at all (log #34).
Affected versions
  Checked 2026-08-24; AOSP documentation changes frequently.
```

---

## Appendix D — Terminology

| Term | Definition | Common confusion |
|---|---|---|
| **BPF / eBPF** | The in-kernel virtual machine and its extended form; programs are verified then JIT-compiled | "BPF" alone historically meant packet filtering; today it almost always means eBPF |
| **BTF** | *BPF Type Format*. A compact, deduplicated type encoding generated from DWARF by `pahole` and shipped in the kernel image; exposed at runtime as `/sys/kernel/btf/vmlinux` | Written "BTF", never "btf2json's format". It is **not** compressed DWARF and holds no addresses or data values |
| **`.BTF` / `.BTF_ids` / `.BTF.base`** | The ELF sections carrying BTF (base BTF, BPF id patch data, and the base reference for split module BTF) | An ELF *section* named `.BTF` is not the same thing as the *raw blob* at `/sys/kernel/btf/vmlinux`, though the bytes are the same format |
| **build ID** | A hash (usually SHA-1) placed in an ELF note at link time, identifying that exact binary | Strong corroboration of artifact identity; **not** Volatility's ISF matching key |
| **CO-RE** | *Compile Once – Run Everywhere*: BPF programs relocated against the running kernel's BTF at load time | The reason BTF exists; unrelated to memory forensics except that it made BTF ubiquitous |
| **DWARF** | The standard debugging-information format emitted by compilers into `.debug_*` sections | Spelled DWARF (capitals). "Debug symbols" colloquially means DWARF **plus** a symbol table |
| **debuginfo / debug symbols** | A distribution's packaging of DWARF (and often an unstripped `vmlinux` and debug modules) | `-dbg` (Debian) vs `-dbgsym` (Ubuntu ddebs) vs `-debuginfo` (RPM) — three names, same idea |
| **`debuginfod`** | An HTTP service that serves debug artifacts keyed by build ID; client `debuginfod-find`, configured with `DEBUGINFOD_URLS` | Kernel coverage is not guaranteed |
| **ELF** | Executable and Linkable Format — the container for `vmlinux`, `.ko` modules and userland binaries | A raw BTF blob is *not* an ELF |
| **ISF** | *Intermediate Symbol File* — the JSON symbol table Volatility 3 consumes: `metadata`, `base_types`, `user_types`, `enums`, `symbols` | Sometimes loosely called a "profile" or "symbol pack". Volatility 3 has no profiles |
| **kallsyms** | The kernel's own compressed symbol table, built into the image and exposed at `/proc/kallsyms` | Runtime addresses, censored by `kptr_restrict`; not a `System.map` |
| **KASLR** | *Kernel Address Space Layout Randomisation* (`CONFIG_RANDOMIZE_BASE`) — the kernel is relocated by a random slide at boot | Volatility resolves it from the image (VMCOREINFO or `init_task` scanning), not from the ISF |
| **`kptr_restrict`** | `sysctl kernel.kptr_restrict`: 0 = show pointers, 1 = hide from unprivileged, 2 = hide from everyone | Setting it to 0 modifies running state — record it |
| **`linux_banner`** | The kernel symbol whose `.rodata` bytes are the "Linux version …" string, terminated by `\n\0` | The **symbol** existing in `System.map` says nothing about the **text**; Volatility matches on the bytes |
| **`pahole`** | The tool from the *dwarves* package that converts DWARF to BTF (`-J`) and prints structure layouts (`-C`) | The package is `dwarves`; the binary is `pahole` |
| **`symdb`** *(btf2json term)* | The 239,380-line symbol-name → ISF-type database compiled into `btf2json`, used because BTF does not say which type a given global has | Specific to that third-party tool; not an ISF or Volatility concept |
| **`__lemon_va_bits`** *(lemon-btf2json term)* | A synthetic symbol the EURECOM fork injects to carry the VA-bits value for its ARM64 Volatility fork | Meaningless to upstream Volatility |
| **structure offset / member offset** | The byte offset of a member within a structure, as recorded in `user_types.<T>.fields.<m>.offset` | The single most build-sensitive value in the whole ISF |
| **symbol address** | The address associated with a symbol name. Link-time in ELF/`System.map`/ISF; runtime in `/proc/kallsyms` | Mixing the two silently breaks KASLR arithmetic |
| **`System.map`** | Text file of `address type name` produced at kernel build time | Not a type database |
| **type information** | Structure/union/enum definitions with sizes and member offsets — what lets a forensic tool interpret raw bytes | Distinct from symbols; DWARF and BTF carry it, `System.map` and kallsyms do not |
| **`vmlinux`** | The linked ELF kernel image | Ends in **x**. May or may not carry DWARF/BTF/symtab |
| **`vmlinuz`** | The compressed, self-extracting boot image (`bzImage` on x86) | Ends in **z**. Not an ELF; extracting it does not produce a debug kernel |
| **Volatility 2 profile** *(historical)* | ZIP of `module.dwarf` + `System.map`, selected by `--profile=` | Unrelated to ISFs; the Volatility 2 repository is archived |
