# Referencia de la API de Playground

> **Interna pero con contrato estable. El frontend depende de esta superficie.**

El Playground expone una API REST (v1) para sesiones de lab interactivas: VMs ad-hoc, redes multi-segmento, capturas bajo demanda y métricas en vivo. Todas las rutas están montadas bajo el tag `playground`.

**URL base**: `http://<host>:8000/api/v1/playground` para rutas acotadas al Playground. Las rutas de paquetes de escenario están montadas en `http://<host>:8000/api/v1/scenario-packages`.

Los cuerpos de petición y respuesta referencian schemas Pydantic definidos en `src/astinuslab/api/schemas.py` (p. ej. `PlaygroundSessionOut`, `PlaygroundVMOut`). Los modelos de respuesta locales declarados en `src/astinuslab/playground/serializers.py` y `src/astinuslab/playground/routes/*.py` (`PlaygroundAcceptedOut`, `PlaygroundCaptureOut`, `PlaygroundAcquisitionOut`, `PlaygroundCreateIfNeededSpawnVMRequest`) se documentan inline.

Las rutas que encolan trabajo de Celery devuelven HTTP `202 Accepted`. Los conflictos de sesión única y de nombrado devuelven `409 Conflict`. Las sesiones inexistentes o no activas devuelven `404 Not Found`. Los fallos de encolado del broker se manifiestan como `503 Service Unavailable` con detalle `operation could not be queued`.

## Campos identificadores de sesión

Las respuestas de Playground ahora exponen `session_id` como identificador canónico de la sesión de playground. El `job_id` legacy permanece presente con el mismo valor, está marcado como deprecado en OpenAPI y se eliminará en la próxima versión menor. Los nuevos clientes frontend/API deben leer `session_id` y solo caer a `job_id` para servidores antiguos.

---

## 1. Sesiones

Una sesión de playground es de slot único: solo una sesión puede estar activa a la vez, y no puede coexistir con un job forense activo.

### Crear sesión
`POST /sessions` -> `202 Accepted`
Crea una nueva sesión de playground vacía.
**Respuesta**: `PlaygroundSessionOut`.
**Errores**: `409` si ya hay una sesión de playground activa, o si hay un job forense activo actualmente.

### Obtener sesión actual
`GET /sessions/current` -> `200 OK`
Devuelve la sesión activa actual, incluyendo todas las VMs y redes.
**Respuesta**: `PlaygroundSessionOut`.
**Errores**: `404` si no hay sesión de playground activa.

### Obtener sesión actual o null
`GET /sessions/current-or-null` -> `200 OK`
Devuelve la sesión activa actual, incluyendo todas las VMs y redes, o JSON `null` cuando no existe ninguna sesión activa. El frontend lo usa para polling normal sin tratar un Playground vacío como error.
**Respuesta**: `PlaygroundSessionOut` o `null`.

### Obtener el fallo de destrucción más reciente
`GET /sessions/recent-failure` -> `200 OK`
Devuelve la sesión de Playground completada más recientemente cuyo desmontaje falló, o JSON `null` cuando no se ha registrado ningún desmontaje fallido. El frontend lo usa para mostrar un aviso no bloqueante después de que termine una sesión.
**Respuesta**: `PlaygroundFailureOut` o `null`. Un objeto de fallo contiene `session_id`, `status` (`destroy_failed`) y los campos opcionales `error_summary` y `finished_at`.

### Destruir sesión
`DELETE /sessions/{job_id}` -> `202 Accepted`
Marca la sesión como `destroy_requested` y encola el desmontaje de todas las VMs y redes.
**Respuesta**: `PlaygroundAcceptedOut`.
**Errores**: `404` si la sesión no se encuentra o no está activa.

---

## 2. Redes

Las redes son segmentos respaldados por libvirt acotados a una sesión. Cada red tiene un nombre único dentro de su sesión, un CIDR y un flag de aislamiento.

### Sugerir CIDR
`GET /networks/suggest-cidr?job_id=<session-id>&extra=10.42.2.0/24` -> `200 OK`
**Respuesta**: `PlaygroundCidrSuggestionOut` (`subnet_cidr`).
Devuelve el siguiente CIDR `10.42.x.0/24` libre. `job_id` es opcional y reserva las redes existentes de esa sesión. `extra` es opcional y repetible; úsalo para reservar CIDRs borrador del asistente que aún no están persistidos.
**Errores**: `409` si el pool de CIDR disponible está agotado.

### Crear red (sin fricción, crear-o-reutilizar sesión)
`POST /networks` -> `202 Accepted`
**Payload**: `PlaygroundCreateNetworkRequest` (`name`, `subnet_cidr`, `isolated`).
**Respuesta**: `PlaygroundSessionOut` — el estado completo de la sesión tras registrar la red como pendiente.

Reutiliza la sesión de playground activa si existe, de lo contrario crea una nueva sesión primero. La red se registra en estado `pending` antes de encolar la tarea de Celery `create_network`, así que el frontend puede salir de la vista "crear primera red" en un solo round trip y sondear el estado de la sesión para la activación. Esto difiere de la variante acotada a sesión de abajo, que requiere que la sesión ya exista.

**Errores**:
- `409` si hay un job forense activo, si el nombre de la red ya existe en la sesión, o si se detecta un conflicto de CIDR.
- `503` si el encolado del broker falla tras registrar la red (la red se marca como `failed`, y cualquier sesión creada en esta llamada se desmonta).

### Crear red (acotada a sesión)
`POST /sessions/{job_id}/networks` -> `202 Accepted`
**Payload**: `PlaygroundCreateNetworkRequest`.
**Respuesta**: `PlaygroundNetworkOut`.
Requiere que la sesión objetivo sea la sesión activa. Registra una red pendiente y encola la tarea de creación.
**Errores**: `404` si la sesión no está activa; `409` por colisiones de nombre, conflictos de CIDR o jobs forenses activos; `503` ante fallo del broker.

### Eliminar red
`DELETE /networks/{network_id}` -> `202 Accepted`
Marca la red como `destroying` y encola el desmontaje.
**Respuesta**: `PlaygroundAcceptedOut`.
**Errores**: `404` si la red no se encuentra en la sesión activa; `409` si alguna VM sigue conectada.

---

## 3. VMs

Las VMs se arrancan desde imágenes del catálogo y se conectan a una o más redes vía NICs. Las etiquetas deben ser únicas dentro de una sesión.

### Desplegar VM (wrapper crear-si-hace-falta)
`POST /vms` -> `202 Accepted`
**Payload**: `PlaygroundCreateIfNeededSpawnVMRequest`:

```json
{
  "label": "target-1",
  "image_id": "ubuntu-22-04-default",
  "vcpus": 2,
  "memory_mb": 4096,
  "disk_gb": 40,
  "role": "target",
  "workload_id": "wordpress-6.4",
  "network_ids": ["net-abc123"],
  "new_networks": [
    {"name": "dmz", "subnet_cidr": "10.42.2.0/24", "isolated": true}
  ]
}
```

`role` es `"target"` o `"attacker"` (por defecto `"target"`). `network_ids` es opcional: cuando se omite sin `new_networks`, el wrapper asegura que exista una red aislada por defecto (`name="default"`, `subnet_cidr="10.42.1.0/24"`) y conecta la VM a ella. `new_networks` es opcional y contiene objetos `PlaygroundCreateNetworkRequest` inline; cada red inline se crea primero, luego se conecta a la VM junto con cualquier `network_ids` existente. Cuando se proporciona `network_ids`, cada red referenciada debe existir ya en la sesión activa y estar en estado `active`.

**Respuesta**: `PlaygroundVMOut`.

Crea una sesión de playground si ninguna está activa, asegura que las redes solicitadas (o una red por defecto) estén presentes, luego delega al endpoint de spawn acotado a sesión. Cualquier sesión creada por esta llamada se desmonta ante fallo.

**Errores**:
- `400` si una red o imagen referenciada es desconocida, o falla la validación del workload.
- `404` si falta una red referenciada.
- `409` si la etiqueta ya está en uso, la red no es usable, o se viola la unicidad de sesión/red/VM.
- `500` ante fallo interno (cualquier sesión creada en esta llamada se desmonta).
- `503` si el encolado del broker falla.

### Desplegar VM (acotada a sesión)
`POST /sessions/{job_id}/vms` -> `202 Accepted`
**Payload**: `PlaygroundSpawnVMRequest` (`label`, `image_id`, `nics`, opcional `vcpus`/`memory_mb`/`disk_gb`, `role`, opcional `workload_id`).
**Respuesta**: `PlaygroundVMOut`.
Registra una VM pendiente con una NIC por entrada en `nics` y encola la tarea de spawn.
**Errores**: `400` por redes desconocidas o fallos de validación del workload; `404` si la sesión no está activa; `409` por colisiones de etiqueta o estado de red no usable; `503` si el encolado del broker falla (la VM se marca como `failed`).

### Destruir VM
`DELETE /vms/{vm_id}` -> `202 Accepted`
Marca la VM como `destroying` y encola el desmontaje.
**Respuesta**: `PlaygroundAcceptedOut`.
**Errores**: `404` si la VM no se encuentra en la sesión activa; `503` si el encolado del broker falla (el estado de la VM se revierte).

### Acción de energía de VM
`POST /vms/{vm_id}/actions` -> `202 Accepted`
**Payload**: `PlaygroundVMActionRequest` (`action`: `"start"`, `"stop"` o `"reboot"`).
**Respuesta**: `PlaygroundAcceptedOut`.
Ejecuta la acción de libvirt correspondiente contra el dominio de la VM. `stop` se mapea a `virsh shutdown`; `start` y `reboot` usan las acciones virsh correspondientes. El estado de la VM se actualiza de forma optimista tras el éxito del comando.
**Errores**: `404` si la VM no se encuentra en la sesión activa; `409` si la acción solicitada no está disponible para el estado actual de la VM/sesión; `500` si el comando de libvirt falla.

---

## 4. NICs

El attach y detach de NICs operan sobre la sesión activa.

### Conectar NIC
`POST /sessions/current/vms/{vm_id}/nics` -> `202 Accepted`
**Payload**: `PlaygroundAttachNicRequest` (`network_id`).
**Respuesta**: `PlaygroundAcceptedOut`.
**Errores**: `404` si no hay sesión activa, VM o red; `503` si el encolado del broker falla.

### Desconectar NIC
`DELETE /sessions/current/vms/{vm_id}/nics/{nic_id}?force=false` -> `202 Accepted`
**Respuesta**: `PlaygroundAcceptedOut`.
**Errores**: `404` si no hay sesión activa, VM o NIC; `503` si el encolado del broker falla. Si `force=true` y el encolado falla, la fila de NIC se elimina localmente y el endpoint devuelve `200 OK`.

---

## 5. Capturas

Las capturas producen adquisiciones de memoria o disco ligadas a una VM y a la sesión padre.

### Disparar captura
`POST /vms/{vm_id}/captures` -> `202 Accepted`
**Payload**: `PlaygroundCaptureRequest` (`kind`: `"memory"` o `"disk"`).
**Respuesta** (modelo local `PlaygroundCaptureOut`):

```json
{
  "acquisition_id": "<hex>",
  "status": "queued",
  "task_id": "<celery-task-id-or-null>"
}
```

Se crea una nueva fila `Acquisition` en estado `queued` y se registra un evento `queued` antes de encolar la tarea de captura.

**Errores**: `404` si la VM no está en la sesión activa; `409` si la VM no está `running`; `503` si el encolado del broker falla (la adquisición se marca como `failed` y se registra un evento `failed`).

### Listar capturas
`GET /vms/{vm_id}/captures` -> `200 OK`
**Respuesta**: `list[PlaygroundAcquisitionOut]` (modelo local). Cada entrada:

```json
{
  "id": "<hex>",
  "name": "playground-<vm_id>-<kind>-<timestamp>",
  "status": "queued|running|completed|succeeded|failed|cancelled|error",
  "created_at": "<iso8601>",
  "path": "<path or null>",
  "sha256": "<hex or null>",
  "size_bytes": 0,
  "kind": "memory|disk",
  "session_id": "<job_id>",
  "vm_label": "<label>"
}
```

Ordenadas de más reciente primero.

### Streaming de eventos de captura (SSE)
`GET /captures/{acq_id}/events` -> `200 OK` (`text/event-stream`)
Stream de Server-Sent Events de las filas `AcquisitionEvent` para la adquisición dada.

**Errores**: `404` si la adquisición no se encuentra.

Cada evento se emite en formato SSE estándar:

```
event: <status>
data: {"id":<int>,"status":"<status>","message":"<text>","created_at":"<iso8601>"}

```

**Tipos de evento**:

| Evento | Significado |
| --- | --- |
| `queued` | La captura se ha registrado y la tarea de Celery está encolada. |
| `running` | La tarea de captura ha empezado a ejecutarse. |
| `completed` | La captura terminó con éxito (terminal). |
| `failed` | La captura falló (terminal). |
| `stream_timeout` | Se alcanzó el deadline del stream del lado del servidor. Se emite con `data: {"status":"stream_timeout","message":"server closed stream"}` antes de cerrar la conexión. |

El stream se cierra cuando la adquisición alcanza cualquier estado terminal (`succeeded`, `completed`, `failed`, `cancelled`, `error`), cuando el cliente se desconecta, o cuando transcurre el timeout del lado del servidor (`playground_capture_stream_timeout_seconds`).

---

## 6. Catálogo

Los endpoints de catálogo exponen las imágenes y workloads que se pueden usar en el playground.

### Listar imágenes de Playground
`GET /catalog/images?role=target` -> `200 OK`
Devuelve las imágenes base elegibles para el playground para el `role` solicitado (por defecto `"target"`).
**Respuesta**: `list[dict]` de `catalog.list_playground_images_for_role`.
**Errores**: `400` si el rol no es válido.

### Listar workloads para una imagen
`GET /catalog/workloads?image_id=<id>` -> `200 OK`
Devuelve los workloads compatibles con la imagen dada, anotados con el estado de compatibilidad.
**Respuesta**: `list[PlaygroundWorkloadCatalogOut]`.
**Errores**: `404` si la imagen no se encuentra.

---

## 7. Métricas

Las métricas se muestrean por VM vía `playground_metrics`. Son best-effort y devuelven `503` si el muestreo de libvirt no está disponible.

### Obtener métricas de VM
`GET /vms/{vm_id}/metrics` -> `200 OK`
**Respuesta**: `PlaygroundVMMetricsOut` (`vm_id`, `state`, `cpu_pct`, `mem_pct`, `mem_used_mb`, `mem_total_mb`, `rx_bytes`, `tx_bytes`, `sampled_at`).
**Errores**: `404` si la VM no está en la sesión activa; `503` si el muestreador falla.

### Métricas de sesión por lotes
`GET /sessions/current/metrics` -> `200 OK`
Muestrea cada VM `running` de la sesión activa.
**Respuesta**: `dict[str, PlaygroundVMMetricsOut]` indexado por `vm_id`. Las VMs cuyo muestreo falla se omiten (el fallo se registra pero no falla la petición).
**Errores**: `404` si no hay sesión de playground activa.

---

## 8. Topología

Las posiciones de layout de topología se persisten por nodo para la sesión activa. El frontend hace debounce de los guardados por nodo en el lado del cliente; el servidor almacena una fila por nodo.

### Obtener layout de topología
`GET /sessions/current/topology-layout` -> `200 OK`
**Respuesta**: `PlaygroundTopologyLayout` (`nodes: list[PlaygroundTopologyNode]`).
**Errores**: `404` si no hay sesión de playground activa.

### Guardar nodo de topología
`PATCH /sessions/current/topology-layout/{node_id}` -> `200 OK`
**Payload**: `PlaygroundTopologyNode` (`node_id`, `node_type` de `"vm"` o `"network"`, `x`, `y`). El `node_id` del cuerpo debe coincidir con el path.
**Respuesta**: `PlaygroundTopologyNode`.
**Errores**: `400` si el `node_id` del cuerpo no coincide con el path; `404` si no hay sesión de playground activa.

---

## 9. Recetas

Las recetas son documentos JSON que usan `PlaygroundRecipe` (`schema_version: 1`). Capturan redes, VMs y posiciones de topología para labs de Playground repetibles. Las entradas de VM pueden incluir `vcpus`, `memory_mb` y `disk_gb`; la importación los pasa al mismo camino de spawn que la creación directa de VMs.

### Exportar receta actual
`GET /sessions/current/recipe` -> `200 OK`
**Respuesta**: `PlaygroundRecipe`.
Exporta la sesión activa. Los IDs de red y de VM se traducen a nombres de receta estables: los nodos de red usan nombres de red, los nodos de VM usan etiquetas de VM.
**Errores**: `404` si no hay sesión de playground activa.

### Importar receta
`POST /recipes/import` -> `202 Accepted`
**Payload**: `PlaygroundRecipe`.
**Respuesta**: `PlaygroundSessionOut`.
Valida la forma de la receta, crea una sesión si hace falta, crea redes, despliega VMs y almacena las posiciones de topología. Las entradas `networks` de las VMs de la receta referencian nombres de red de la receta, no IDs de base de datos.
**Errores**: `400` por referencias de receta inválidas o errores de validación de schema; `409` por colisiones de nombre con redes o etiquetas de VM existentes, o si la receta supera la capacidad de VMs (`code: vm_capacity`) antes de crear redes; también pueden manifestarse errores específicos de creación/spawn de la creación de red o VM.

---

## 10. Paquetes de escenario

Los paquetes de escenario son archivos zip para compartir con un clic. Contienen:

- `metadata.yaml` — metadatos del paquete con `schema_version: 1` y `package_type: astinuslab.playground.scenario`.
- `scenario.yaml` — datos de receta de Playground.
- Entradas opcionales de scripts relativas y seguras para futuros flujos de paquetes de catálogo/workload.

Estas rutas están montadas bajo `/api/v1/scenario-packages`, no bajo `/api/v1/playground`.

### Exportar paquete actual de Playground
`GET /api/v1/scenario-packages/playground/current` -> `200 OK` (`application/zip`)

Exporta la sesión activa de Playground como paquete zip. La respuesta usa `Content-Disposition: attachment` con un nombre de archivo `.zip`.

**Errores**: `404` si no hay sesión de playground activa.

### Importar paquete de Playground
`POST /api/v1/scenario-packages/import` -> `202 Accepted`

**Payload**: `multipart/form-data` con:

- `file` — zip de paquete de escenario.
- `resource_overrides` — objeto JSON opcional indexado por etiqueta de VM. Los campos permitidos por VM son `vcpus`, `memory_mb` y `disk_gb`.

**Respuesta**: `PlaygroundSessionOut`.

La importación valida el archivo, rechaza rutas inseguras, parsea `scenario.yaml`, aplica sobreescrituras de recursos, valida el resultado como `PlaygroundRecipe` y luego crea redes y VMs mediante el flujo normal de importación de recetas. Los scripts empaquetados se validan/listan pero este endpoint no los instala en el catálogo local.

**Errores**: `400` por zips malformados, rutas inseguras, YAML inválido, sobreescrituras inválidas o schemas de receta inválidos; `409` por colisiones de nombre con redes o etiquetas de VM existentes.

---

## Modelos de respuesta locales

Estos modelos se declaran dentro de `src/astinuslab/playground/serializers.py` y los usan solo las rutas del playground:

### `PlaygroundAcceptedOut`
```json
{
  "status": "accepted",
  "resource_id": "<id or null>",
  "job_id": "<job_id or null>",
  "task_id": "<celery-task-id or null>"
}
```
Devuelto por todos los endpoints `202 Accepted` que no devuelven un snapshot de recurso.

### `PlaygroundCreateIfNeededSpawnVMRequest`
El schema de petición para `POST /vms`. Consulta **Desplegar VM (wrapper crear-si-hace-falta)** arriba para la lista completa de campos.

### `PlaygroundCaptureOut`
Devuelto por `POST /vms/{vm_id}/captures`. Consulta **Disparar captura** arriba.

### `PlaygroundAcquisitionOut`
Devuelto por `GET /vms/{vm_id}/captures`. Consulta **Listar capturas** arriba.
