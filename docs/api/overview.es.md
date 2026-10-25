# Visión general de la API

AstinusLab expone la UI web y la API REST desde la app FastAPI en `src/astinuslab/main.py`.

| Superficie | URL base |
| --- | --- |
| API principal | `http://localhost:8000/api/v1` |
| API de Playground | `http://localhost:8000/api/v1/playground` |
| API JSON de Volatility | `http://localhost:8000/api/v1/volatility` |
| Check de liveness | `http://localhost:8000/health` |
| Check de readiness | `http://localhost:8000/ready` |
| OpenAPI JSON | `http://localhost:8000/openapi.json` |
| Swagger UI | `http://localhost:8000/docs` |
| Assets estáticos | `http://localhost:8000/static/...` |
| Workbench de Volatility | `http://localhost:8000/volatility` |

Usa `/health` para confirmar que el proceso de API atiende peticiones. Usa `/ready` para comprobar su base de datos y storage escribible; los fallos de workers o libvirt se informan como `degraded` sin dejar inaccesible la UI del operador.

Las builds de símbolos solo-ISF siguen siendo Jobs internamente. Los usuarios de UI deben iniciar las builds de símbolos Linux desde `/volatility`; los llamadores de API deben seguir usando `POST /api/v1/jobs` con `build_vol3_isf: true` para jobs Linux `immediate`. Los targets Windows rechazan `build_vol3_isf` porque el análisis de Windows usa símbolos PDB después de la captura de memoria.

Los modelos de petición y respuesta se definen en `src/astinuslab/api/schemas.py`, con algunos modelos de respuesta locales de Playground en `src/astinuslab/playground/api.py` y `src/astinuslab/playground/routes/*.py`.

## Tipo de contenido

Usa cuerpos JSON y define primero `api_curl` desde [Ejemplos API](../usage/examples.md). Gestiona un token bearer admin configurado o la cookie/cabecera CSRF de desarrollo. Las instalaciones release activan la autenticación por defecto.

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs"   -H "Content-Type: application/json"   -d '{"target_id":"ubuntu-22-04-default","scenario_id":"immediate-capture","execution_mode":"immediate"}'
```

Los endpoints de artefactos devuelven respuestas de archivo.

## Autenticación

AstinusLab admite autenticación bearer-token configurable para un único administrador. Los checkouts de desarrollo normalmente la mantienen desactivada en loopback; las instalaciones release la activan por defecto. Cuando está activada, todas las rutas requieren el token bearer excepto `/health`, `/ready`, `/login`, `/logout` y los assets `/static/*`: esto cubre la API JSON, los endpoints de Playground, las descargas de artefactos, la documentación OpenAPI y las propias páginas de la UI web. Consulta [Autenticación](authentication.md).

## Formato de errores

La mayoría de errores de validación y de aplicación usan cuerpos de error JSON de FastAPI con un campo `detail`. Las respuestas de conflicto (`409`) usan el sobre estructurado `{ "detail": string, "code": string, "context": object|null }`. Las transiciones de estado de job inválidas también incluyen los campos legacy de nivel superior `from_status` y `to_status` durante una release.

Consulta [Errores](errors.md).


## Breaking changes y deprecaciones

- `JobOut` ya no expone rutas del filesystem del host (`image_path`, `dump_path`, `disk_dump_path`) ni `celery_task_id`. Usa `has_memory_dump`, `has_disk_dump` y los metadatos de dump existentes; obtén el contenido mediante endpoints de descarga.
- `ArtifactOut` ya no expone rutas de servidor (`path`, `manifest_path`, `sha256_sidecar_path`). Obtén el contenido de artefactos, manifiestos y sidecars sha256 mediante los endpoints de descarga de artefactos.
- `POST /api/v1/jobs/{job_id}/acquire-memory-dump` está deprecado. Usa `POST /api/v1/jobs/{job_id}/capture`; el alias permanece durante una release menor.
- Los endpoints JSON de Volatility están versionados bajo `/api/v1/volatility/*`. Las rutas antiguas `/volatility/api/*` son alias deprecados y emiten `Deprecation: true`; las rutas que no están bajo `/api/v1` son internas de UI e inestables.
