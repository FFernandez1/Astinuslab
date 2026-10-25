# Composants

## Modules du backend

| Path | Responsabilité |
| --- | --- |
| `src/astinuslab/main.py` | Construcción de la app FastAPI, mount estático, registro de routers, health check, gate de admin-auth y middleware de cabeceras de seguridad (CSP). |
| `src/astinuslab/core/` | Politique de plateforme transversale : configuration, compatibilité, fake mode, verrous, journalisation, contexte de requête, sécurité, sondes de readiness, analyse de sortie et état de l’hôte. Les chemins canoniques du paquet sont les seuls emplacements pris en charge. |
| `src/astinuslab/api/routes.py` | Rutas REST principales `/api/v1` para catálogos, jobs, adquisiciones, artefactos, runtimes y retención. |
| `src/astinuslab/playground/api.py` | Ensamblado del router `/api/v1/playground` y helpers de ruta compartidos. |
| `src/astinuslab/playground/routes/` | Módulos de ruta de Playground para sesiones, redes, VMs, NICs, capturas, métricas, topología, catálogo y recetas. |
| `src/astinuslab/api/schemas.py` | Modèles Pydantic de requête et de réponse. |
| `src/astinuslab/core/config.py` | Chargement de `astinuslab.toml`, overrides d’environnement, résolution des chemins et validation des paramètres. |
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

| Path | Responsabilité |
| --- | --- |
| `src/astinuslab/web/templates/` | Templates Jinja2 pour dashboard, jobs, pages admin, playground et partials. |
| `src/astinuslab/web/static/` | Assets CSS et JavaScript. |
| `src/astinuslab/web/static/playground/` | Modules client Playground pour état, appels API, polling, rendu de carte, modales et comportement de wizard. |

## Scripts hôte

| Path | Responsabilité |
| --- | --- |
| `astinuslab.sh` | Lanceur de développement, contrôle de processus, nettoyage, service docs et scan d'images. |
| `ops/deploy/install.sh` | Installateur release pour `/opt/astinuslab`, venv, stockage, helpers, sudoers, AppArmor et systemd. |
| `ops/deploy/uninstall.sh` | Workflow de désinstallation release. |
| `ops/host/` | Scripts côté hôte pour libvirt, disque, isolation réseau, cache de workloads et helpers Volatility. |
| `ops/images/` | Helpers opérateur pour le téléchargement d'images et les métadonnées de provenance. |
| `ops/systemd/` | Modèles source des services API, runner et analyse installés. |
| `tools/dev/` | Scripts auxiliaires de développement pour venv, DB, nettoyage et exécutions de matrice ISF. |
| `tools/quality/` | Contrôles de politique qualité du dépôt exécutés par la CI. |
| `tools/release/` | Contrats de preuve, validation et revue de release. |
| `guest/scripts/` | Scripts copiés dans les VMs guest pour détection d'environnement, checks disque, helpers de paquets et builds de symboles. |

## Configuration et données

| Path | Responsabilité |
| --- | --- |
| `astinuslab.toml` | Paramètres principaux du backend. |
| `config/astinuslab.env.example` | Environnement d'exemple pour lanceur/systemd. |
| `config/catalog/` | Targets, scenarios, workloads, images attacker, nœuds réseau et profils. |
| `images/definitions/` | Images cloud locales et sidecars de métadonnées d'image. |
| `storage/` | Base de données runtime, logs, artefacts, fichiers temporaires, clés SSH et cache de workloads. |
| `vol3/` | Scripts de symboles Volatility 3 et cache de symboles réutilisable ; les outils de l'opérateur restent hors du dépôt. |

## Scaling

### Plafond de scaling (V1 par conception)

AstinusLab V1 est un système mono-hôte, mono-runner. L'invariant porteur est : broker Celery SQLite, exactement un worker de **capture/cycle de vie** plus un worker dédié à la queue `analysis` (chacun avec `--concurrency=1`), et un hôte propriétaire des opérations libvirt/storage. Le worker d'analyse exécute uniquement `run_vol3_plugin` (lecture seule, aucun contact VM ou guest) ; le travail Volatility 3 du même job est sérialisé entre les deux workers par un verrou fichier par job, et les deux workers gardent les transactions d'écriture DB courtes sous SQLite WAL.

Ajouter naïvement un second runner ou augmenter la concurrence des workers peut casser trois hypothèses :

- The SQLAlchemy/SQLite Celery broker is polling-based and not the scaling boundary for concurrent workers.
- Capture and runtime guards combine `flock` with in-process locks; those do not provide distributed lock semantics across hosts or arbitrary worker processes.
- Reconciliation, waiting-capture expiry, retention, and long acquisition tasks share the same worker thread, so a long capture can delay cleanup by design. Interactive workbench plugin runs are exempt: they execute on the analysis worker.

La capacité VM au niveau produit est un réglage séparé et sûr : `[orchestration] max_concurrent_vms` contrôle par admission le nombre de VMs actives simultanément (VMs Playground, ou paire target+attacker d'un Job ; jamais les deux, car Playground et Jobs restent mutuellement exclusifs). Elle est appliquée par count-then-insert sous SQLite `BEGIN IMMEDIATE` et libérée par les mêmes chemins de réconciliation qui terminalisent les lignes VM. L'augmenter ne nécessite jamais, et ne doit jamais être implémenté en, augmentant le `--concurrency` worker.

Le scaling au niveau worker est un autre projet : migrez d'abord le broker vers Redis ou un autre broker de production, redessinez les verrous/réconciliations de runner pour plusieurs workers, puis revisitez le scaling base de données/storage si la charge l'exige. D'ici là, gardez un runner avec concurrency 1.
