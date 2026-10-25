# Exemples

Aquests exemples assumeixen que l'API s'executa a `http://localhost:8000`.

## Configuració d'autenticació i CSRF

AstinusLab protegeix els endpoints mutables de l'API (POST, PUT, PATCH, DELETE) contra CSRF. En mode release (o amb autenticació habilitada), les peticions han de proporcionar un token Bearer vàlid. En mode desenvolupament sense autenticació de l'API, les peticions han d'inicialitzar una cookie `astinuslab_csrf` mitjançant `GET /playground` i retornar-la a la capçalera `X-CSRF-Token`.

Defineix aquest helper reutilitzable `api_curl` en Bash abans d'executar els exemples següents:

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

Consulta [Autenticació](../api/authentication.md) per a més detalls sobre tokens i seguretat.

## Llistar dades del catàleg

```bash
api_curl "$BASE_URL/api/v1/targets"
api_curl "$BASE_URL/api/v1/scenarios"
api_curl "$BASE_URL/api/v1/workloads"
api_curl "$BASE_URL/api/v1/cves"
```

## Crear un Job de captura immediata

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

## Crear un Job de lab manual

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

## Disparar una captura manual

Fes-ho servir només després que un job manual arribi a `waiting_for_capture`:

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs/<job_id>/capture" \
  -H "Content-Type: application/json" \
  -d '{
    "capture_type": "both",
    "exploit_tags": ["manual-lab"],
    "exploit_notes": "Captured after operator activity"
  }'
```

## Descarregar artefactes

```bash
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/artifact"
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/disk-artifact"
```

La resposta inclou les capçaleres `X-Astinuslab-SHA256` i `X-Astinuslab-Size` quan hi ha metadades de checksum i mida disponibles.

## Crear una sessió de Playground

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/sessions"
```

## Crear una xarxa de Playground

L'endpoint de crear-o-reutilitzar crea una sessió si cal:

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/networks" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "internal",
    "subnet_cidr": "10.42.10.0/24",
    "isolated": true
  }'
```

## Desplegar una VM de Playground

Si s'omet `network_ids`, el wrapper crea o reutilitza la xarxa de playground per defecte.

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

## Capturar una VM de Playground

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/vms/<vm_id>/captures" \
  -H "Content-Type: application/json" \
  -d '{"kind": "memory"}'
```

Llistar les captures de la VM:

```bash
api_curl "$BASE_URL/api/v1/playground/vms/<vm_id>/captures"
```

Fer streaming d'esdeveniments de captura:

```bash
api_curl -N "$BASE_URL/api/v1/playground/captures/<acquisition_id>/events"
```

## Aturar una sessió de Playground

```bash
api_curl -X DELETE "$BASE_URL/api/v1/playground/sessions/<session_id>"
```
