# Vue d'ensemble de l'API

AstinusLab expose l'UI web et l'API REST depuis l'app FastAPI dans `src/astinuslab/main.py`.

| Surface | URL de base |
| --- | --- |
| API principale | `http://localhost:8000/api/v1` |
| API Playground | `http://localhost:8000/api/v1/playground` |
| API JSON Volatility | `http://localhost:8000/api/v1/volatility` |
| Check de liveness | `http://localhost:8000/health` |
| Check de readiness | `http://localhost:8000/ready` |
| OpenAPI JSON | `http://localhost:8000/openapi.json` |
| Swagger UI | `http://localhost:8000/docs` |
| Assets statiques | `http://localhost:8000/static/...` |
| Workbench Volatility | `http://localhost:8000/volatility` |

Utilisez `/health` pour confirmer que le processus API sert les requêtes. Utilisez `/ready` pour vérifier sa base de données et son stockage inscriptible ; les échecs des workers ou de libvirt sont signalés par `degraded` sans rendre l'UI opérateur indisponible.

Les builds de symboles ISF-only restent des Jobs en interne. Les utilisateurs de l'UI doivent lancer les builds de symboles Linux depuis `/volatility` ; les appelants API doivent continuer à utiliser `POST /api/v1/jobs` avec `build_vol3_isf: true` pour les jobs Linux `immediate`. Les targets Windows rejettent `build_vol3_isf`, car l'analyse Windows utilise les symboles PDB après la capture mémoire.

Les modèles de requête et de réponse sont définis dans `src/astinuslab/api/schemas.py`, avec quelques modèles de réponse Playground locaux dans `src/astinuslab/playground/api.py` et `src/astinuslab/playground/routes/*.py`.

## Type de contenu

Utilisez des corps JSON et définissez d’abord `api_curl` depuis les [Exemples API](../usage/examples.md). Il utilise un token bearer admin configuré ou le cookie/en-tête CSRF de développement. Les installations release activent l’authentification par défaut.

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs"   -H "Content-Type: application/json"   -d '{"target_id":"ubuntu-22-04-default","scenario_id":"immediate-capture","execution_mode":"immediate"}'
```

Les endpoints d'artefacts renvoient des réponses de fichier.

## Authentification

AstinusLab prend en charge une authentification bearer-token configurable pour un administrateur unique. Les checkouts de développement la désactivent normalement sur loopback ; les installations release l'activent par défaut. Quand elle est activée, toutes les routes nécessitent le token bearer sauf `/health`, `/ready`, `/login`, `/logout` et les assets `/static/*` : cela couvre l'API JSON, les endpoints Playground, les téléchargements d'artefacts, la documentation OpenAPI et les pages de l'UI web elles-mêmes. Voir [Authentification](authentication.md).

## Format d'erreur

La plupart des erreurs de validation et d'application utilisent les corps d'erreur JSON de FastAPI avec un champ `detail`. Les réponses de conflit (`409`) utilisent l'enveloppe structurée `{ "detail": string, "code": string, "context": object|null }`. Les transitions d'état de job invalides incluent aussi les champs legacy de premier niveau `from_status` et `to_status` pendant une release.

Voir [Erreurs](errors.md).


## Breaking changes et dépréciations

- `JobOut` n'expose plus les chemins du filesystem hôte (`image_path`, `dump_path`, `disk_dump_path`) ni `celery_task_id`. Utilisez `has_memory_dump`, `has_disk_dump` et les métadonnées de dump existantes ; récupérez le contenu via les endpoints de téléchargement.
- `ArtifactOut` n'expose plus les chemins serveur (`path`, `manifest_path`, `sha256_sidecar_path`). Récupérez le contenu des artefacts, les manifestes et les sidecars sha256 via les endpoints de téléchargement d'artefacts.
- `POST /api/v1/jobs/{job_id}/acquire-memory-dump` est déprécié. Utilisez `POST /api/v1/jobs/{job_id}/capture` ; l'alias reste disponible pendant une release mineure.
- Les endpoints JSON Volatility sont versionnés sous `/api/v1/volatility/*`. Les anciens chemins `/volatility/api/*` sont des alias dépréciés et émettent `Deprecation: true` ; les routes hors `/api/v1` sont internes à l'UI et instables.
