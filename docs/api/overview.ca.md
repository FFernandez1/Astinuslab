# Visió general de l'API

AstinusLab exposa la UI web i l'API REST des de l'app FastAPI a `src/astinuslab/main.py`.

| Superfície | URL base |
| --- | --- |
| API principal | `http://localhost:8000/api/v1` |
| API de Playground | `http://localhost:8000/api/v1/playground` |
| API JSON de Volatility | `http://localhost:8000/api/v1/volatility` |
| Check de liveness | `http://localhost:8000/health` |
| Check de readiness | `http://localhost:8000/ready` |
| OpenAPI JSON | `http://localhost:8000/openapi.json` |
| Swagger UI | `http://localhost:8000/docs` |
| Assets estàtics | `http://localhost:8000/static/...` |
| Workbench de Volatility | `http://localhost:8000/volatility` |

Fes servir `/health` per confirmar que el procés de l'API atén peticions. Fes servir `/ready` per comprovar la base de dades i l'emmagatzematge amb escriptura; els errors dels workers o de libvirt s'informen com a `degraded` sense deixar inaccessible la UI de l'operador.

Les builds de símbols només-ISF continuen sent Jobs internament. Els usuaris de UI han d'iniciar les builds de símbols Linux des de `/volatility`; els cridadors d'API han de continuar fent servir `POST /api/v1/jobs` amb `build_vol3_isf: true` per a jobs Linux `immediate`. Els targets Windows rebutgen `build_vol3_isf` perquè l'anàlisi de Windows fa servir símbols PDB després de la captura de memòria.

Els models de petició i resposta es defineixen a `src/astinuslab/api/schemas.py`, amb alguns models de resposta locals de Playground a `src/astinuslab/playground/api.py` i `src/astinuslab/playground/routes/*.py`.

## Tipus de contingut

Utilitza cossos JSON i defineix primer `api_curl` des dels [Exemples API](../usage/examples.md). Gestiona un token bearer admin configurat o la cookie/capçalera CSRF de desenvolupament. Les instal·lacions release activen l’autenticació per defecte.

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs"   -H "Content-Type: application/json"   -d '{"target_id":"ubuntu-22-04-default","scenario_id":"immediate-capture","execution_mode":"immediate"}'
```

Els endpoints d'artefactes retornen respostes de fitxer.

## Autenticació

AstinusLab admet autenticació bearer-token configurable per a un únic administrador. Els checkouts de desenvolupament normalment la mantenen desactivada a loopback; les instal·lacions release l'activen per defecte. Quan està activada, totes les rutes requereixen el token bearer excepte `/health`, `/ready`, `/login`, `/logout` i els assets `/static/*`: això cobreix l'API JSON, els endpoints de Playground, les descàrregues d'artefactes, la documentació OpenAPI i les pròpies pàgines de la UI web. Vegeu [Autenticació](authentication.md).

## Format d'errors

La majoria d'errors de validació i d'aplicació fan servir cossos d'error JSON de FastAPI amb un camp `detail`. Les respostes de conflicte (`409`) fan servir el sobre estructurat `{ "detail": string, "code": string, "context": object|null }`. Les transicions d'estat de job invàlides també inclouen els camps legacy de nivell superior `from_status` i `to_status` durant una release.

Vegeu [Errors](errors.md).


## Breaking changes i deprecacions

- `JobOut` ja no exposa rutes del filesystem de l'host (`image_path`, `dump_path`, `disk_dump_path`) ni `celery_task_id`. Fes servir `has_memory_dump`, `has_disk_dump` i les metadades de dump existents; obtén el contingut mitjançant endpoints de descàrrega.
- `ArtifactOut` ja no exposa rutes de servidor (`path`, `manifest_path`, `sha256_sidecar_path`). Obtén el contingut d'artefactes, manifestos i sidecars sha256 mitjançant els endpoints de descàrrega d'artefactes.
- `POST /api/v1/jobs/{job_id}/acquire-memory-dump` està deprecat. Fes servir `POST /api/v1/jobs/{job_id}/capture`; l'àlies roman durant una release menor.
- Els endpoints JSON de Volatility estan versionats sota `/api/v1/volatility/*`. Les rutes antigues `/volatility/api/*` són àlies deprecats i emeten `Deprecation: true`; les rutes que no estan sota `/api/v1` són internes de UI i inestables.
