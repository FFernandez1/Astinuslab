# Avisos de tercers

> Traducció no oficial. La versió anglesa d'aquesta pàgina és la referència autoritativa.

Aquest inventari separa el codi font propietat d'AstinusLab del material de tercers que AstinusLab distribueix realment, i del programari extern i material de runtime fet servir per fluxos forenses opcionals. La inclusió en aquest inventari d'avisos no significa que un component de tercers llistat estigui inclòs a l'arxiu font, l'instal·lador o el paquet Python d'AstinusLab: llegeix la columna `Estat de distribució`, que és autoritativa per fila. `Distribuït` no implica propietat d'AstinusLab.

## Inventari de distribució

| Component | Estat de distribució | Llicència o termes | Avís |
|---|---|---|---|
| Codi font propietat d'AstinusLab | Distribuït | Volatility Software License (VSL) 1.0 | Copyright 2026 Francisco Fernandez. El `LICENSE` arrel s'aplica únicament al material propietat d'AstinusLab. No rellicencia material de tercers, i AstinusLab no forma part de Volatility. |
| htmx 2.0.4, incorporat a `src/astinuslab/web/static/htmx.min.js` | Distribuït | Zero-Clause BSD (0BSD) | Un dels components de tercers distribuïts, juntament amb Inter i JetBrains Mono. Viatja tant a l'arxiu font com al paquet instal·lat perquè les dades de paquet de setuptools inclouen explícitament els recursos web propis del paquet. El fitxer és idèntic byte a byte a l'artefacte upstream [`v2.0.4` `dist/htmx.min.js`](https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js). 0BSD no imposa cap condició de retenció d'avisos ni d'atribució; el text upstream exacte es conserva a `LICENSES/htmx-0BSD.txt` per completitud de l'inventari. AstinusLab no rellicencia htmx. |
| Inter 4.1 (`src/astinuslab/web/static/fonts/InterVariable.woff2`) | Distribuït | SIL Open Font License 1.1 | Avís i llicència conservats a `LICENSES/inter-OFL-1.1.txt`. |
| JetBrains Mono 2.304 (`src/astinuslab/web/static/fonts/JetBrainsMono-*.woff2`, 400/600/700/800) | Distribuït | SIL Open Font License 1.1 | Avís i llicència conservats a `LICENSES/jetbrains-mono-OFL-1.1.txt`. |
| Volatility 3 Framework | No distribuït; subministrat per l'operador | Volatility Software License (VSL) 1.0 | Copyright 2019 Volatility Foundation. Revisa la [pàgina oficial de VSL 1.0](https://www.volatilityfoundation.org/license/vsl-v1.0) i el [codi font fixat de Volatility 3](https://github.com/volatilityfoundation/volatility3/tree/v2.28.0). AstinusLab no rellicencia Volatility 3. |
| `dwarf2json` | No distribuït; subministrat per l'operador | VSL 1.0 | Copyright 2019 Volatility Foundation. Revisa la [pàgina oficial de VSL 1.0](https://www.volatilityfoundation.org/license/vsl-v1.0) i el [codi font fixat auditat](https://github.com/volatilityfoundation/dwarf2json/tree/9f14607e0d339d463ea725fbd5c08aa7b7d40f75). L'operador verifica i registra l'artefacte escollit. |
| `pflag` a la build oficial auditada de `dwarf2json` 0.9.0 | No distribuït per AstinusLab | BSD-3-Clause | Només procedència informativa. L'avís upstream exacte es conserva a `LICENSES/pflag-BSD-3-Clause.txt`. |
| Runtime de Go a la build oficial auditada de `dwarf2json` 0.9.0 | No distribuït per AstinusLab | BSD-3-Clause | Només procedència informativa. L'avís upstream exacte es conserva a `LICENSES/go-BSD-3-Clause.txt`. |
| Paquets de símbols de Windows i contingut de catàleg | Material extern de runtime, no payload font | Termes del proveïdor upstream | El material continua regit pels termes del proveïdor aplicables. Els operadors han de revisar aquests termes abans de fer-lo servir; AstinusLab registra identificadors d'origen, URLs, hashos i altra procedència disponible del material de símbols adquirit. Aquest inventari no assigna responsabilitat legal. |

Les URLs d'origen de llicència, revisions, dates d'obtenció i hashos de payload exactes de les còpies de text conservades estan registrats a `LICENSES/README.md`.

## Procedència de l'asset frontend incorporat i distribuït

`src/astinuslab/web/static/htmx.min.js`: Un dels components de tercers distribuïts, juntament amb Inter i JetBrains Mono. Està versionat al repositori, així que viatja amb l'arxiu font, i les dades de paquet declarades de setuptools el porten als wheels i a les releases instal·lades:

- projecte upstream: <https://github.com/bigskysoftware/htmx>;
- tag upstream: `v2.0.4`;
- cadena de versió incrustada al fitxer distribuït: `version:"2.0.4"`;
- artefacte upstream:
  <https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js>;
- mida del fitxer distribuït: 50917 bytes;
- SHA-256 del fitxer distribuït:
  `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.

L'artefacte upstream es va descarregar de manera independent el 2026-08-14 i el seu SHA-256 va coincidir byte a byte amb el fitxer distribuït. htmx `v2.0.4` té llicència Zero-Clause BSD, que concedeix ús, còpia, modificació i distribució sense cap condició d'avís, atribució o copyleft. Conservar `LICENSES/htmx-0BSD.txt` completa per tant l'inventari; no compleix cap obligació.

## Procedència de l'artefacte `dwarf2json` auditat

Els fets següents documenten l'artefacte Linux oficial upstream auditat per a aquesta revisió de límit de release. El binari en si no es distribueix:

- versió upstream: `0.9.0`;
- commit upstream: `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`;
- data del commit: `2024-10-08T23:53:08Z`;
- codi font upstream: <https://github.com/volatilityfoundation/dwarf2json>;
- asset oficial de release Linux v0.9.0:
  <https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64>;
- metadades de build: `go1.18.10`, `linux/amd64`, `vcs.modified=false`;
- dependència directa: `github.com/spf13/pflag v1.0.5`;
- SHA-256 del binari oficial auditat:
  `e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`.

L'asset oficial de release es va descarregar de manera independent el 2026-08-04 i el seu SHA-256 va coincidir amb el valor auditat del binari anterior.

Aquesta procedència és documentació, no un sidecar empaquetat. No és una llista de permesos automàtica. Un operador pot configurar un artefacte `dwarf2json` diferent només després de revisar-ne la llicència i la procedència i registrar-ne el SHA-256 exacte a la configuració d'AstinusLab i a l'evidència de release.
