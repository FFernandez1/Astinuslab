# Volatility 3

Si un análisis aceptado muestra que su estado no está disponible, el resultado sigue siendo desconocido. Restablece la conexión o inicia sesión de nuevo y elige **Comprobar estado** para consultar esa misma ejecución; esto no envía otro análisis. Al recargar la misma pestaña se restaura la observación de la ejecución aceptada y de su entrada exacta. La ejecución permanece desactivada mientras el resultado no se resuelva. Elegir otra entrada o plugin cambia el contexto; el texto de las reglas YARA no se guarda para la recarga.

AstinusLab usa Volatility 3 para el análisis de memoria de Linux. En V1, Volatility 3 es central en el flujo Captura → Análisis → Evidencia para labs de Linux. El límite importante es:

- Los ISFs son artefactos de caché global reutilizables en `vol3/symbols/linux/`.
- La salida de plugins es local al job en `storage/dumps/<job_id>/vol3/`.

## Analizar una captura exacta de Playground

1. Antes de cerrar la VM, registra el kernel Linux en el momento de la captura (`uname -r`), la arquitectura (`uname -m`) y cómo los verificaste, vinculándolos a la VM, sesión y adquisición. Conserva la identidad de compilación y los símbolos. Otro arranque de la misma imagen no demuestra el kernel capturado.
2. Completa una captura de **memoria** en Playground. En **Volatility**, selecciona esa fuente exacta mediante su identidad de artefacto/adquisición; un nombre parecido no basta. Un disco no es una captura de memoria.
3. Introduce el kernel capturado, arquitectura `x86_64` y una nota de procedencia que explique la verificación. Marca que has verificado el contexto contra esa captura y guarda el contexto. No confirmes una versión deducida únicamente del catálogo.
4. Se crea un Job con `execution_mode=artifact_analysis`, vinculado al artefacto original, checksum, sesión y adquisición. Registra análisis derivado sin arrancar ni recapturar una VM. Un contexto de kernel contradictorio se rechaza.
5. Adjunta o sube un ISF compatible. Sus metadatos de kernel Linux y arquitectura deben coincidir con el contexto; cambiar la etiqueta de un archivo incompatible no sirve. Prepara símbolos antes de capturar cuando sea posible; un job solo-ISF de otro arranque solo sirve si coincide la identidad exacta de kernel/compilación.
6. Ejecuta un plugin Linux como `linux.pslist.PsList` y espera el resultado final. Descarga el informe, manifiesto y checksum desde el Job de análisis o los endpoints Artifact. Conserva por separado la memoria original: [exportación de evidencias](../reference/FORENSIC_INTEGRITY.md).

La API usa `POST /api/v1/volatility/analysis-contexts` con `artifact_id`, `kernel_release`, `architecture: "x86_64"`, `verified: true` y una nota `provenance` no vacía. Usa la preparación de autenticación/CSRF de [Ejemplos](../usage/examples.md). Se rechazan discos, bytes ausentes, propiedad incoherente o contexto sin verificar.


## Herramientas externas: instalación y recuperación

Volatility 3 Framework 2.28.0 y `dwarf2json` son herramientas externas
instaladas por el operador. Lee el VSL upstream y el inventario de
`THIRD_PARTY_NOTICES.md`; AstinusLab nunca descarga herramientas ni acepta sus
licencias en nombre del operador.

```bash
cd /opt/astinuslab/current
./astinuslab.sh guide-volatility
```

Este comando es de solo lectura: muestra estado y comandos recomendados, pero no descarga, instala, configura ni reinicia herramientas.

### Instalar y verificar

Elige una ruta versionada fuera del checkout de AstinusLab. Este es un ejemplo
ejecutado por el operador, no por el instalador:

```bash
sudo install -d -m 0755 /opt/volatility3-2.28.0
sudo chown "$(id -un):$(id -gn)" /opt/volatility3-2.28.0
python3.12 -m venv /opt/volatility3-2.28.0
/opt/volatility3-2.28.0/bin/python -m pip install 'volatility3==2.28.0'
/opt/volatility3-2.28.0/bin/vol -q frameworkinfo.FrameworkInfo
sudo install -d -m 0755 /opt/dwarf2json/0.9.0
sudo install -m 0755 /path/to/downloaded/dwarf2json-linux-amd64 /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
echo 'e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b  /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64' | sha256sum --check -
sha256sum /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64 --help
```

El ejemplo muestra `Volatility 3 Framework 2.28.0`. La versión mínima admitida
y la versión probada son **2.28.0**. Las versiones posteriores se aceptan con
`untested=true` (`READY (UNTESTED)` en la guía); verifica tus casos antes de usarlas.
Registra origen,
tag o commit, arquitectura y SHA-256 local de `dwarf2json`. Artefacto auditado:
`https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64`;
commit `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`.

### Configurar, recuperar y actualizar

Edita la sección `[vol3]` existente en `astinuslab.toml`: sustituye o descomenta
las claves de la guía y conserva las demás. Crea la sección solo si falta; no
anexes una segunda tabla `[vol3]`. Valida la sintaxis antes de reiniciar, usando
la ruta real de tus datos. La guía sigue mostrando instrucciones si el TOML está
mal formado.

```bash
/opt/astinuslab/current/.venv/bin/python -c 'import pathlib,tomllib; tomllib.loads(pathlib.Path("/opt/astinuslab/astinuslab.toml").read_text()); print("TOML valid")'
```

Configura las rutas explícitas y `dwarf2json_sha256`, reinicia los servicios y
comprueba `/api/v1/volatility/readiness` o la página Volatility. Los códigos
`*_not_configured`, `*_not_found`, `*_not_executable`,
`volatility_version_unsupported`, `volatility_probe_failed`,
`dwarf2json_hash_not_configured` y `dwarf2json_hash_mismatch` indican la acción
correctiva. Un `guest transfer mismatch` exige verificar el mismo hash en host y
guest; Upload ISF es una alternativa manual cuando no se puede construir.
La preparación de análisis y la preparación para construir ISF son independientes: una puede estar `READY` mientras la otra no está disponible.

Para actualizar, detén el encolado, instala en una ruta versionada nueva, revisa
licencia/procedencia/hash, ejecuta la prueba compatible y los tests de regresión,
actualiza la configuración atómicamente, reinicia y verifica readiness. Conserva
la ruta anterior hasta validar rollback y nunca sobrescribas un binario.
Para revertir, restaura la ruta y el par de hash anteriores, reinicia y verifica.

## Volatility 101 para AstinusLab

Si vienes de instalaciones Volatility estándar, los jobs estrictos solo-ISF pueden parecer distintos. En AstinusLab, el análisis Linux usa un Intermediate Symbol Format (ISF) ajustado al kernel exacto de la VM, no perfiles adivinados. Flujo típico: genera/cachea el ISF, ejecuta el lab, captura memoria y analiza con Volatility 3 usando ese ISF.

## Símbolos Windows (PDB)

Windows no construye un ISF por kernel desde DWARF como Linux. Volatility 3 resuelve símbolos desde PDB: por defecto desde Microsoft Symbol Server, o desde una caché offline bajo `vol3/symbols/windows/` para labs aislados. Los pasos Linux de build ISF/live-guest no aplican a targets Windows. Este soporte de Windows es experimental: preparar targets Windows no es un flujo automatizado de 1.0.0, no tiene evidencia de aceptación de release y queda fuera del alcance soportado.

## Generación de ISF

AstinusLab construye un ISF para el kernel exacto en ejecución de la VM recién arrancada. Esto es arquitectura intencionada: la VM es la fuente de verdad para la release del kernel, los metadatos de distro y la disponibilidad de paquetes.

El runner de producción puede construir un ISF mediante:

- Reutilizar un ISF válido existente de la caché local cuando la release del kernel y el build ID conocido coinciden.
- Obtener paquetes de depuración de la distro desde los repositorios de paquetes del lado del host. Para algunas distros, el script de host fetch puede usar debuginfod internamente cuando se conoce el build ID del guest.
- Conectarse al guest en vivo y usar un `vmlinux` coincidente con DWARF, debuginfod o una instalación de paquetes de depuración permitida explícitamente cuando el host fetch no puede producir un ISF válido. `dwarf2json` no admite una entrada que solo contenga BTF y este fallback nunca la utiliza.

Cada job registra si produjo un ISF nuevo o reutilizó uno en caché.

## Jobs estrictos solo-ISF

Un job explícito solo-ISF debe producir o reutilizar un ISF válido. Si no hay un ISF válido disponible, el job falla. Esto evita que el análisis posterior use símbolos faltantes, obsoletos o no coincidentes.

Los jobs solo-ISF usan la cadena de proveedores del host: `local_cache -> prebuilt_isf -> debuginfod -> host_fetch`. Si esa cadena falla, AstinusLab prueba el fallback del guest en vivo, que puede usar un `vmlinux` coincidente con DWARF, debuginfod o una instalación de paquetes de depuración permitida explícitamente. Una entrada que solo contenga BTF no se envía a `dwarf2json`. Los logs de host fetch se conservan en `storage/dumps/<job_id>/vol3/symbol-build-host.log`; los logs del fallback en vivo se conservan en `storage/dumps/<job_id>/vol3/symbol-build-live.log`; los intentos de proveedores y las razones de skip/fallo se conservan en `storage/dumps/<job_id>/vol3/symbol-diagnostics.json`.

La identidad de la caché es consciente del build ID. Un ISF en caché con un build ID coincidente se reutiliza; un ISF en caché con un build ID diferente se trata como un miss y se reconstruye. Los ISFs heredados en caché sin build ID registrado siguen siendo soft hits para que las cachés antiguas sigan funcionando, y AstinusLab registra el build ID conocido del job en la nueva fila de artefacto.

Las versiones de AstinusLab anteriores a la Fase C escribían `vol3/symbols/linux/<kernel_release>.json`. Esos archivos siguen siendo legibles como entradas de caché soft-hit heredadas cuando no hay registrado un build ID en conflicto. Los nuevos símbolos cualificados por build ID usan `vol3/symbols/linux/<kernel_release>_<buildid-prefix>.json`.

## Override manual (subir ISF / dump)

Cuando la caché build-ID-aware y la cadena live-guest no pueden producir símbolos que ya tienes, AstinusLab permite **Upload ISF** desde el workbench `/volatility` y **Upload memory dump** desde el detalle del job. Ambos overrides omiten salvaguardas y registran procedencia **unverified**; úsalos solo cuando sepas que símbolos y dump corresponden al kernel del job. Endpoints: `POST /api/v1/volatility/targets/{target_id}/isf/upload`, `POST /api/v1/jobs/{job_id}/isf/upload` y `POST /api/v1/jobs/{job_id}/dump/upload`.

### Ejecutar solo-ISF desde la UI

1. Abre **Volatility** en la barra lateral (`/volatility`).
2. Selecciona un target Linux.
3. Selecciona una ruta de build `immediate`.
4. Selecciona **Queue ISF build**.
5. Observa la tarjeta Volatility 3 ISF del detalle del job enlazado o la tabla de historial de builds en Volatility.

Los Jobs siguen ejecutando el trabajo internamente; la página Volatility es el flujo enfocado de preparación de símbolos y **Jobs** queda como ledger de ejecuciones.

### Ejecutar solo-ISF desde la API

Crea un job con `build_vol3_isf: true`. AstinusLab lo trata como un job solo-ISF para los modos `immediate`.

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "build_vol3_isf": true
}
```

## Salida del análisis

Cuando se ejecuta el análisis de plugins, los resultados se escriben en `storage/dumps/<job_id>/vol3/`. Estos archivos pertenecen a ese job y se pueden eliminar junto con los artefactos del job. No son la caché global de símbolos.

`analysis-summary.json` registra `dump_path`, `dump_sha256`, `isf_path`, `isf_sha256`, `symbols_path` y `vol3_version`. Durante cada ejecución AstinusLab prepara el ISF seleccionado en un directorio de símbolos local al job y apunta Volatility 3 a ese directorio para que el ISF registrado sea el único ISF disponible para la ejecución de plugins.

El análisis exitoso también publica `analysis-summary.json` como un artefacto de informe de primera clase:

- `analysis-summary.json.manifest.json` usa `artifact-manifest/v1` con `producer: vol3_analysis` y `kind: report`.
- `analysis-summary.json.sha256` contiene el sidecar SHA-256 del archivo de resumen.
- La fila de Artefacto registra el archivo de informe, la ruta del manifiesto, la ruta del sidecar SHA-256, el tamaño y el checksum.
- La fila de Informe enlaza el artefacto de memoria de entrada con el artefacto de informe de salida.

Plugins comunes incluyen `pslist`, `pstree`, `lsmod`, `mountinfo`, `sockstat`, `ip.Addr` e `ip.Link`.

### Evidencia de ejecuciones de plugin del workbench

Las ejecuciones bajo demanda de un solo plugin desde el workbench **Volatility** también son evidencia de primera clase. Cada ejecución completada escribe `storage/dumps/<job_id>/vol3/runs/<run_id>.json`, publica manifiesto y sidecar SHA-256, y crea filas Artifact/Report que enlazan el artefacto de memoria de entrada con el reporte de salida.

### Ejecutar análisis post-captura

1. Completa un job con un volcado de memoria.
2. Confirma que el job tiene un ISF válido registrado en la tarjeta Volatility 3 ISF.
3. Selecciona **Run Volatility 3** en la página de detalle del job.
4. Revisa `analysis-summary.json` y la salida por plugin en `storage/dumps/<job_id>/vol3/`.

El análisis también requiere que `settings.vol3_volatility_bin` y `settings.vol3_default_plugins` estén configurados.

## Resolución de problemas

### Faltan símbolos de depuración

Síntomas:

- El job solo-ISF falla.
- Los logs mencionan que faltan `vmlinux`, `debuginfo`, `dbgsym` o paquetes del repositorio de depuración.

Soluciones:

- Confirma que la VM ejecuta una distribución Linux x86_64 soportada de `docs/reference/DISTRO_SUPPORT.md`. Las arquitecturas ARM64 (`aarch64`) explícitamente no están soportadas para la generación de ISF ni el análisis de memoria.
- Confirma que la release exacta del kernel tiene paquetes de depuración coincidentes.
- Habilita el repositorio de símbolos de depuración de la distro.
- Inspecciona `storage/dumps/<job_id>/vol3/symbol-build-host.log` para ver el fallo exacto del host fetch.
- Usa el fallback del guest en vivo cuando la obtención de paquetes del lado del host no pueda resolver el paquete.

### ISF inválido

Síntomas:

- El archivo JSON se escribe pero Volatility lo rechaza.
- Las ejecuciones de plugins fallan antes de producir salida útil.

Soluciones:

- Elimina el archivo defectuoso de `vol3/symbols/linux/` y vuelve a ejecutar la generación.
- Comprueba que `dwarf2json` puede analizar el `vmlinux` seleccionado.
- Confirma que el nombre del archivo ISF y los metadatos del job coinciden con el kernel detectado.

### Fallos del repositorio de paquetes

Síntomas:

- Los comandos de paquetes fallan durante el host fetch o la construcción en el guest en vivo.
- Los logs muestran errores de DNS, mirror, GPG, suscripción o metadatos de release.

Soluciones:

- Verifica la red y el DNS desde el host y el guest.
- Comprueba la disponibilidad del mirror del repositorio.
- Da tiempo suficiente para paquetes de depuración grandes; los paquetes de símbolos de Ubuntu/RHEL pueden superar 1 GB.
- Ajusta `[vol3].fetch_timeout_seconds` cuando los mirrors son lentos.
- Para RHEL, confirma la suscripción del repositorio o una fuente de debuginfo equivalente.
- Reintenta con una release de imagen cloud soportada si la release de la distro está al final de su vida.

### Fallos de plugins

Síntomas:

- La generación de ISF tiene éxito pero la salida de plugins falta o está incompleta.
- Los archivos en `storage/dumps/<job_id>/vol3/` contienen errores.

Soluciones:

- Confirma que existe el volcado de memoria del job.
- Confirma que existe el ISF coincidente en `vol3/symbols/linux/`.
- Inspecciona el log de eventos del job y la salida de Volatility por job.
- Vuelve a ejecutar el análisis tras regenerar el ISF si los símbolos eran obsoletos, inválidos o se construyeron para un build de kernel distinto (mismo `uname -r`, build ID diferente — p. ej. tras una recompilación de seguridad del proveedor).
