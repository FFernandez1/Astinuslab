# Components

## Mòduls del backend

| Path | Responsabilitat |
| --- | --- |
| `src/astinuslab/main.py` | Construcción de la app FastAPI, mount estático, registro de routers, health check, gate de admin-auth y middleware de cabeceras de seguridad (CSP). |
| `src/astinuslab/core/` | Política de plataforma transversal: configuración, compatibilidad, fake mode, locks, logging, contexto de petición, seguridad, probes de readiness, parseo de salida y estado del host. Les rutes canòniques del paquet són les úniques ubicacions admeses. |
| `src/astinuslab/api/routes.py` | Rutas REST principales `/api/v1` para catálogos, jobs, adquisiciones, artefactos, runtimes y retención. |
| `src/astinuslab/playground/api.py` | Ensamblado del router `/api/v1/playground` y helpers de ruta compartidos. |
| `src/astinuslab/playground/routes/` | Módulos de ruta de Playground para sesiones, redes, VMs, NICs, capturas, métricas, topología, catálogo y recetas. |
| `src/astinuslab/api/schemas.py` | Models Pydantic de petició i resposta. |
| `src/astinuslab/core/config.py` | Càrrega de `astinuslab.toml`, overrides d'entorn, resolució de rutes i validació de settings. |
| `src/astinuslab/db/` | Inicialització, models ORM, repositoris i migracions de la base de dades SQLite. |
| `src/astinuslab/jobs/job_services.py` | Operacions de cicle de vida de Job, control de runtime i servei de retenció. |
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

| Path | Responsabilitat |
| --- | --- |
| `src/astinuslab/web/templates/` | Plantilles Jinja2 per a dashboard, jobs, pàgines admin, playground i partials. |
| `src/astinuslab/web/static/` | Assets CSS i JavaScript. |
| `src/astinuslab/web/static/playground/` | Mòduls client de Playground per a estat, crides API, polling, renderitzat de mapa, modals i comportament de wizard. |

## Scripts d'host

| Path | Responsabilitat |
| --- | --- |
| `astinuslab.sh` | Launcher de desenvolupament, control de processos, neteja, servei de docs i escaneig d'imatges. |
| `ops/deploy/install.sh` | Instal·lador release per a `/opt/astinuslab`, venv, storage, helpers, sudoers, AppArmor i systemd. |
| `ops/deploy/uninstall.sh` | Workflow de desinstal·lació release. |
| `ops/host/` | Scripts del costat host per a libvirt, disc, aïllament de xarxa, caché de workloads i helpers de Volatility. |
| `ops/images/` | Helpers per a operadors de descàrrega d'imatges i metadades de procedència. |
| `ops/systemd/` | Templates font per als serveis instal·lats d'API, runner i anàlisi. |
| `tools/dev/` | Scripts auxiliars de desenvolupament per a venv, DB, neteja i execucions de matriu ISF. |
| `tools/quality/` | Checks de política de qualitat del repositori executats per CI. |
| `tools/release/` | Contractes d'evidència, validació i revisió de release. |
| `guest/scripts/` | Scripts copiats a VMs guest per a detecció d'entorn, checks de disc, helpers de paquets i builds de símbols. |

## Configuració i dades

| Path | Responsabilitat |
| --- | --- |
| `astinuslab.toml` | Settings principals del backend. |
| `config/astinuslab.env.example` | Entorn d'exemple per a launcher/systemd. |
| `config/catalog/` | Targets, scenarios, workloads, imatges attacker, nodes de xarxa i perfils. |
| `images/definitions/` | Imatges cloud locals i sidecars de metadades d'imatge. |
| `storage/` | Base de dades runtime, logs, artefactes, fitxers temporals, claus SSH i caché de workloads. |
| `vol3/` | Scripts de símbols de Volatility 3 i memòria cau de símbols reutilitzable; les eines de l'operador resten fora del repositori. |

## Escalat

### Sostre d'escalat (V1 per disseny)

AstinusLab V1 és un sistema d'un sol host i un sol runner. La invariant portant és: broker Celery SQLite, exactament un worker de **captura/cicle de vida** més un worker dedicat a la cua `analysis` (cadascun amb `--concurrency=1`), i un host propietari de les operacions de libvirt/storage. El worker d'anàlisi executa només `run_vol3_plugin` (només lectura, sense VM ni contacte amb guest); el treball de Volatility 3 del mateix job se serialitza entre tots dos workers mitjançant un lock de fitxer per job, i tots dos workers mantenen curtes les transaccions d'escriptura de DB sota SQLite WAL.

Afegir ingènuament un segon runner o augmentar la concurrència de workers pot trencar tres supòsits:

- The SQLAlchemy/SQLite Celery broker is polling-based and not the scaling boundary for concurrent workers.
- Capture and runtime guards combine `flock` with in-process locks; those do not provide distributed lock semantics across hosts or arbitrary worker processes.
- Reconciliation, waiting-capture expiry, retention, and long acquisition tasks share the same worker thread, so a long capture can delay cleanup by design. Interactive workbench plugin runs are exempt: they execute on the analysis worker.

La capacitat de VM a nivell de producte és un knob separat i segur: `[orchestration] max_concurrent_vms` controla per admissió quantes VMs poden estar actives alhora (VMs de Playground, o un parell target+attacker de Job; mai tots dos, perquè Playground i Jobs continuen sent mútuament exclusius). S'aplica amb un count-then-insert sota SQLite `BEGIN IMMEDIATE` i s'allibera per les mateixes rutes de reconciliació que terminalitzen files de VM. Augmentar-lo mai requereix, i mai s'ha d'implementar mitjançant, augmentar el `--concurrency` del worker.

L'escalat a nivell de worker és un altre projecte: mou primer el broker a Redis o un altre broker de producció, redissenya locks/reconciliació de runner per a diversos workers i després revisa l'escalat de base de dades/storage si la càrrega ho requereix. Fins aleshores, mantén un runner amb concurrency 1.
