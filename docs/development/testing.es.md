# Pruebas

Los tests automatizados se configuran en `pyproject.toml` y GitHub Actions.

Después de `./astinuslab.sh setup`, instala el toolchain frontend fijado y Chromium para los tests de navegador:

```bash
npm ci
npx playwright install chromium
```

Consulta la [matriz de verificación](verification-matrix.md), mantenida en inglés con fallback para evitar duplicar las instrucciones. Aísla el trabajo concurrente y conserva los cambios de otras tareas.

## Tests del backend

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
```

Ejecutar un archivo concreto:

```bash
.venv/bin/python -m pytest tests/python/api/test_api.py -q
```

Ejecutar el gate de cobertura de CI:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

El umbral de cobertura es `84` tanto en `pyproject.toml` como en `.github/workflows/ci.yml`.

La medida es cobertura de **ramas** (`branch = true` en `pyproject.toml`), no de líneas. Este código es denso en cláusulas de guarda, y una guarda cuyo cuerpo nunca se ejecuta sigue contando como línea cubierta. Contar arcos dio 84,3% donde las líneas daban 86,7%, así que el umbral se reajustó a la cifra medida. Súbelo a medida que se cierren arcos; no lo bajes.

### Layout de carpetas de tests del backend

Los tests bajo `tests/python/` se organizan en carpetas de dominio: API, forensics, jobs, playground, lab_packages, host, UI, security, integration, e2e y `host_real`. Ejecuta una carpeta de dominio de forma aislada con comandos como:

```bash
.venv/bin/python -m pytest tests/python/playground -q
.venv/bin/python -m pytest tests/python/forensics -q
.venv/bin/python -m pytest tests/python/jobs -q
ASTINUSLAB_REAL_HOST=1 .venv/bin/python -m pytest tests/python/host_real -q  # solo host KVM/libvirt real
```

`tests/python/ui/` contiene tests pytest de rutas FastAPI/Jinja renderizadas en servidor; `tests/browser/` contiene tests de navegador Playwright.

## Tests unitarios del repositorio

Los tests unitarios del instalador, helpers de host, launcher y tema de documentación viven en `tests/python/tooling`:

```bash
.venv/bin/python -m pytest tests/python/tooling -q
```

## Niveles de test

Usa el nivel seguro más bajo que cubra el cambio:

| Nivel | Comando | Alcance | Seguridad |
| --- | --- | --- | --- |
| Nivel 0 estático | `.venv/bin/python -m ruff check .`, `.venv/bin/python -m mypy src tests/python ops/deploy`, `.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict`, `npm run lint:js` | Sintaxis, imports, tipos, documentación y lint de JavaScript | Sin VM ni mutación del host |
| Nivel 1 backend | `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q` | Tests unitarios, servicios, repositorios, API y UI renderizada por servidor | Usa fakes/fixtures; los archivos e2e se omiten salvo petición explícita |
| Nivel 1b tooling del repositorio | `.venv/bin/python -m pytest tests/python/tooling -q` | Tests unitarios del instalador, launcher, helpers de host y tema de documentación | Usa archivos temporales y scripts falsos |
| Nivel 2 backend e2e | `.venv/bin/python -m pytest tests/python/e2e -q` | Cobertura de navegador Playwright propiedad del backend contra un backend fake-libvirt | Se ejecuta en CI después de los smoke tests frontend |
| Nivel 2 smoke UI | `npm run test:smoke` | Smoke tests de navegador con mocks de rutas/API | Sin POSTs de provisión ni mutación de libvirt |
| Nivel 2 integración UI | `npm run test:integration` | Checks de lectura/validación con backend real | Manual por ahora; mantener aislado del estado runtime del desarrollador |
| Nivel 3 e2e VM completo | `ASTINUSLAB_E2E_FULL=1 npm run test:e2e:full` | Flujos completos navegador + backend + VMs libvirt | Solo manual; puede mutar libvirt y almacenamiento del host |

### Ejecutar un job más allá de la admisión

Crear un Job es seguro en cualquier sitio. *Ejecutarlo* no lo es: una ejecución
instala una imagen base mediante `sudo libvirt_storage_admin.sh` y define un
dominio, así que en un host con esa regla de sudo configurada — el estado normal
de una máquina que ejecuta este producto — lanzar un job desde un test provisiona
una VM real con un disco real. Ni `ASTINUSLAB_FAKE_LIBVIRT` ni
`ASTINUSLAB_FAKE_PLAYGROUND` cubren ese camino.

```bash
ASTINUSLAB_FAKE_ORCHESTRATION=1
```

sustituye las dependencias de `JobRunDeps` que tocan el host, y nada más: los
runners recorren el grafo de estados real y escriben los eventos reales. Un job
`manual_trigger` llega a `waiting_for_capture`, así que la progresión de etapas,
la cancelación y el disparador de captura quedan al alcance. Un job de captura
hace el mismo arranque y luego **falla** donde habría producido evidencia: fingir
un arranque es fingir una observación, fingir una adquisición es falsificar un
artefacto, y el flag no lo hace.

Cualquier comando que cruzaría al host lanza una excepción en lugar de devolver
un éxito simulado, de modo que un camino no previsto por el fake falla el test en
vez de llegar a la máquina. El flag está desactivado salvo que se defina, lo
reportan `/health` y el banner de fake mode, y es deliberadamente independiente
de los otros dos para que el nivel de Jobs siga probando la admisión contra un
backend que nunca ejecuta.

Los niveles Playwright de navegador usan `venv/bin/python` por defecto y pueden apuntar a otro intérprete cuando un host de release o una imagen de CI usa otra ruta de venv:

```bash
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:smoke
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:integration
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:e2e:full
```

No ejecutes Nivel 3 en un host con labs activos hasta comprobar VMs, redes y rutas de almacenamiento en ejecución. Mantén e2e VM completo fuera del CI de PR por defecto salvo que exista un runner libvirt dedicado.

## Evidencia de release (libvirt real)

CI usa fakes de libvirt, así que antes de etiquetar una release ejecuta los caminos de VM real en un host libvirt y registra el resultado. Una suite pytest verde es obligatoria, pero no basta para dar GO porque CI no cubre todos los caminos de libvirt, guest, captura y artefactos de producción.

1. Antes de recoger evidencia de host, parte del commit de release: `git status --short` debe estar vacío y debes registrar el valor exacto de `git rev-parse HEAD` en las notas de release o en el paquete de evidencia.
2. Asegura una imagen preparada (con su backing file presente en `images/definitions/`) y una clave SSH activa generada en la página Security (`/ssh-keys`).
3. Ejecuta el Tier-3 full E2E: `npm run test:e2e:full`.
4. Ejecuta los flujos verificados en host sobre el commit de release registrado y prepara el JSON de siete filas y las pruebas descritas en el procedimiento interno de aceptación de release.
5. Recoge y valida de forma independiente el bundle privado con `collect_release_evidence.sh` y `validate_release_evidence.py` según ese procedimiento.
6. Mantén el bundle privado bajo control del responsable de release. Solo su proyección `public/`, generada y revisada, puede publicarse.

| # | Flujo | Cómo ejecutarlo | Evidencia requerida |
| --- | --- | --- | --- |
| 1 | Jobs / `immediate`, memoria | API o UI, cualquier target | Job completado, trío del artefacto de memoria, sha256 independiente y prueba del flujo |
| 2 | Jobs / `post_script` o interactivo, disco o `both` | API o UI, cualquier target (disco se rechaza en modo `immediate`) | Job completado, trío del artefacto de disco, sha256 independiente y prueba del flujo |
| 3 | Jobs / `post_script` | Escenario `workload-sample-post-script` | Job completado, trío del artefacto, sha256 independiente y log de eventos desde `running_scenario_script` hasta `waiting_for_capture` |
| 4 | Captura interactiva de Job | Disparar captura desde la UI | Job completado, trío del artefacto, traza Playwright y captura de pantalla |
| 5 | Instalación de workload en Playground | Instalar DVWA desde una imagen local ya preparada, sin salida externa | Identidad del workload completado y prueba de accesibilidad o backend equivalente; sin trío de artefacto |
| 6 | Captura de memoria en Playground | Botón de captura de la UI | Adquisición completada, trío del artefacto de memoria, traza Playwright y captura de pantalla |
| 7 | Captura de disco en Playground | Botón de captura de la UI | Adquisición completada, trío del artefacto de disco, prueba de reanudación, traza Playwright y captura de pantalla |

GO requiere evidencia de las siete filas producida en el commit de release en un host KVM real. Una fila ausente sin waiver completo de seis campos fuerza NO-GO; cualquier waiver completo y vigente limita el veredicto a GO-WITH-RISKS. No se prevén waivers para 1.0.0.

El collector valida los artefactos raw donde están, pero nunca los copia. `evidence.json` privado es la autoridad; manifiestos, sidecars, pruebas, checksums y `HOST_WORKFLOW_CHECKLIST.md` son material de integridad o vistas generadas. Nunca edites el checklist para convertirlo en PASS. El validator independiente debe informar PASS para el candidato exacto y limpio antes de revisar la release.

## Marcadores de pytest

Los marcadores base se registran en `pyproject.toml` para mantener explícitas las ejecuciones enfocadas:

```bash
.venv/bin/python -m pytest --markers
.venv/bin/python -m pytest tests/python -m "not e2e" --collect-only -q
.venv/bin/python -m pytest tests/python/e2e -m e2e --collect-only -q
.venv/bin/python -m pytest tests/python/tooling -q
```

## Lint y type check

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
```

Ruff selecciona `E9`, `F`, `I`, `UP`, `B` y `S`. Mypy está configurado para Python 3.12 con `check_untyped_defs = true`.

## Scripts de shell

Cuando ShellCheck está instalado:

```bash
shellcheck ops/host/*.sh
```

## Documentación

```bash
.venv/bin/python -m pip install "mkdocs-material>=9.5" "mkdocs-static-i18n>=1.2"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

## CI

`.github/workflows/ci.yml` se ejecuta en push y pull request. El job backend se ejecuta en Python 3.12 y 3.13; el job frontend/Playwright, incluido pytest e2e del backend, se ejecuta en Python 3.12:

1. Instalar `libvirt-dev` y `pkg-config`.
2. Instala `requirements-dev.lock` — generado a partir de `uv.lock`, nunca editado manualmente.
   Consulta [Bloqueo de dependencias](../architecture/dependency-locking.md).
3. Instalar el proyecto con `python -m pip install -e . --no-deps`.
4. Ejecutar lint y comprobación de formato de Ruff.
5. Ejecutar Mypy.
6. Ejecutar pytest del backend con el gate de cobertura.
7. Ejecutar los tests de tooling del repositorio desde `tests/python/tooling`.
8. Ejecutar lint frontend, comprobación de formato, tests unitarios JavaScript y smoke tests Playwright.
9. Instalar Chromium para Python Playwright.
10. Ejecutar pytest e2e del backend desde `tests/python/e2e`.
11. Subir los artefactos HTML de cobertura y del informe Playwright.

`.github/workflows/vol3-symbols.yml` se ejecuta en pull requests que tocan rutas relacionadas con Volatility 3 y símbolos.
