# Endpoints

Esta lista está verificada contra `src/astinuslab/api/routes.py`, `src/astinuslab/playground/api.py`, `src/astinuslab/playground/routes/*.py` y `src/astinuslab/main.py`.

## Health

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/health` | Check de liveness del proceso de API, sin dependencias. |
| `GET` | `/ready` | Check de readiness de dependencias; devuelve 503 solo si fallan la base de datos o el storage escribible. |
| `GET` | `/api/v1/readiness` | Ejecutar comprobaciones autenticadas profundas de dependencias y permisos. |
| `GET` | `/api/v1/metrics` | Métricas de actividad de solo lectura autenticadas sobre una ventana temporal. |

### Sondas operativas

- `GET /health` es el check de liveness del proceso. No comprueba dependencias y devuelve `200` mientras el proceso de API pueda atender peticiones.
- `GET /ready` trata la base de datos y el storage escribible como dependencias críticas. Los fallos de workers o libvirt devuelven `200` con `status: degraded`; los fallos de base de datos o storage devuelven `503` con `status: not_ready`.

Ambos endpoints son públicos y devuelven una cabecera `X-Request-ID`. Las respuestas solo exponen estados generales de los componentes; usa ese identificador para encontrar los detalles de diagnóstico en los logs del backend.

### Métricas de actividad

`GET /api/v1/metrics` es el resumen autenticado de solo lectura de la actividad reciente, agregado desde las filas existentes de Job y HostAction. Mientras `/ready` responde si la plataforma puede trabajar, este responde cómo ha estado trabajando.

`window_hours` (por defecto `24`, rango 1–2160) selecciona la ventana. La respuesta contiene `generated_at`, `window_hours`, una lista `actions` (por tipo de acción: `completed`, `failed`, `p50_seconds`, `p95_seconds`, `max_seconds`), un mapa `jobs` con recuentos por estado y un bloque `queue` (`queued`, `running`, `oldest_queued_age_seconds`, `max_dispatch_attempts`). Los percentiles son de rango más cercano sobre acciones completadas únicamente. El bloque `queue` no se limita a la ventana. Las mismas cifras aparecen en la página de Ajustes.

### Readiness profunda

`GET /api/v1/readiness` es la comprobación autenticada del operador que debe ejecutarse antes de iniciar Jobs. Devuelve `200` cuando todas las comprobaciones aplicables han pasado o son `not_applicable`, y `503` cuando ha fallado al menos una. El conjunto completo de sondeos tiene un plazo de respuesta de 10 segundos; si vence, devuelve la comprobación estable `deep_readiness_timeout` en lugar de dejar la petición bloqueada.

La respuesta contiene `ready`, el `status` global y una lista ordenada `checks`. Cada comprobación tiene un `name` estable y un `code` legible por máquina, un `status` `passed`, `failed` o `not_applicable`, un `detail` seguro para el operador y las identidades de servicio `applies_to` (`api`, `runner` o `analysis`). Las comprobaciones cubren la transacción de escritura de la base de datos, el broker Celery, heartbeats separados del runner y del análisis, la conexión libvirt, la escritura y el guardrail de espacio libre del almacenamiento, la política de raíces privilegiadas, la propiedad/modo de los helpers, la no escritura del código de la aplicación, las herramientas de análisis y build ISF de Volatility y el estado del modo fake.

| `name` de la comprobación | Valores `code` estables no preparados | `applies_to` |
| --- | --- | --- |
| `deep_readiness_deadline` | `deep_readiness_timeout` | `api`, `runner`, `analysis` |
| `database_transaction` | `database_transaction_failed` | `api`, `runner`, `analysis` |
| `celery_broker` | `celery_broker_unavailable` | `api`, `runner`, `analysis` |
| `runner_heartbeat` | `runner_heartbeat_missing`, `remote_control_unsupported` | `runner` |
| `analysis_heartbeat` | `analysis_heartbeat_missing`, `remote_control_unsupported` | `analysis` |
| `privileged_helper_sudo_runner` | `privileged_helper_sudo_unavailable`, `runner_readiness_probe_in_progress`, `runner_readiness_report_invalid`, `runner_report_unavailable`, `remote_control_unsupported` | `runner` |
| `libvirt_connection` | `libvirt_connection_unavailable`, `fake_libvirt_active` | `runner` |
| `storage_write` | `storage_not_writable` | `api`, `runner`, `analysis` |
| `storage_free_space` | `storage_free_space_unavailable`, `storage_free_space_below_guardrail`, `storage_guardrail_unavailable` | `runner`, `analysis` |
| `root_policy` | `acl_policy_storage_root_mismatch`, `fake_libvirt_active` | `runner` |
| `privileged_helper_integrity` | `privileged_helper_missing`, `privileged_helper_invalid_type`, `privileged_helper_owner_mismatch`, `privileged_helper_mode_mismatch`, `fake_libvirt_active` | `runner` |
| `privileged_helper_sudo_api` | `not_applicable` | `api` |
| `source_non_writability` | `application_source_missing`, `application_source_writable`, `application_source_unreadable` | `api`, `runner`, `analysis` |
| `volatility_analysis`, `volatility_isf_build` | Códigos de readiness de Volatility descritos más abajo, o `feature_disabled` | `runner`, `analysis` / `runner` |
| `fake_mode` | `fake_mode_active` | `api`, `runner`, `analysis` |

El sudo de los helpers privilegiados está delimitado deliberadamente por servicio. El servicio API se ejecuta con `NoNewPrivileges=true`, por lo que su comprobación sudo siempre es `not_applicable` y el proceso API nunca ejecuta `sudo -n`. La cobertura sudo del runner se evalúa dentro del runner y se expone mediante su respuesta de readiness propia; si no llega ninguna respuesta del runner, falla el heartbeat mientras la comprobación sudo sigue siendo `not_applicable`. La falta de cobertura sudo tampoco es un aviso para el worker de análisis.

Los latidos de los procesos de trabajo tienen el mismo alcance de transporte. El control remoto de Celery es un intercambio de difusión y el transporte predeterminado del broker SQLite no implementa la difusión, por lo que nunca puede llegar ninguna respuesta. En un broker así, `runner_heartbeat`, `analysis_heartbeat` y `privileged_helper_sudo_runner` informan `not_applicable` con `remote_control_unsupported` en lugar de un fallo falso permanente; la vivacidad de los procesos se demuestra con el gestor de servicios y con idas y vueltas reales de la cola.

## Web UI

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/volatility` | Abrir el workbench de Volatility 3 para preparación de símbolos, builds ISF, diagnósticos e historial de builds. |
| `POST` | `/volatility/build` | Crear un job solo-ISF de build de símbolos Linux desde el workbench de Volatility. Internamente usa la misma cola de jobs y contrato `build_vol3_isf` que la API REST. |

## API JSON de Volatility

API canónica del workbench de Volatility 3, versionada bajo `/api/v1/volatility`. Las rutas antiguas `/volatility/api/*` son alias deprecados que emiten `Deprecation: true`.

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/api/v1/volatility/readiness` | Informar si las herramientas Volatility 3 y `dwarf2json` suministradas por el operador son usables. |
| `GET` | `/api/v1/volatility/dumps` | Listar dumps de memoria disponibles para el workbench de Volatility. |
| `GET` | `/api/v1/volatility/catalog` | Listar plugins de Volatility soportados, opcionalmente filtrados por OS. |
| `GET` | `/api/v1/volatility/queue` | Listar ejecuciones recientes de plugins del workbench entre todos los dumps. |
| `DELETE` | `/api/v1/volatility/queue` | Limpiar la cola: eliminar los archivos de resultados de cada ejecución de plugin (se conservan filas de reportes y artefactos). |
| `POST` | `/api/v1/volatility/runs` | Encolar una ejecución bajo demanda de un solo plugin para el dump de un job. |
| `GET` | `/api/v1/volatility/runs/latest` | Devolver la ejecución completada más reciente para un `job_id`+`plugin`, o `{"status": "none"}`. |
| `GET` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Sondear una ejecución de plugin del workbench y recuperar resultados persistidos. |
| `DELETE` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Eliminar una ejecución de plugin de la cola. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Almacena un ISF proporcionado por el operador desde la fila de destino del banco de trabajo de Volatility (detallado en Carga manual de ISF más abajo). |

### Readiness de herramientas

Volatility 3 y `dwarf2json` son herramientas de host suministradas por el operador. `GET /api/v1/volatility/readiness` informa de las dos capacidades por separado, de modo que un cliente puede ofrecer análisis de memoria mientras los builds de ISF de Linux siguen no disponibles, o al revés:

```json
{
  "enabled": true,
  "analysis": {
    "ready": true,
    "code": "ready",
    "detail": "Volatility 3 is ready.",
    "version": "2.28.0",
    "untested": false
  },
  "isf_build": {
    "ready": false,
    "code": "dwarf2json_not_configured",
    "detail": "Configure [vol3].dwarf2json_path and dwarf2json_sha256 to build a Linux ISF from kernel debug symbols. Prebuilt symbol sources and manual ISF upload remain available.",
    "version": null,
    "untested": false
  }
}
```

- `enabled` es `false` cuando la integración con Volatility está desactivada; ambas capacidades informan entonces `feature_disabled` sin sondear el host.
- `code` es un valor estable legible por máquina. La lista completa está en [Errores](errors.md), que usa los mismos códigos para la respuesta `503` / `dependency_unavailable`.
- `version` lleva la versión del banner de Volatility siempre que un sondeo la haya producido, es decir, con `ready` y con `volatility_version_unsupported`. En cualquier otro caso es `null`, y siempre es `null` para `isf_build`, porque la comprobación de `dwarf2json` verifica un pin SHA-256 en lugar de sondear una versión.
- `untested` es `true` solo para una herramienta de `analysis` que está `ready` con una versión igual o superior a la versión mínima pero fuera de las versiones probadas con esta versión del producto. Siempre es `false` para `isf_build`.
- La respuesta omite deliberadamente las rutas de los ejecutables configurados, los hashes esperado y real de `dwarf2json`, los valores de entorno y la salida cruda del sondeo.

### Versión mínima y versiones no probadas

`2.28.0` es el **mínimo** documentado, no una coincidencia exacta. Las versiones se ordenan según [PEP 440](https://peps.python.org/pep-0440/), nunca como cadenas:

- Por debajo del mínimo —incluida una preversión de él, como `2.28.0rc1`, que queda por debajo de `2.28.0`— la herramienta se rechaza: `ready` es `false` con el código `volatility_version_unsupported`, y no se ejecuta ningún análisis. Una cadena de versión que no se puede interpretar como versión se rechaza igual, porque no se puede comparar con el mínimo.
- Una de las versiones probadas: `ready` es `true`, `untested` es `false`, y nada cambia.
- Igual o superior al mínimo pero sin ser una versión probada: el análisis se ejecuta igualmente, y la respuesta lo marca para que un cliente pueda avisar al operador de que los resultados no están validados:

```json
{
  "ready": true,
  "code": "ready",
  "detail": "Volatility 2.29.0 is newer than the versions tested with this release; analysis will run, but results have not been validated against this version.",
  "version": "2.29.0",
  "untested": true
}
```

La comprobación se realiza de nuevo en cada llamada, y una respuesta de readiness nunca se trata como autorización de ejecución: el hash de `dwarf2json` se recalcula inmediatamente antes de cada ejecución local y de cada transferencia al guest.

Como el resto de la API JSON, este endpoint requiere el token bearer de admin o la cookie de sesión del navegador cuando `auth.enabled = true`.

## Catálogo

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/api/v1/targets` | Listar imágenes de SO target. |
| `GET` | `/api/v1/scenarios` | Listar escenarios de job. |
| `GET` | `/api/v1/workloads` | Listar workloads. Soporta los parámetros de consulta `cve`, `target_id` y `compatible_only`. |
| `GET` | `/api/v1/cves` | Listar CVEs indexados e IDs de workload. |

## Paquetes de escenario

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/api/v1/scenario-packages/playground/current` | Exportar el Playground activo como zip compartible con `metadata.yaml` y `scenario.yaml`. |
| `POST` | `/api/v1/scenario-packages/import` | Importar un zip de paquete de escenario. El multipart opcional `resource_overrides` puede redimensionar VMs por etiqueta con `vcpus`, `memory_mb` y `disk_gb`. |

## Jobs

| Método | Ruta | Propósito |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | Crear un job. |
| `GET` | `/api/v1/jobs` | Listar jobs. |
| `GET` | `/api/v1/jobs/{job_id}` | Obtener los detalles de un job. |
| `DELETE` | `/api/v1/jobs/{job_id}` | Eliminar solo metadatos vacíos de un Job terminal tras confirmar su limpieza. `delete_files=true` no elimina evidencias ni evita las protecciones de custodia. |
| `GET` | `/api/v1/jobs/{job_id}/logs` | Listar los eventos de un job. |
| `GET` | `/api/v1/jobs/{job_id}/artifact` | Descargar el artefacto de memoria. |
| `GET` | `/api/v1/jobs/{job_id}/disk-artifact` | Descargar el artefacto de disco. |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Solicitar la cancelación. |
| `POST` | `/api/v1/jobs/{job_id}/capture` | Disparar captura manual de memoria, disco o ambos. |
| `POST` | `/api/v1/jobs/{job_id}/acquire-memory-dump` | Alias de la captura manual. |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Crear un nuevo job a partir de la configuración de un job fallido. |
| `POST` | `/api/v1/jobs/{job_id}/force-fail` | Forzar un job al estado fallido y matar los subprocesos activos. |
| `GET` | `/api/v1/jobs/{job_id}/lab-info` | Obtener URLs de workload, credenciales y metadatos de lab. |
| `POST` | `/api/v1/jobs/{job_id}/reset-lab` | Solicitar la restauración desde el snapshot limpio del lab. |
| `GET` | `/api/v1/jobs/{job_id}/runtimes` | Listar los registros de runtime por VM. |
| `POST` | `/api/v1/jobs/{job_id}/runtimes/{role}/{action}` | Solicitar una acción de runtime como `reboot` o `shutdown`. |

La eliminación devuelve `204` cuando el Job terminal ya no conserva evidencias y su limpieza está confirmada. Devuelve `404` si no existe; `409 conflict` si no es terminal; `409 evidence_retained` si conserva evidencias o historial de custodia; y `409 cleanup_pending` si siguen pendientes recursos. Elimina las evidencias elegibles por **Storage**, respetando sus protecciones, antes de reintentar. El parámetro `delete_files` no cambia esta secuencia.

## Adquisiciones y acciones de host

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/api/v1/acquisitions` | Listar adquisiciones. |
| `POST` | `/api/v1/acquisitions` | Crear un registro de adquisición. |
| `GET` | `/api/v1/acquisitions/{acquisition_id}` | Obtener los detalles de una adquisición. |
| `PATCH` | `/api/v1/acquisitions/{acquisition_id}/status` | Actualizar el estado de la adquisición a `live`, `paused` o `sealed`. |
| `GET` | `/api/v1/cases` | Listar casos. |
| `POST` | `/api/v1/cases` | Crear un caso. |
| `GET` | `/api/v1/cases/{case_id}` | Obtener los detalles de un caso. |
| `PATCH` | `/api/v1/cases/{case_id}/status` | Actualizar el estado del caso a `open`, `closed` o `sealed`. |
| `GET` | `/api/v1/artifacts` | Listar artefactos. Soporta los parámetros de consulta `case_id`, `job_id` y `acquisition_id`. |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Obtener identidad, checksum y procedencia; usar los endpoints de descarga para sidecars. |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | Descargar el archivo del artefacto por su ID. |
| `GET` | `/api/v1/artifacts/{artifact_id}/manifest` | Descargar el sidecar de manifiesto del artefacto. |
| `GET` | `/api/v1/artifacts/{artifact_id}/sha256` | Descargar el sidecar SHA-256 del artefacto. |
| `GET` | `/api/v1/reports` | Listar informes. Soporta los parámetros de consulta `case_id`, `job_id`, `acquisition_id`, `input_artifact_id` y `output_artifact_id`. |
| `GET` | `/api/v1/reports/{report_id}` | Obtener los metadatos de un informe. |
| `GET` | `/api/v1/host-actions` | Listar los registros de acciones de host. |
| `POST` | `/api/v1/run-retention` | Disparar la limpieza de retención. |

## Sesiones y redes del Playground

| Método | Ruta | Propósito |
| --- | --- | --- |
| `POST` | `/api/v1/playground/sessions` | Crear una sesión de playground. |
| `GET` | `/api/v1/playground/sessions/current` | Obtener la sesión de playground activa. |
| `GET` | `/api/v1/playground/sessions/current-or-null` | Obtener la sesión de playground activa, o `null` cuando no hay ninguna, sin devolver un 404. |
| `GET` | `/api/v1/playground/sessions/recent-failure` | Obtener el desmontaje fallido más reciente del Playground, o `null` cuando no hay ninguno registrado. |
| `DELETE` | `/api/v1/playground/sessions/{job_id}` | Destruir la sesión de playground activa. |
| `POST` | `/api/v1/playground/networks` | Crear una red, creando una sesión si hace falta. |
| `GET` | `/api/v1/playground/networks/suggest-cidr` | Sugerir el siguiente CIDR de playground disponible. Los parámetros opcionales `job_id` y `extra` repetido reservan CIDRs existentes o propuestos. |
| `POST` | `/api/v1/playground/sessions/{job_id}/networks` | Crear una red en una sesión activa existente. |
| `DELETE` | `/api/v1/playground/networks/{network_id}` | Eliminar una red que no tiene VMs conectadas. |
| `GET` | `/api/v1/playground/sessions/current/topology-layout` | Obtener las posiciones guardadas de los nodos de topología. |
| `PATCH` | `/api/v1/playground/sessions/current/topology-layout/{node_id}` | Guardar la posición de un nodo de topología. |
| `GET` | `/api/v1/playground/sessions/current/recipe` | Exportar la sesión activa como una receta JSON de playground. |
| `POST` | `/api/v1/playground/recipes/import` | Importar una receta JSON de playground en la sesión activa, creando una si hace falta. |

## VMs, NICs, capturas y métricas del Playground

| Método | Ruta | Propósito |
| --- | --- | --- |
| `POST` | `/api/v1/playground/vms` | Desplegar una VM, creando una sesión y una red por defecto si hace falta. |
| `POST` | `/api/v1/playground/sessions/{job_id}/vms` | Desplegar una VM en una sesión activa existente. |
| `DELETE` | `/api/v1/playground/vms/{vm_id}` | Destruir una VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/actions` | Solicitar una acción de energía de VM: `start`, `stop` o `reboot`. |
| `POST` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics` | Conectar una NIC a una VM. |
| `DELETE` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}` | Desconectar una NIC de una VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/captures` | Disparar captura de memoria o disco de una VM en ejecución. |
| `GET` | `/api/v1/playground/vms/{vm_id}/captures` | Listar las capturas de una VM. |
| `GET` | `/api/v1/playground/captures/{acq_id}/events` | Hacer streaming de eventos de captura como Server-Sent Events. |
| `GET` | `/api/v1/playground/vms/{vm_id}/metrics` | Obtener métricas en vivo de una VM. |
| `GET` | `/api/v1/playground/sessions/current/metrics` | Obtener métricas en vivo de las VMs en ejecución de la sesión actual. |

## Catálogo del Playground

| Método | Ruta | Propósito |
| --- | --- | --- |
| `GET` | `/api/v1/playground/catalog/images` | Listar las imágenes elegibles para el playground. El parámetro opcional `role` por defecto es `target`. |
| `GET` | `/api/v1/playground/catalog/workloads` | Listar los workloads compatibles con `image_id`. |

## Overrides avanzados

Vías de escape para operadores que suministran artefactos manualmente (registrados con procedencia no verificada). Requieren el token bearer de admin cuando la autenticación está activada.

| Método | Ruta | Propósito |
| --- | --- | --- |
| `POST` | `/api/v1/jobs/{job_id}/isf/upload` | Almacenar un ISF suministrado por el operador para un job, omitiendo el enforcement de GNU build-id. |
| `POST` | `/api/v1/jobs/{job_id}/dump/upload` | Almacenar un dump de memoria suministrado por el operador para un job, omitiendo la validación de completitud ELF. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Almacenar un ISF suministrado por el operador desde la fila de target del workbench de Volatility. |

## Ejemplos de petición

Crear un job:

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "capture_type": "memory",
  "build_vol3_isf": false
}
```

Disparar una captura manual:

```json
{
  "capture_type": "both",
  "exploit_tags": ["manual-lab"],
  "exploit_notes": "Operator-triggered capture"
}
```

Crear una red de playground:

```json
{
  "name": "internal",
  "subnet_cidr": "10.42.10.0/24",
  "isolated": true
}
```

Desplegar una VM de playground:

```json
{
  "label": "target-1",
  "image_id": "ubuntu-22-04-default",
  "role": "target",
  "vcpus": 2,
  "memory_mb": 4096,
  "disk_gb": 40
}
```
