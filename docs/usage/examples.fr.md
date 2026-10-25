# Exemples

Ces exemples supposent que l'API s'exécute sur `http://localhost:8000`.

## Configuration de l'authentification et de CSRF

AstinusLab protège les points de terminaison d'API mutants (POST, PUT, PATCH, DELETE) contre le CSRF. En mode release (ou avec l'authentification activée), les requêtes doivent fournir un jeton Bearer valide. En mode développement sans authentification de l'API, les requêtes doivent amorcer un cookie `astinuslab_csrf` via `GET /playground` et le renvoyer dans l'en-tête `X-CSRF-Token`.

Définissez cet assistant réutilisable `api_curl` en Bash avant d'exécuter les exemples ci-dessous :

```bash
BASE_URL="${BASE_URL:-http://localhost:8000}"
# Set ASTINUSLAB_TOKEN to the configured admin token for authenticated installs.
# Leave it unset only for local development with authentication disabled.
api_curl() {
  if [ -n "${ASTINUSLAB_TOKEN:-}" ]; then
    curl --fail-with-body -H "Authorization: Bearer $ASTINUSLAB_TOKEN" "$@"
    return $?
  fi
  local cookie_jar csrf_token request_status
  cookie_jar=$(mktemp "${TMPDIR:-/tmp}/astinuslab-cookies.XXXXXX") || return 1
  if ! curl --fail --silent --show-error -c "$cookie_jar" "$BASE_URL/playground" >/dev/null; then
    rm -f "$cookie_jar"
    return 1
  fi
  csrf_token=$(awk '$6 == "astinuslab_csrf" {value=$7} END {print value}' "$cookie_jar")
  if [ -z "$csrf_token" ]; then
    rm -f "$cookie_jar"
    echo "No CSRF cookie: check BASE_URL and authentication mode." >&2
    return 1
  fi
  if curl --fail-with-body -b "$cookie_jar" -H "X-CSRF-Token: $csrf_token" "$@"; then
    request_status=0
  else
    request_status=$?
  fi
  rm -f "$cookie_jar"
  return "$request_status"
}
```

Consultez [Authentification](../api/authentication.md) pour les détails de configuration des jetons et la sécurité.

## Lister les données du catalogue

```bash
api_curl "$BASE_URL/api/v1/targets"
api_curl "$BASE_URL/api/v1/scenarios"
api_curl "$BASE_URL/api/v1/workloads"
api_curl "$BASE_URL/api/v1/cves"
```

## Créer un Job de capture immédiate

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "ubuntu-22-04-default",
    "scenario_id": "immediate-capture",
    "execution_mode": "immediate",
    "capture_type": "memory"
  }'
```

## Créer un Job de labo manuel

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "ubuntu-22-04-default",
    "scenario_id": "lab-session",
    "execution_mode": "manual_trigger",
    "capture_type": "memory",
    "workload_id": "dvwa"
  }'
```

## Déclencher une capture manuelle

À utiliser uniquement après qu'un job manuel atteint `waiting_for_capture` :

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs/<job_id>/capture" \
  -H "Content-Type: application/json" \
  -d '{
    "capture_type": "both",
    "exploit_tags": ["manual-lab"],
    "exploit_notes": "Captured after operator activity"
  }'
```

## Télécharger les artefacts

```bash
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/artifact"
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/disk-artifact"
```

La réponse inclut les en-têtes `X-Astinuslab-SHA256` et `X-Astinuslab-Size` lorsque les métadonnées de somme de contrôle et de taille sont disponibles.

## Créer une session Playground

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/sessions"
```

## Créer un réseau Playground

L'endpoint créer-ou-réutiliser crée une session si nécessaire :

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/networks" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "internal",
    "subnet_cidr": "10.42.10.0/24",
    "isolated": true
  }'
```

## Lancer une VM Playground

Si `network_ids` est omis, le wrapper crée ou réutilise le réseau playground par défaut.

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/vms" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "target-1",
    "image_id": "ubuntu-22-04-default",
    "role": "target",
    "vcpus": 2,
    "memory_mb": 4096,
    "disk_gb": 40
  }'
```

## Capturer une VM Playground

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/vms/<vm_id>/captures" \
  -H "Content-Type: application/json" \
  -d '{"kind": "memory"}'
```

Lister les captures de la VM :

```bash
api_curl "$BASE_URL/api/v1/playground/vms/<vm_id>/captures"
```

Diffuser les événements de capture en flux :

```bash
api_curl -N "$BASE_URL/api/v1/playground/captures/<acquisition_id>/events"
```

## Arrêter une session Playground

```bash
api_curl -X DELETE "$BASE_URL/api/v1/playground/sessions/<session_id>"
```
