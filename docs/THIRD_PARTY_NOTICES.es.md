# Avisos de terceros

> Traducción no oficial. La versión inglesa de esta página es la referencia autoritativa.

Este inventario separa el código fuente propiedad de AstinusLab del material de terceros que AstinusLab realmente distribuye, y del software externo y material de runtime usado por flujos forenses opcionales. La inclusión en este inventario de avisos no significa que un componente de terceros listado esté incluido en el archivo fuente, el instalador o el paquete Python de AstinusLab: lee la columna `Estado de distribución`, que es autoritativa por fila. `Distribuido` no implica propiedad de AstinusLab.

## Inventario de distribución

| Componente | Estado de distribución | Licencia o términos | Aviso |
|---|---|---|---|
| Código fuente propiedad de AstinusLab | Distribuido | Volatility Software License (VSL) 1.0 | Copyright 2026 Francisco Fernandez. El `LICENSE` raíz se aplica únicamente al material propiedad de AstinusLab. No relicencia material de terceros, y AstinusLab no forma parte de Volatility. |
| htmx 2.0.4, incorporado en `src/astinuslab/web/static/htmx.min.js` | Distribuido | Zero-Clause BSD (0BSD) | Uno de los componentes de terceros distribuidos, junto con Inter y JetBrains Mono. Viaja tanto en el archivo fuente como en el paquete instalado porque los datos de paquete de setuptools incluyen explícitamente los recursos web propios del paquete. El archivo es idéntico byte a byte al artefacto upstream [`v2.0.4` `dist/htmx.min.js`](https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js). 0BSD no impone ninguna condición de retención de avisos ni de atribución; el texto upstream exacto se conserva en `LICENSES/htmx-0BSD.txt` para completitud del inventario. AstinusLab no relicencia htmx. |
| Inter 4.1 (`src/astinuslab/web/static/fonts/InterVariable.woff2`) | Distribuido | SIL Open Font License 1.1 | Aviso y licencia conservados en `LICENSES/inter-OFL-1.1.txt`. |
| JetBrains Mono 2.304 (`src/astinuslab/web/static/fonts/JetBrainsMono-*.woff2`, 400/600/700/800) | Distribuido | SIL Open Font License 1.1 | Aviso y licencia conservados en `LICENSES/jetbrains-mono-OFL-1.1.txt`. |
| Volatility 3 Framework | No distribuido; suministrado por el operador | Volatility Software License (VSL) 1.0 | Copyright 2019 Volatility Foundation. Revisa la [página oficial de VSL 1.0](https://www.volatilityfoundation.org/license/vsl-v1.0) y el [código fuente fijado de Volatility 3](https://github.com/volatilityfoundation/volatility3/tree/v2.28.0). AstinusLab no relicencia Volatility 3. |
| `dwarf2json` | No distribuido; suministrado por el operador | VSL 1.0 | Copyright 2019 Volatility Foundation. Revisa la [página oficial de VSL 1.0](https://www.volatilityfoundation.org/license/vsl-v1.0) y el [código fuente fijado auditado](https://github.com/volatilityfoundation/dwarf2json/tree/9f14607e0d339d463ea725fbd5c08aa7b7d40f75). El operador verifica y registra el artefacto elegido. |
| `pflag` en la build oficial auditada de `dwarf2json` 0.9.0 | No distribuido por AstinusLab | BSD-3-Clause | Solo procedencia informativa. El aviso upstream exacto se conserva en `LICENSES/pflag-BSD-3-Clause.txt`. |
| Runtime de Go en la build oficial auditada de `dwarf2json` 0.9.0 | No distribuido por AstinusLab | BSD-3-Clause | Solo procedencia informativa. El aviso upstream exacto se conserva en `LICENSES/go-BSD-3-Clause.txt`. |
| Paquetes de símbolos de Windows y contenido de catálogo | Material externo de runtime, no payload fuente | Términos del proveedor upstream | El material sigue regido por los términos del proveedor aplicables. Los operadores deben revisar esos términos antes de usarlo; AstinusLab registra identificadores de origen, URLs, hashes y demás procedencia disponible del material de símbolos adquirido. Este inventario no asigna responsabilidad legal. |

Las URLs de origen de licencia, revisiones, fechas de obtención y hashes de payload exactos de las copias de texto conservadas están registrados en `LICENSES/README.md`.

## Procedencia del asset frontend incorporado y distribuido

`src/astinuslab/web/static/htmx.min.js`: Uno de los componentes de terceros distribuidos, junto con Inter y JetBrains Mono. Está versionado en el repositorio, por lo que viaja con el archivo fuente, y los datos de paquete declarados de setuptools lo incluyen en los wheels y las versiones instaladas:

- proyecto upstream: <https://github.com/bigskysoftware/htmx>;
- tag upstream: `v2.0.4`;
- cadena de versión embebida en el archivo distribuido: `version:"2.0.4"`;
- artefacto upstream:
  <https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js>;
- tamaño del archivo distribuido: 50917 bytes;
- SHA-256 del archivo distribuido:
  `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.

El artefacto upstream se descargó de forma independiente el 2026-08-14 y su SHA-256 coincidió byte a byte con el archivo distribuido. htmx `v2.0.4` está licenciado bajo Zero-Clause BSD, que concede uso, copia, modificación y distribución sin ninguna condición de aviso, atribución o copyleft. Conservar `LICENSES/htmx-0BSD.txt` completa por tanto el inventario; no cumple una obligación.

## Procedencia del artefacto `dwarf2json` auditado

Los siguientes hechos documentan el artefacto Linux oficial upstream auditado para esta revisión de límite de release. El binario en sí no se distribuye:

- versión upstream: `0.9.0`;
- commit upstream: `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`;
- fecha del commit: `2024-10-08T23:53:08Z`;
- código fuente upstream: <https://github.com/volatilityfoundation/dwarf2json>;
- asset oficial de release Linux v0.9.0:
  <https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64>;
- metadatos de build: `go1.18.10`, `linux/amd64`, `vcs.modified=false`;
- dependencia directa: `github.com/spf13/pflag v1.0.5`;
- SHA-256 del binario oficial auditado:
  `e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`.

El asset oficial de release se descargó de forma independiente el 2026-08-04 y su SHA-256 coincidió con el valor auditado del binario anterior.

Esta procedencia es documentación, no un sidecar empaquetado. No es una lista de permitidos automática. Un operador puede configurar un artefacto `dwarf2json` distinto solo tras revisar su licencia y procedencia y registrar su SHA-256 exacto en la configuración de AstinusLab y en la evidencia de release.
