# Estructura del repositorio

AstinusLab mantiene código fuente, catálogos, docs, tests y estado de runtime local en ubicaciones estables. Este mapa es solo una guía para contribuidores; no reubica rutas de runtime.

> El objetivo de reorganización por etapas está aprobado, pero esta página sigue describiendo las rutas que ya se han integrado. Consulta la [estructura objetivo aprobada del repositorio](../architecture/repository-target-layout.md) y su matriz de migración; no uses rutas futuras hasta que se integre su etapa.

## Código fuente

```text
src/astinuslab/*.py: 10

src
src/astinuslab
src/astinuslab/api
src/astinuslab/core
src/astinuslab/db
src/astinuslab/forensics
src/astinuslab/i18n_data
src/astinuslab/images
src/astinuslab/jobs
src/astinuslab/live_session
src/astinuslab/migrations
src/astinuslab/orchestration
src/astinuslab/playground
src/astinuslab/storage
src/astinuslab/ui
src/astinuslab/virtualization
src/astinuslab/web
src/astinuslab/workloads
```

- `src/astinuslab/`: app FastAPI instalable, tareas Celery, modelos SQLAlchemy, helpers de orquestación, migraciones Alembic propias del paquete y recursos web bajo `web/`.
- `tests/`: suites Python de aplicación y tooling, contratos shell y tests de navegador.
- `guest/`: scripts, templates y helpers del guest usados dentro de las VMs de lab. Los ejemplos Autounattend para preparar imágenes Windows viven en `guest/windows/`.
- `ops/host/`: helpers instalados de host para libvirt, almacenamiento, ACL y build portable de Windows.
- `ops/images/`: helpers para operadores de descarga de imágenes y metadatos de procedencia.
- `ops/deploy/`: implementaciones canónicas de instalación, configuración de roots privilegiados y desinstalación.
- `ops/systemd/`: templates fuente para los servicios instalados de API, runner y análisis.
- `tools/dev/`: helpers de desarrollo local solo para contribuidores; nunca se instalan en una release.
- `tools/quality/`: checks de política de calidad del repositorio invocados directamente por CI.
- `tools/release/`: herramientas versionadas de evidencia, validación y revisión de release; permanecen en el archivo fuente pero no en la instalación.

## Configuración y catálogos

- `astinuslab.toml`: archivo de configuración local por defecto.
- `config/catalog/`: metadatos de catálogos de targets, atacantes, workloads e imágenes.
- `images/definitions/*.meta.json`: metadatos pequeños de procedencia/catálogo de imágenes VM que pueden revisarse y commitearse.

## Documentación

- `docs/`: páginas fuente de MkDocs. Taxonomía canónica: `docs/user/` para workflows humanos, `docs/api/` para docs de API enfocadas en endpoints, `docs/reference/` para hechos estables de configuración/especificación, `docs/development/` para internos de contribuidor y `docs/community/` para gobernanza/autores.
- `docs/mkdocs.yml`: navegación, tema y configuración i18n de documentación.

## Runtime local y archivos generados

Estas rutas son estado local de runtime/generado. No las muevas como parte de limpieza estructural y no las elimines mientras haya jobs de AstinusLab, sesiones Playground, workers Celery o servidores de docs activos.

- `storage/`: bases SQLite, bases broker/result de Celery, archivos PID, logs, dumps, claves SSH, temporales, caché de workloads y estado Playground. `storage/.gitkeep` mantiene presente el directorio.
- `build/`: scratch raíz ignorado para output local/generado de build. Los helpers Windows reutilizables viven en `ops/host/windows_build/`; los templates guest Windows reutilizables viven en `guest/windows/`. Prefiere `storage/tmp/windows-build/<id>/` para nuevo output por ejecución de build de imágenes Windows.
- `images/definitions/*.{img,qcow2,raw}`: binarios locales de imágenes VM. Los sidecars de metadatos pueden rastrearse; los binarios grandes permanecen locales salvo excepción explícita con procedencia y checksum.
- `vol3/symbols/`: símbolos Volatility 3 generados, logs de build y salida de extracción.
- Las herramientas Volatility 3 y `dwarf2json` instaladas por el operador quedan
  fuera del repositorio y no son archivos generados del árbol fuente.
- `site/`: salida de build de MkDocs.

Consulta [Archivos generados y de runtime](GENERATED_RUNTIME_FILES.md) para reglas de limpieza y checks de seguridad.

## Tests

- `tests/python/`: suites pytest de Python para APIs backend, orquestación, storage, forensics, Playground y helpers de host.
- `tests/python/api/`: tests de superficie REST (`test_api.py`, `test_api_v1_extended.py`, `test_openapi_contract.py`).
- `tests/python/jobs/`: tests del dominio Job — repositorio, máquina de estados, transiciones y runtimes (`test_repositories.py`, `test_job_state_support.py`, `test_job_transitions.py`, `test_runtimes.py`).
- `tests/python/host/`: tests de helpers/scripts host (`test_host_acl_helper.py`, `test_host_helpers.py`, `test_sudoers_helper_install.py`, `test_network_isolation_helper.py`, `test_libvirt_storage_admin_helper.py`).
- `tests/python/forensics/`: tests de pipeline forense/captura.
- `tests/python/playground/`: tests de sesión/VM/red Playground.
- `tests/python/integration/`: tests de integración transversales.
- `tests/python/ui/`: tests de rutas FastAPI/Jinja y templates renderizados del servidor.
- `tests/python/e2e/`: cobertura end-to-end propia del backend con pytest.
- `tests/python/tooling/`: tests pytest de scripts y tooling del repositorio; excluidos de la ejecución predeterminada de `tests/python`, pero ejecutables con `.venv/bin/python -m pytest tests/python/tooling -q`.
- `tests/shell/host/`: pruebas shell de aceptación del host, incluida la matriz de instalación/actualización/rollback.
- `tests/browser/`: smoke/integration/full tests de navegador con Playwright.

### Propiedad de los tests

Todos los tests de aplicación pertenecen a carpetas de dominio bajo `tests/python/`.
La raíz contiene únicamente `conftest.py`, marcadores de paquete y módulos de apoyo.
Los tests del repositorio viven en `tooling/` y se seleccionan explícitamente.

## Estado local de herramientas

- `.claude/`, `.agents/`, `.hermes/`, `.superpowers/`, `.antigravitycli/` y `.worktrees/`: estado local de agentes y herramientas, ignorado por Git.
- Los archivos de instrucciones de agentes (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) y los archivos de exclusión de herramientas como `.cursorignore` son configuración personal y no se versionan.

## Templates y assets frontend

- `src/astinuslab/web/templates/admin_*.html`: páginas admin/workbench como storage, images, logs, settings, workloads y dumps.
- `src/astinuslab/web/templates/job_*.html`: páginas de Job.
- `src/astinuslab/web/templates/partials/`: fragmentos HTMX y parciales reutilizables.
- `src/astinuslab/web/templates/playground.html`: template front-door de Playground.
- `src/astinuslab/web/static/css/layout.css`: stylesheet global de compatibilidad cargada por templates base.
- `src/astinuslab/web/static/css/`: tokens, layout, componentes y estilos por página compartidos.
- `src/astinuslab/web/static/fonts/`: las dos familias tipográficas de marca autoalojadas (woff2), servidas desde el mismo origen para cumplir la CSP `font-src 'self'`.
- `src/astinuslab/web/static/playground/`: módulos JavaScript de Playground.

Preserva rutas públicas, nombres de templates y URLs estáticas durante trabajo estructural salvo que un plan de compatibilidad dedicado diga lo contrario.

### Propiedad de templates

- `src/astinuslab/web/templates/admin_*.html`: páginas admin/workbench.
- `src/astinuslab/web/templates/job_*.html`: páginas job.
- `src/astinuslab/web/templates/partials/`: fragmentos HTMX y parciales reutilizables.
