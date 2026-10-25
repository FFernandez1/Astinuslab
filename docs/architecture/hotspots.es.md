# Hotspots del código y presupuestos de tamaño

Esta página lista archivos y funciones que superan los presupuestos de tamaño
blandos. El objetivo es la visibilidad, no una política dura: los archivos
grandes se toleran cuando la responsabilidad es genuinamente única, pero el
crecimiento ilimitado debería disparar una extracción enfocada antes del
siguiente cambio no trivial.

Cuando toques cualquier elemento de esta lista y el cambio añada más de ~20
líneas netas, prefiere un pequeño paso de extracción que baje el conteo.

Los números siguientes están verificados por
`tests/unit/test_hotspot_budgets.py`, así que no pueden desviarse: si un
elemento listado crece, la suite falla hasta que se reescribe la columna de LOC,
y un archivo o función que cruce un umbral de descubrimiento falla hasta que se
añade a la tabla. Los techos blandos **no** se aplican -- varias entradas están
por encima de su techo por decisión deliberada, y las notas explican por qué.

Lo que sí se impone es la dirección del cambio. La misma suite registra, por
tabla, cuántas entradas superan su techo, por cuántas líneas en total y cuánto
suman los techos. Las tres cifras se redeclaran exactamente, y la regla para
redeclararlas es que solo pueden bajar. Subir un techo para que una entrada
cumpla no funciona: la suma de los techos cambia y la suite avisa de que la
tabla ha empeorado. La complejidad ciclomática tiene su propio trinquete en
`pyproject.toml` (`lint.mccabe.max-complexity`), fijado en la peor función del
árbol para que nada nuevo pueda ser peor que lo que ya es lo peor.

## Cómo se miden los números

- **LOC de archivo** es el conteo de líneas físicas, tal como lo reporta `wc -l`.
- **LOC de función** va de la línea `def` hasta la última línea del cuerpo. Los
  decoradores quedan excluidos, así que añadir uno no mueve el número.
- El escaneo de funciones cubre funciones de nivel de módulo y métodos de clases
  de nivel de módulo. Los closures anidados dentro de una función ya se cuentan
  dentro de su entrada contenedora.
- **LOC de archivo de shell** es el conteo de líneas físicas de un `*.sh`
  seguido por Git.
- **Python incrustado en shell** es el número de líneas dentro de los cuerpos
  heredoc `python3 - ... <<'PY'` de ese script, hallados con el mismo helper
  de descubrimiento que usan los gates de lint, de modo que nunca puedan
  discrepar sobre qué cuenta como Python incrustado.

## Umbrales de descubrimiento

Cualquier cosa que alcance o supere estos tamaños **debe** aparecer abajo.
Los dos primeros se aplican a `src/astinuslab`; los dos últimos se aplican a
cada `*.sh` seguido por Git:

- Archivos: **900** líneas.
- Funciones: **180** líneas.
- Archivos de shell: **700** líneas.
- Python incrustado en shell: **250** líneas.

Son más laxos que los techos por entrada a propósito. Un techo dice dónde
debería acabar un módulo; un umbral dice el punto a partir del cual quedarse
fuera de esta página deja de ser una opción. Listar algo más pequeño siempre
está permitido, y unas cuantas entradas se siguen de forma proactiva aun estando
todavía por debajo de su techo.

## Gates de baseline

Última medición completa 2026-09-05, en `feature/portable-lab-sharing`:

- `.venv/bin/python -m ruff check .`: pasando.
- `.venv/bin/python -m ruff format --check .`: pasando, 499 archivos.
- `.venv/bin/python -m mypy src tests/python`: pasando con mypy 2.1.0, 455 archivos fuente.
- `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q`:
  pasando, 4005 passed, 18 skipped, cobertura total 86.02%.
- `.venv/bin/python -m pytest tests/python/tooling -q`: pasando, 933 passed,
  2 skipped. Comprobado de nuevo el 2026-09-06 tras mover el diseño durable de
  portable-lab-sharing a `docs/plans/`, dejando `docs/superpowers/` local a las
  herramientas y sin versionar.

Los archivos Python de backend más grandes siguen siendo los módulos de flujo de
orquestación / Volatility 3. Vuelve a ejecutar los gates de calidad después de
tocar cualquier módulo listado; el gate de cobertura activo es el 86% de
líneas *y ramas*.

## Archivos largos

| Archivo | LOC actual | Techo blando | Notas |
|------|-------------|--------------|-------|
| `src/astinuslab/i18n_data/en.py` | 975 | 1100 | Datos de idioma con claves y marcadores equivalentes en los cuatro idiomas. Conserva la terminología y las comprobaciones de paridad al dividir. |
| `src/astinuslab/i18n_data/es.py` | 975 | 1100 | Datos de idioma con claves y marcadores equivalentes en los cuatro idiomas. Conserva la terminología y las comprobaciones de paridad al dividir. |
| `src/astinuslab/i18n_data/fr.py` | 975 | 1100 | Datos de idioma con claves y marcadores equivalentes en los cuatro idiomas. Conserva la terminología y las comprobaciones de paridad al dividir. |
| `src/astinuslab/i18n_data/ca.py` | 975 | 1100 | Datos de idioma con claves y marcadores equivalentes en los cuatro idiomas. Conserva la terminología y las comprobaciones de paridad al dividir. |
| `src/astinuslab/lab_packages/package_service.py` | 1827 | 1000 | Fachada del ciclo de vida de paquetes portables: importación, preparación, lanzamiento, reinicio, desmontaje, borrado y reconciliación. El módulo nuevo más grande de la página. La ruta de reconciliación y la máquina de estados por acción son las dos piezas cohesivas que conviene extraer primero. |
| `src/astinuslab/lab_packages/prepare_service.py` | 963 | 900 | Admisión de Prepare, leases de retención, reclamaciones de ejecución y propiedad de los activos verificados. R33 separa la reutilización inmutable compartida de la mutación exclusiva; conserva el orden de los bloqueos y las pruebas de recuperación al extraer partes. |
| `src/astinuslab/lab_packages/verification_service.py` | 1463 | 1000 | Verificación de arranque limpio del lado del creador: ciclo de vida de la sesión, vallado de workers, evaluación de checkpoints y cancelación. El vallado y la evaluación son separables; mantén la contabilidad de leases en un solo sitio al dividir. |
| `src/astinuslab/lab_packages/archive.py` | 1270 | 1000 | Lector y escritor de `.astinuslab` más las comprobaciones de seguridad del archivo: travesía de rutas, enlaces, rutas duplicadas o que colisionan por mayúsculas, entradas no declaradas y bombas de compresión. Aquí la longitud es la lista de amenazas; extrae el escritor antes que los validadores. |
| `src/astinuslab/lab_packages/api.py` | 1523 | 900 | Rutas FastAPI del flujo de paquetes. Cuerpos de ruta planos, así que divide por etapa del flujo -- inspección/importación, preparación, lanzamiento, verificación -- conforme crezca. |
| `src/astinuslab/forensics/vol3_support.py` | 1207 | 1000 | Construcción de símbolos Volatility 3 / ISF en el paquete `forensics/`. La cadena de construcción de símbolos está fuertemente acoplada al estado del proveedor; extrae solo con cobertura dirigida. |
| `src/astinuslab/playground/repositories.py` | 1269 | 900 | Persistencia del Playground: ciclo de vida de filas de VM, red y NIC, más los helpers de reclamación compare-and-swap. Mayormente funciones de consulta planas, así que el crecimiento es barato de dividir por entidad. |
| `src/astinuslab/db/models.py` | 1163 | 1200 | Definiciones declarativas de tablas de todo el esquema; creció con las tablas de paquete, instancia y verificación. El crecimiento es una clase por tabla y no lógica enredada, así que el techo generoso es deliberado. Divide por dominio solo si el archivo deja de leerse de arriba abajo. |
| `src/astinuslab/catalog.py` | 1096 | 900 | Carga de catálogo, selectores de compatibilidad e inferencia de descubrimiento de imágenes. Mantén las adiciones estrechas o extrae helpers cohesivos de parseo/normalización. |
| `src/astinuslab/jobs/package_instance_coordinator.py` | 1518 | 900 | Coordinador del lado de Celery para las acciones de instancia de paquete: reserva, arranque bloqueado, asignación de overlays y limpieza de reclamaciones obsoletas. La limpieza y la reserva toman locks; extráelas juntas o no las extraigas. |
| `src/astinuslab/storage/publication.py` | 1070 | 900 | Publicación de artefactos, hashing y escritura de manifiestos. Divídelo por tipo de artefacto si vuelve a crecer. |
| `src/astinuslab/orchestration/deps.py` | 777 | 1500 | Contenedor de dependencias explícito que sustituyó al shim de reexportación disuelto de `orchestrator.py`; mayormente closures de cableado por defecto. Techo deliberadamente generoso -- ver la nota de `JobRunDeps.default` abajo. |
| `src/astinuslab/db/repositories.py` | 1033 | 900 | Consultas de persistencia y controles del ciclo de vida; paginación acotada y recuentos agrupados añadidos por la auditoría del backend. Separar por entidad antes de ampliarlo. |

## Funciones largas

| Función | LOC | Techo blando | Notas |
|----------|-----|--------------|-------|
| `src/astinuslab/orchestration/deps.py::JobRunDeps.default` | 623 | 600 | El bloque de cableado de producción: un closure por dependencia inyectada. La complejidad ciclomática es 1, así que aquí la longitud es una propiedad del patrón de cableado explícito y no de lógica enredada. Trata el *crecimiento* como la señal, no el número absoluto; si pasa el techo, divide el contenedor por dominio en vez de acortar los closures. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare_windows` | 235 | 180 | Preflight de Windows, firmware por instancia, bootstrap QGA y limpieza. Conservar el orden de las fases y las pruebas de arranque. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare` | 210 | 180 | Selección de plataforma y preparación del arranque Linux. Conservar el límite entre plataformas y la limpieza de errores. |
| `src/astinuslab/forensics/vol3_analysis.py::_run_reserved_analysis` | 417 | 180 | Riesgo de regresión más alto que los módulos de flujo de job-run. |
| `src/astinuslab/forensics/capture_support.py::_acquire_and_publish_locked` | 320 | 180 | Mantiene la semántica de bloqueo del lado de captura. |
| `src/astinuslab/forensics/vol3_support.py::run_vol3_symbol_build` | 280 | 180 | La cadena de construcción de símbolos está fuertemente acoplada al estado del proveedor. |
| `src/astinuslab/jobs/startup_recovery.py::cleanup_stale_playground_resources` | 266 | 180 | Limpieza de arranque con revalidación de propiedad en la limpieza de libvirt, del sistema de archivos y de la base de datos. Mantiene intactos los recursos de los paquetes, la verificación congelada y la custodia de las capturas; extrae una etapa cada vez solo con cobertura específica de custodia. |
| `src/astinuslab/forensics/vol3_plugin_run.py::run_vol3_plugin` | 395 | 180 | Invocación del plugin, selección de ISF y persistencia de resultados en un solo cuerpo. |
| `src/astinuslab/lab_packages/package_service.py::LabPackageService.reconcile` | 237 | 180 | Reconciliación de arranque para instancias de paquete, leases de contenido y acciones interrumpidas. Misma clase de riesgo que `reconcile_libvirt_resources`: un bug aquí filtra almacenamiento y VMs, así que extrae una etapa cada vez con cobertura dirigida. |
| `src/astinuslab/playground/vm_lifecycle.py::spawn_vm` | 190 | 180 | Baja desde 416, y sin cambios por la división en dominios: cambió de módulo, no se recortó más. Queda el esqueleto try/except/finally que posee el lease de reclamación y la limpieza de fallos, más las llamadas a las ocho fases extraídas de ella. Cada recorte adicional añadiría un sitio de llamada tan largo como el bloque que elimina. |
| `src/astinuslab/lab_packages/export_service.py::export_locked` | 230 | 180 | Exportación bloqueada: comprobación de atestación, congelado de la definición, cálculo de digests de capas y ensamblado del archivo. La mitad de digests y ensamblado es la extracción más limpia. |
| `src/astinuslab/db/__init__.py::restore_database` | 209 | 180 | Restauración de copia con comprobaciones de esquema y rollback. Intensivo en ramas por naturaleza; mantén el camino de rollback en un solo lugar al dividirlo. |
| `src/astinuslab/lab_packages/verification_service.py::_run_verification` | 205 | 180 | Un intento de verificación de arranque limpio de principio a fin. Con muchas ramas porque cada etapa lleva una salida por cancelación y otra por fallo. |
| `src/astinuslab/lab_packages/archive.py::write_archive_to` | 201 | 180 | Escritor del archivo: orden de miembros, manifiesto canónico y streaming de payloads en una sola pasada para que la salida siga siendo determinista byte a byte. Dividirlo pone en riesgo el determinismo que existe para garantizar. |
| `src/astinuslab/lab_packages/archive.py::inspect_archive` | 201 | 180 | La ruta de inspección pasiva: todas las comprobaciones de seguridad del archivo que se ejecutan antes de importar nada. La longitud es la lista de amenazas; mantén las comprobaciones en una secuencia legible. |
| `src/astinuslab/lab_packages/prepare_service.py::run_prepare` | 41 | 180 | Adquisición de contenido: primero los bytes embebidos, luego la caché local y después las fuentes fijadas explícitamente, con publicación atómica y reanudación. |
| `src/astinuslab/virtualization/process_support.py::run_command` | 221 | 180 | Bounded process supervision and exception-preserving cleanup; keep custody transitions together. |
| `src/astinuslab/lab_packages/launch_service.py::launch_package` | 197 | 180 | Lanzamiento transaccional: recálculo del preflight, reserva, creación de overlays y rollback ante cualquier fallo. Apenas por encima del techo; mantén entera la ruta de rollback. |
| `src/astinuslab/jobs/runtime_support.py::parse_guest_ip` | 156 | 150 | El parseo de red del guest tiene muchas ramas de fallback. Mantén los cambios estrechos y respaldados por tests de compatibilidad. |

## Scripts de shell largos

El shell es la superficie menos comprobada del proyecto. ShellCheck y
`bash -n` atrapan sintaxis y comillas; nada lee estos archivos en busca de
lógica, no hay medición de cobertura, y la única herramienta de aceptación
ejecutable -- `tests/shell/host/upgrade_matrix.sh` sigue necesitando un host desechable y una persona que lo lance. Ahora se puede disparar como `.github/workflows/upgrade-acceptance.yml` en vez de montarlo a mano, pero deliberadamente no está programado: instala y desinstala de verdad, como root, así que se ejecuta donde alguien ha declarado un host prescindible. Los dos defectos
bloqueantes hallados por la aceptación de host estaban en
`ops/deploy/install.sh`, y uno era un patrón `rsync --exclude` sin anclar. El
tamaño es el único indicador barato de ese riesgo, así que se presupuesta
aquí como la superficie de Python.

| Script | LOC actual | Techo blando | Notas |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh` | 3590 | 2500 | Sigue siendo el archivo de mayor consecuencia del repositorio: se ejecuta como root, crea identidades, mueve ACL y política MAC, y confirma o revierte una actualización. 136 funciones, así que su longitud es composición y no un único bloque. Baja desde 5151 ahora que los cuerpos de snapshot/restauración de ACL, manifiesto de travesía, registro de transacciones, clave SSH, configuración gestionada, permisos, autoría de configuración, marcador de identidad y limpieza heredada son módulos reales. Redúcelo más sacando cuerpos de heredoc (ver la tabla siguiente), no fusionando funciones de shell. |
| `tests/shell/host/upgrade_matrix.sh` | 1374 | 1200 | La matriz de aceptación de instalación, actualización y rollback. La longitud es el número de filas y la evidencia que cada fila registra; dividirla dividiría el informe único que lee quien revisa una release. Se presupuesta para que el crecimiento siga siendo deliberado. |
| `astinuslab.sh` | 1211 | 900 | Lanzador de operador y `setup-host`. Mezcla control de servicios en modo dev con preparación de host; la mitad de preparación de host es la separable. |
| `ops/deploy/configure-privileged-roots.sh` | 782 | 700 | Configuración privilegiada de la raíz de almacenamiento, con registro de transacciones y rollback exacto. El propio rollback vive en `ops/deploy/host_access.py`, que este script y el instalador llaman, y la comprobación del marcador de identidad en `ops/deploy/privileged_state.py`, que llaman los tres puntos de entrada privilegiados. El 35% de lo que queda sigue siendo Python incrustado. |

## Python incrustado en shell

1.581 líneas de Python viven dentro de heredocs `<<'PY'`. Ruff, Mypy y la
cobertura se basan en `*.py`, así que ninguno ve este código; hasta que el
helper de descubrimiento se amplió para continuaciones con barra invertida,
791 de esas líneas ni siquiera se comprobaban sintácticamente.
`tests/python/tooling/test_host_scripts.py` ahora compila cada cuerpo y le
pasa pyflakes, pero eso es un suelo, no comprobación de tipos ni cobertura.
Trata un número creciente aquí como la señal de enviar la lógica como módulo
real bajo `ops/` y llamarlo, no como motivo para subir el techo.

2.419 líneas ya han seguido ese camino: `ops/deploy/acl_snapshot.py`,
`ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`,
`ops/deploy/managed_config.py`, `ops/deploy/permissions.py` y
`ops/deploy/privileged_state.py` no los importa nadie y se invocan como
subprocesos, exactamente igual que los heredocs, pero ahora Ruff, Mypy y sus
propios tests unitarios sí los leen. `ops/deploy/uninstall.sh` dejó así ambas
tablas: pasó de 819 líneas y 480 de Python incrustado a 349 y ninguna.

| Script | LOC de Python incrustado | Techo blando | Notas |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh::embedded-python` | 677 | 800 | Baja desde 2333: los cuerpos de snapshot/restauración de ACL, manifiesto de travesía, registro de transacciones, clave SSH, configuración gestionada, permisos, autoría de configuración, marcador de identidad y limpieza heredada de vol3 son ahora `ops/deploy/acl_snapshot.py`, `ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`, `ops/deploy/managed_config.py`, `ops/deploy/permissions.py` y `ops/deploy/privileged_state.py`. Lo que queda es una cola larga sin un único cuerpo grande. |
| `ops/deploy/configure-privileged-roots.sh::embedded-python` | 276 | 300 | Bajo su techo desde que el cuerpo de rollback de 281 líneas pasó a `ops/deploy/host_access.py`, compartido con el instalador que escribe los registros que lee, y la comprobación del marcador de identidad pasó a `ops/deploy/privileged_state.py`. Queda la comprobación de autoridad de la fuente de instalación, de 92 líneas. |

## Qué hacer cuando esta lista crece

1. Elige el elemento más pequeño que supere su techo.
2. Extrae el bloque contiguo de responsabilidad única más largo a un
   helper hermano en el mismo módulo (o un `_helpers.py` hermano si
   emerge un tema claro).
3. El helper debe tomar todas las entradas como parámetros y devolver valores
   explícitamente. Sin estado mutable compartido.
4. Ejecuta el slice de tests dirigido para el módulo tocado, después el
   `ASTINUSLAB_FAKE_LIBVIRT=1 pytest -q` completo.
5. Actualiza los números de LOC en este archivo en el mismo commit.
   `tests/python/tooling/test_hotspot_budgets.py` falla hasta que lo hagas.

Para una entrada de shell, el paso 2 es distinto: el objetivo de extracción
es un módulo `*.py` real bajo `ops/`, invocado como subproceso, no otra
función de shell. Eso es lo que mueve el código bajo Ruff, Mypy y cobertura.
