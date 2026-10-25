# Ejemplos

Estos ejemplos asumen que la API se ejecuta en `http://localhost:8000`.

## Configuración de autenticación y CSRF

AstinusLab protege los endpoints mutables de la API (POST, PUT, PATCH, DELETE) contra CSRF. En modo release (o con autenticación habilitada), las peticiones deben proporcionar un token Bearer válido. En modo desarrollo sin autenticación de API, las peticiones deben inicializar una cookie `astinuslab_csrf` mediante `GET /playground` y devolverla en la cabecera `X-CSRF-Token`.

Define este helper reutilizable `api_curl` en Bash antes de ejecutar los ejemplos:

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

Consulta [Autenticación](../api/authentication.md) para más detalles sobre tokens y seguridad.

## Listar datos del catálogo

```bash
api_curl "$BASE_URL/api/v1/targets"
api_curl "$BASE_URL/api/v1/scenarios"
api_curl "$BASE_URL/api/v1/workloads"
api_curl "$BASE_URL/api/v1/cves"
```

## Crear un Job de captura inmediata

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

Usa esto solo después de que un job manual alcance `waiting_for_capture`:

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs/<job_id>/capture" \
  -H "Content-Type: application/json" \
  -d '{
    "capture_type": "both",
    "exploit_tags": ["manual-lab"],
    "exploit_notes": "Captured after operator activity"
  }'
```

## Descargar artefactos

```bash
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/artifact"
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/disk-artifact"
```

La respuesta incluye las cabeceras `X-Astinuslab-SHA256` y `X-Astinuslab-Size` cuando hay metadatos de checksum y tamaño disponibles.

## Crear una sesión de Playground

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/sessions"
```

## Crear una red de Playground

El endpoint de crear-o-reutilizar crea una sesión si hace falta:

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

Si se omite `network_ids`, el wrapper crea o reutiliza la red de playground por defecto.

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

Listar las capturas de la VM:

```bash
api_curl "$BASE_URL/api/v1/playground/vms/<vm_id>/captures"
```

Hacer streaming de eventos de captura:

```bash
api_curl -N "$BASE_URL/api/v1/playground/captures/<acquisition_id>/events"
```

## Detener una sesión de Playground

```bash
api_curl -X DELETE "$BASE_URL/api/v1/playground/sessions/<session_id>"
```
