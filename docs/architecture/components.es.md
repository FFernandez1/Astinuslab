# Componentes

## Módulos del backend

| Path | Responsabilidad |
| --- | --- |
| `src/astinuslab/main.py` | Construcción de la app FastAPI, mount estático, registro de routers, health check, gate de admin-auth y middleware de cabeceras de seguridad (CSP). |
| `src/astinuslab/core/` | Política de plataforma transversal: configuración, compatibilidad, fake mode, locks, logging, contexto de petición, seguridad, probes de readiness, parseo de salida y estado del host. Las rutas canónicas del paquete son las únicas ubicaciones admitidas. |
| `src/astinuslab/api/routes.py` | Rutas REST principales `/api/v1` para catálogos, jobs, adquisiciones, artefactos, runtimes y retención. |
| `src/astinuslab/playground/api.py` | Ensamblado del router `/api/v1/playground` y helpers de ruta compartidos. |
| `src/astinuslab/playground/routes/` | Módulos de ruta de Playground para sesiones, redes, VMs, NICs, capturas, métricas, topología, catálogo y recetas. |
| `src/astinuslab/api/schemas.py` | Modelos Pydantic de petición y respuesta. |
| `src/astinuslab/core/config.py` | Carga de `astinuslab.toml`, overrides de entorno, resolución de rutas y validación de settings. |
| `src/astinuslab/db/` | Inicialización, modelos ORM, repositorios y migraciones de la base de datos SQLite. |
| `src/astinuslab/jobs/job_services.py` | Operaciones de ciclo de vida de Job, control de runtime y servicio de retención. |
| `src/astinuslab/forensics/services.py` | Operaciones de servicio de captura y Volatility 3. |
| `src/astinuslab/forensics/case_services.py` | Operaciones de servicios de casos, artefactos, reportes, adquisiciones y CVE. |
| `src/astinuslab/jobs/celery_app.py` | App Celery, definiciones de tareas y tabla de dispatch `TASK_MAP`. |
| `src/astinuslab/jobs/celery_main.py` | Target de importación de app Celery usado por comandos de launcher y worker systemd. |
| `src/astinuslab/jobs/celery_worker.py` | Shim de importación backward-compatible para entrypoints de worker. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py` | Flujos de job de arranque de VM dirigido por escenario, adquisición y post-script. |
| `src/astinuslab/orchestration/deps.py` | Contenedor explícito de dependencias (`JobRunDeps`) que cablea los flujos de ejecución de job; el punto de inyección de tests. |
| `src/astinuslab/virtualization/` | Ciclo de vida libvirt de dominios/redes/discos, build de seed cloud-init, staging de arranque, esperas guest QGA/SSH y soporte de scripts host/guest. |
| `src/astinuslab/playground/orchestrator.py` | Tareas de sesión, red, VM, captura y limpieza de Playground. |
| `src/astinuslab/playground/repositories.py` | Helpers de persistencia de Playground. |
| `src/astinuslab/playground/recipes.py` | Validación de import/export de recetas JSON de Playground. |
| `src/astinuslab/catalog.py` | Carga de catálogos, descubrimiento de imágenes e inferencia de metadatos de imagen. |
| `src/astinuslab/forensics/vol3_support.py` | Cadena de providers ISF Linux de Volatility 3 y workflow de build de símbolos. |
| `src/astinuslab/forensics/symbols/` | Identidad ISF, caché, providers y lógica de runner. |
| `src/astinuslab/forensics/api.py` | Rutas REST del workbench `/api/v1/volatility`: dumps, catálogo de plugins, ejecuciones de plugins bajo demanda, cola y resultados por ejecución. |
| `src/astinuslab/live_session/` | Flujo de ejecución de live-session (Interactive Lab, wire name `manual_trigger`): `runner.py` posee el flujo, `support.py` posee el arranque del attacker, provisioning de workload y handoff de captura. |
| `src/astinuslab/orchestration/runners/isf_matrix_runner.py` | Ejecuciones batch de matriz ISF entre imágenes target. |
| `src/astinuslab/storage/` | Manifiestos de artefactos y sidecars SHA-256, barridos de retención, keystore SSH, rutas de ejecución y soporte de admin de storage. |
| `src/astinuslab/images/` | Operaciones de subida/admin de imágenes e inspección de metadatos de imagen. |
| `src/astinuslab/workloads/` | Helpers de provisioning de workloads y caché de paquetes de workload. |
| `src/astinuslab/ui/` | Módulos de rutas HTML de UI. |

## Frontend

| Path | Responsabilidad |
| --- | --- |
| `src/astinuslab/web/templates/` | Plantillas Jinja2 para dashboard, jobs, páginas admin, playground y partials. |
| `src/astinuslab/web/static/` | Assets CSS y JavaScript. |
| `src/astinuslab/web/static/playground/` | Módulos cliente de Playground para estado, llamadas API, polling, renderizado de mapa, modales y comportamiento de wizard. |

## Scripts de host

| Path | Responsabilidad |
| --- | --- |
| `astinuslab.sh` | Launcher de desarrollo, control de procesos, limpieza, servicio de docs y escaneo de imágenes. |
| `ops/deploy/install.sh` | Instalador release para `/opt/astinuslab`, venv, storage, helpers, sudoers, AppArmor y systemd. |
| `ops/deploy/uninstall.sh` | Workflow de desinstalación release. |
| `ops/host/` | Scripts del lado host para libvirt, disco, aislamiento de red, caché de workloads y helpers de Volatility. |
| `ops/images/` | Helpers para operadores de descarga de imágenes y metadatos de procedencia. |
| `ops/systemd/` | Templates fuente para los servicios instalados de API, runner y análisis. |
| `tools/dev/` | Scripts auxiliares de desarrollo para venv, DB, limpieza y ejecuciones de matriz ISF. |
| `tools/quality/` | Checks de política de calidad del repositorio ejecutados por CI. |
| `tools/release/` | Contratos de evidencia, validación y revisión de release. |
| `guest/scripts/` | Scripts copiados en VMs guest para detección de entorno, checks de disco, helpers de paquetes y builds de símbolos. |

## Configuración y datos

| Path | Responsabilidad |
| --- | --- |
| `astinuslab.toml` | Settings principales del backend. |
| `config/astinuslab.env.example` | Entorno de ejemplo para launcher/systemd. |
| `config/catalog/` | Targets, scenarios, workloads, imágenes attacker, nodos de red y perfiles. |
| `images/definitions/` | Imágenes cloud locales y sidecars de metadatos de imagen. |
| `storage/` | Base de datos runtime, logs, artefactos, archivos temporales, claves SSH y caché de workloads. |
| `vol3/` | Scripts de símbolos de Volatility 3 y caché de símbolos reutilizable; las herramientas del operador quedan fuera del repositorio. |

## Escalado

### Techo de escalado (V1 por diseño)

AstinusLab V1 es un sistema de un solo host y un solo runner. La invariante de carga es: broker Celery SQLite, exactamente un worker de **captura/ciclo de vida** más un worker dedicado de cola `analysis` (cada uno con `--concurrency=1`), y un host propietario de las operaciones de libvirt/storage. El worker de análisis ejecuta solo `run_vol3_plugin` (solo lectura, sin VM ni contacto con guest); el trabajo de Volatility 3 del mismo job se serializa entre ambos workers mediante un lock de archivo por job, y ambos workers mantienen cortas las transacciones de escritura de DB bajo SQLite WAL.

Añadir ingenuamente un segundo runner o aumentar la concurrencia de workers puede romper tres supuestos:

- The SQLAlchemy/SQLite Celery broker is polling-based and not the scaling boundary for concurrent workers.
- Capture and runtime guards combine `flock` with in-process locks; those do not provide distributed lock semantics across hosts or arbitrary worker processes.
- Reconciliation, waiting-capture expiry, retention, and long acquisition tasks share the same worker thread, so a long capture can delay cleanup by design. Interactive workbench plugin runs are exempt: they execute on the analysis worker.

La capacidad de VM a nivel de producto es un knob separado y seguro: `[orchestration] max_concurrent_vms` controla por admisión cuántas VMs pueden estar activas a la vez (VMs de Playground, o un par target+attacker de Job; nunca ambos, porque Playground y Jobs siguen siendo mutuamente exclusivos). Se aplica con un count-then-insert bajo SQLite `BEGIN IMMEDIATE` y se libera por las mismas rutas de reconciliación que terminalizan filas de VM. Aumentarlo nunca requiere, y nunca debe implementarse mediante, aumentar `--concurrency` del worker.

El escalado a nivel de worker es otro proyecto: mueve primero el broker a Redis u otro broker de producción, rediseña locks/reconciliación de runner para varios workers, y luego revisa el escalado de base de datos/storage si la carga lo requiere. Hasta entonces, mantén un runner con concurrency 1.
