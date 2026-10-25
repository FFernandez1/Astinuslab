# Retained Third-Party License Texts

The root `LICENSE` covers only AstinusLab-owned material. It does not relicense
Volatility 3, `dwarf2json`, their dependencies, htmx, or runtime symbol
material. Volatility 3 and `dwarf2json` are operator-supplied and not shipped by
AstinusLab. The BSD notices below are retained as informational provenance for
the audited official `dwarf2json` 0.9.0 binary; AstinusLab does not ship that
binary, `pflag`, or the Go runtime. AstinusLab ships three third-party browser
payloads: the vendored htmx asset and the two self-hosted brand type families,
Inter and JetBrains Mono. Both families are under the SIL Open Font License 1.1,
which requires the copyright notice and licence to travel with the font files;
`inter-OFL-1.1.txt` and `jetbrains-mono-OFL-1.1.txt` discharge that condition.

The license text files in this directory were obtained from pinned authoritative upstream
sources. Retrieved: 2026-08-04, except the htmx text, retrieved 2026-08-14, and
the two font licences, retrieved 2026-09-23. Each row records its own retrieval
date. The dependency inventory records the pinned artifact URLs and hashes
collected for this first-release review; it is not part of the text-retrieval
date statement above.

`dependency-inventory.json` is an engineering inventory generated from the
checked-in locks and hash-verified upstream artifacts, not an upstream license
text. It records 91 Python and 75 npm entries, artifact and license-member hashes,
distribution scope and unresolved legal review. Raw extracted texts and the
collection receipt remain in the private release evidence. The inventory's
lockfile digests must match the candidate; it does not attest arbitrary installed
environments, every platform wheel, unpinned build tools or runtime downloads.

| Local file | Component / upstream revision | Retrieved | Authoritative source | SHA-256 |
|---|---|---|---|---|
| `VSL-1.0.txt` | `dwarf2json` commit `9f14607e0d339d463ea725fbd5c08aa7b7d40f75` | 2026-08-04 | <https://raw.githubusercontent.com/volatilityfoundation/dwarf2json/9f14607e0d339d463ea725fbd5c08aa7b7d40f75/LICENSE.txt> | `94e8e631513d38746775e31e7ad84065d009a57cd80d5d4c4e7c7182373e7cbe` |
| `pflag-BSD-3-Clause.txt` | `github.com/spf13/pflag` tag `v1.0.5` | 2026-08-04 | <https://raw.githubusercontent.com/spf13/pflag/v1.0.5/LICENSE> | `b8514c577c1c4b46cee454d5a882b15fa411e72c5bd7f801f241591789fce61a` |
| `go-BSD-3-Clause.txt` | Go tag `go1.18.10` | 2026-08-04 | <https://raw.githubusercontent.com/golang/go/go1.18.10/LICENSE> | `2d36597f7117c38b006835ae7f537487207d8ec407aa9d9980794b2030cbc067` |
| `htmx-0BSD.txt` | `github.com/bigskysoftware/htmx` tag `v2.0.4` | 2026-08-14 | <https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/LICENSE> | `d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38` |
| `inter-OFL-1.1.txt` | `github.com/rsms/inter` tag `v4.1` | 2026-09-23 | <https://raw.githubusercontent.com/rsms/inter/v4.1/LICENSE.txt> | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |
| `jetbrains-mono-OFL-1.1.txt` | `github.com/JetBrains/JetBrainsMono` tag `v2.304` | 2026-09-23 | <https://raw.githubusercontent.com/JetBrains/JetBrainsMono/v2.304/OFL.txt> | `30f0c136e3c88e422d0791acd97238870f9054a9729bc34cf2ff0d4ed8cac4ad` |

`src/astinuslab/web/static/htmx.min.js` is the vendored htmx browser asset. It is
byte-identical to the upstream `v2.0.4` release artifact at
<https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js>;
an independent 2026-08-14 download produced the same SHA-256
`e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`. Zero-Clause
BSD imposes no notice-retention or attribution requirement, so `htmx-0BSD.txt`
is retained for inventory completeness and provenance, not to satisfy a licence
condition.

`src/astinuslab/web/static/fonts/` holds the self-hosted brand faces. They are
extracted unmodified from the pinned upstream release archives, so the SIL Open
Font License 1.1 Reserved Font Name condition is not engaged: AstinusLab neither
modifies nor renames them.

| Shipped file | Upstream archive | Archive SHA-256 | Member path | File SHA-256 | Bytes |
|---|---|---|---|---|---|
| `InterVariable.woff2` | <https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip> | `9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e` | `web/InterVariable.woff2` | `693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3` | 352240 |
| `JetBrainsMono-Regular.woff2` | <https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip> | `6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf` | `fonts/webfonts/JetBrainsMono-Regular.woff2` | `a9cb1cd82332b23a47e3a1239d25d13c86d16c4220695e34b243effa999f45f2` | 92164 |
| `JetBrainsMono-SemiBold.woff2` | <https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip> | `6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf` | `fonts/webfonts/JetBrainsMono-SemiBold.woff2` | `918edad542a1da608fd2ba8daebaff9ac802309103fe760eed465b8b4e47faf1` | 94472 |
| `JetBrainsMono-Bold.woff2` | <https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip> | `6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf` | `fonts/webfonts/JetBrainsMono-Bold.woff2` | `c503cc5ec5f8b2c7666b7ecda1adf44bd45f2e6579b2eba0fc292150416588a2` | 94588 |
| `JetBrainsMono-ExtraBold.woff2` | <https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip> | `6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf` | `fonts/webfonts/JetBrainsMono-ExtraBold.woff2` | `88097a36a292c145a14394f1fd5133f76b9a3d6fdb133834b2c94a3db61dd39a` | 93692 |

Both retained licence texts are byte-identical to the upstream copies at the raw
URLs recorded for `inter-OFL-1.1.txt` and `jetbrains-mono-OFL-1.1.txt` in the
licence table above. They were downloaded independently on 2026-09-23, not taken
only from the release archives.

The Volatility 3 `v2.28.0` license source is
<https://raw.githubusercontent.com/volatilityfoundation/volatility3/v2.28.0/LICENSE.txt>
and its exact payload SHA-256 is
`6e905a859f6d5ebf23d4b7cdfb8ac425c4ec5dd02b3b274901a967f3e452eb37`.
That payload and the pinned `dwarf2json` VSL copy contain the same VSL 1.0 terms
except that the older `dwarf2json` copy duplicates the words “under the” once
in the Contributing paragraph. `VSL-1.0.txt` intentionally preserves the exact
bytes received with the audited `dwarf2json` source rather than silently
normalizing them. The canonical license information is also available from the
[Volatility Foundation VSL 1.0 page](https://www.volatilityfoundation.org/license/vsl-v1.0).

The audited `dwarf2json` binary corresponds byte-for-byte to the official
v0.9.0 Linux release asset at
<https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64>.
An independent 2026-08-04 download produced SHA-256
`e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`.

AstinusLab itself is licensed under VSL 1.0 (see the root `LICENSE`), so its
Volatility integration, which the VSL counts as an Addition, is published under
the same license as Volatility 3. These files and `THIRD_PARTY_NOTICES.md` are
notices, not a legal determination.
