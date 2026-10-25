# Referència de l'API de Playground

> **Interna però amb contracte estable. El frontend depèn d'aquesta superfície.**

El Playground exposa una API REST (v1) per a sessions de lab interactives: VMs ad-hoc, xarxes multi-segment, captures sota demanda i mètriques en viu. Totes les rutes estan muntades sota el tag `playground`.

**URL base**: `http://<host>:8000/api/v1/playground` per a rutes acotades al Playground. Les rutes de paquets d'escenari estan muntades a `http://<host>:8000/api/v1/scenario-packages`.

Els cossos de petició i resposta referencien schemas Pydantic definits a `src/astinuslab/api/schemas.py` (p. ex. `PlaygroundSessionOut`, `PlaygroundVMOut`). Els models de resposta locals declarats a `src/astinuslab/playground/serializers.py` i `src/astinuslab/playground/routes/*.py` (`PlaygroundAcceptedOut`, `PlaygroundCaptureOut`, `PlaygroundAcquisitionOut`, `PlaygroundCreateIfNeededSpawnVMRequest`) es documenten inline.

Les rutes que encuen feina de Celery retornen HTTP `202 Accepted`. Els conflictes de sessió única i de nomenclatura retornen `409 Conflict`. Les sessions inexistents o no actives retornen `404 Not Found`. Les fallades d'encuament del broker es manifesten com a `503 Service Unavailable` amb detall `operation could not be queued`.

## Camps identificadors de sessió

Les respostes de Playground ara exposen `session_id` com a identificador canònic de la sessió de playground. El `job_id` legacy continua present amb el mateix valor, està marcat com a deprecat a OpenAPI i s'eliminarà en la pròxima versió menor. Els nous clients frontend/API han de llegir `session_id` i només caure a `job_id` per a servidors antics.

---

## 1. Sessions

Una sessió de playground és de slot únic: només una sessió pot estar activa alhora, i no pot coexistir amb un job forense actiu.

### Crear sessió
`POST /sessions` -> `202 Accepted`
Crea una nova sessió de playground buida.
**Resposta**: `PlaygroundSessionOut`.
**Errors**: `409` si ja hi ha una sessió de playground activa, o si hi ha un job forense actiu actualment.

### Obtenir sessió actual
`GET /sessions/current` -> `200 OK`
Retorna la sessió activa actual, incloent-hi totes les VMs i xarxes.
**Resposta**: `PlaygroundSessionOut`.
**Errors**: `404` si no hi ha sessió de playground activa.

### Obtenir sessió actual o null
`GET /sessions/current-or-null` -> `200 OK`
Retorna la sessió activa actual, incloent-hi totes les VMs i xarxes, o JSON `null` quan no existeix cap sessió activa. El frontend ho fa servir per al polling normal sense tractar un Playground buit com un error.
**Resposta**: `PlaygroundSessionOut` o `null`.

### Obtenir la fallada de destrucció més recent
`GET /sessions/recent-failure` -> `200 OK`
Retorna la sessió de Playground completada més recentment el desmuntatge de la qual ha fallat, o JSON `null` quan no s'ha registrat cap desmuntatge fallit. El frontend ho fa servir per mostrar un avís de neteja no bloquejant després que acabi una sessió.
**Resposta**: `PlaygroundFailureOut` o `null`. Un objecte de fallada conté `session_id`, `status` (`destroy_failed`) i els camps opcionals `error_summary` i `finished_at`.

### Destruir sessió
`DELETE /sessions/{job_id}` -> `202 Accepted`
Marca la sessió com a `destroy_requested` i encua el desmuntatge de totes les VMs i xarxes.
**Resposta**: `PlaygroundAcceptedOut`.
**Errors**: `404` si la sessió no es troba o no està activa.

---

## 2. Xarxes

Les xarxes són segments basats en libvirt acotats a una sessió. Cada xarxa té un nom únic dins de la seva sessió, un CIDR i un flag d'aïllament.

### Suggerir CIDR
`GET /networks/suggest-cidr?job_id=<session-id>&extra=10.42.2.0/24` -> `200 OK`
**Resposta**: `PlaygroundCidrSuggestionOut` (`subnet_cidr`).
Retorna el següent CIDR `10.42.x.0/24` lliure. `job_id` és opcional i reserva les xarxes existents d'aquella sessió. `extra` és opcional i repetible; fes-lo servir per reservar CIDRs esborrany de l'assistent que encara no estan persistits.
**Errors**: `409` si el pool de CIDR disponible està esgotat.

### Crear xarxa (sense fricció, crear-o-reutilitzar sessió)
`POST /networks` -> `202 Accepted`
**Payload**: `PlaygroundCreateNetworkRequest` (`name`, `subnet_cidr`, `isolated`).
**Resposta**: `PlaygroundSessionOut` — l'estat complet de la sessió després de registrar la xarxa com a pendent.

Reutilitza la sessió de playground activa si existeix, en cas contrari crea primer una nova sessió. La xarxa es registra en estat `pending` abans d'encuar la tasca de Celery `create_network`, així que el frontend pot sortir de la vista "crear primera xarxa" en un sol round trip i sondejar l'estat de la sessió per a l'activació. Això difereix de la variant acotada a sessió de sota, que requereix que la sessió ja existeixi.

**Errors**:
- `409` si hi ha un job forense actiu, si el nom de la xarxa ja existeix a la sessió, o si es detecta un conflicte de CIDR.
- `503` si l'encuament del broker falla després de registrar la xarxa (la xarxa es marca com a `failed`, i qualsevol sessió creada en aquesta crida es desmunta).

### Crear xarxa (acotada a sessió)
`POST /sessions/{job_id}/networks` -> `202 Accepted`
**Payload**: `PlaygroundCreateNetworkRequest`.
**Resposta**: `PlaygroundNetworkOut`.
Requereix que la sessió objectiu sigui la sessió activa. Registra una xarxa pendent i encua la tasca de creació.
**Errors**: `404` si la sessió no està activa; `409` per col·lisions de nom, conflictes de CIDR o jobs forenses actius; `503` davant fallada del broker.

### Eliminar xarxa
`DELETE /networks/{network_id}` -> `202 Accepted`
Marca la xarxa com a `destroying` i encua el desmuntatge.
**Resposta**: `PlaygroundAcceptedOut`.
**Errors**: `404` si la xarxa no es troba a la sessió activa; `409` si alguna VM encara hi està connectada.

---

## 3. VMs

Les VMs s'arrenquen des d'imatges del catàleg i es connecten a una o més xarxes via NICs. Les etiquetes han de ser úniques dins d'una sessió.

### Desplegar VM (wrapper crear-si-cal)
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

`role` és `"target"` o `"attacker"` (per defecte `"target"`). `network_ids` és opcional: quan s'omet sense `new_networks`, el wrapper assegura que existeixi una xarxa aïllada per defecte (`name="default"`, `subnet_cidr="10.42.1.0/24"`) i hi connecta la VM. `new_networks` és opcional i conté objectes `PlaygroundCreateNetworkRequest` inline; cada xarxa inline es crea primer, després es connecta a la VM juntament amb qualsevol `network_ids` existent. Quan es proporciona `network_ids`, cada xarxa referenciada ha d'existir ja a la sessió activa i estar en estat `active`.

**Resposta**: `PlaygroundVMOut`.

Crea una sessió de playground si cap està activa, assegura que les xarxes sol·licitades (o una xarxa per defecte) siguin presents, després delega a l'endpoint de spawn acotat a sessió. Qualsevol sessió creada per aquesta crida es desmunta davant fallada.

**Errors**:
- `400` si una xarxa o imatge referenciada és desconeguda, o falla la validació del workload.
- `404` si falta una xarxa referenciada.
- `409` si l'etiqueta ja està en ús, la xarxa no és utilitzable, o es viola la unicitat de sessió/xarxa/VM.
- `500` davant fallada interna (qualsevol sessió creada en aquesta crida es desmunta).
- `503` si l'encuament del broker falla.

### Desplegar VM (acotada a sessió)
`POST /sessions/{job_id}/vms` -> `202 Accepted`
**Payload**: `PlaygroundSpawnVMRequest` (`label`, `image_id`, `nics`, opcional `vcpus`/`memory_mb`/`disk_gb`, `role`, opcional `workload_id`).
**Resposta**: `PlaygroundVMOut`.
Registra una VM pendent amb una NIC per entrada a `nics` i encua la tasca de spawn.
**Errors**: `400` per xarxes desconegudes o fallades de validació del workload; `404` si la sessió no està activa; `409` per col·lisions d'etiqueta o estat de xarxa no utilitzable; `503` si l'encuament del broker falla (la VM es marca com a `failed`).

### Destruir VM
`DELETE /vms/{vm_id}` -> `202 Accepted`
Marca la VM com a `destroying` i encua el desmuntatge.
**Resposta**: `PlaygroundAcceptedOut`.
**Errors**: `404` si la VM no es troba a la sessió activa; `503` si l'encuament del broker falla (l'estat de la VM es reverteix).

### Acció d'energia de VM
`POST /vms/{vm_id}/actions` -> `202 Accepted`
**Payload**: `PlaygroundVMActionRequest` (`action`: `"start"`, `"stop"` o `"reboot"`).
**Resposta**: `PlaygroundAcceptedOut`.
Executa l'acció de libvirt corresponent contra el domini de la VM. `stop` es mapeja a `virsh shutdown`; `start` i `reboot` fan servir les accions virsh corresponents. L'estat de la VM s'actualitza de manera optimista després de l'èxit de l'ordre.
**Errors**: `404` si la VM no es troba a la sessió activa; `409` si l'acció sol·licitada no està disponible per a l'estat actual de la VM/sessió; `500` si l'ordre de libvirt falla.

---

## 4. NICs

L'attach i detach de NICs operen sobre la sessió activa.

### Connectar NIC
`POST /sessions/current/vms/{vm_id}/nics` -> `202 Accepted`
**Payload**: `PlaygroundAttachNicRequest` (`network_id`).
**Resposta**: `PlaygroundAcceptedOut`.
**Errors**: `404` si no hi ha sessió activa, VM o xarxa; `503` si l'encuament del broker falla.

### Desconnectar NIC
`DELETE /sessions/current/vms/{vm_id}/nics/{nic_id}?force=false` -> `202 Accepted`
**Resposta**: `PlaygroundAcceptedOut`.
**Errors**: `404` si no hi ha sessió activa, VM o NIC; `503` si l'encuament del broker falla. Si `force=true` i l'encuament falla, la fila de NIC s'elimina localment i l'endpoint retorna `200 OK`.

---

## 5. Captures

Les captures produeixen adquisicions de memòria o disc lligades a una VM i a la sessió pare.

### Disparar captura
`POST /vms/{vm_id}/captures` -> `202 Accepted`
**Payload**: `PlaygroundCaptureRequest` (`kind`: `"memory"` o `"disk"`).
**Resposta** (model local `PlaygroundCaptureOut`):

```json
{
  "acquisition_id": "<hex>",
  "status": "queued",
  "task_id": "<celery-task-id-or-null>"
}
```

Es crea una nova fila `Acquisition` en estat `queued` i es registra un esdeveniment `queued` abans d'encuar la tasca de captura.

**Errors**: `404` si la VM no és a la sessió activa; `409` si la VM no està `running`; `503` si l'encuament del broker falla (l'adquisició es marca com a `failed` i es registra un esdeveniment `failed`).

### Llistar captures
`GET /vms/{vm_id}/captures` -> `200 OK`
**Resposta**: `list[PlaygroundAcquisitionOut]` (model local). Cada entrada:

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

Ordenades de més recent primer.

### Streaming d'esdeveniments de captura (SSE)
`GET /captures/{acq_id}/events` -> `200 OK` (`text/event-stream`)
Flux de Server-Sent Events de les files `AcquisitionEvent` per a l'adquisició donada.

**Errors**: `404` si l'adquisició no es troba.

Cada esdeveniment s'emet en format SSE estàndard:

```
event: <status>
data: {"id":<int>,"status":"<status>","message":"<text>","created_at":"<iso8601>"}

```

**Tipus d'esdeveniment**:

| Esdeveniment | Significat |
| --- | --- |
| `queued` | La captura s'ha registrat i la tasca de Celery està encuada. |
| `running` | La tasca de captura ha començat a executar-se. |
| `completed` | La captura ha acabat amb èxit (terminal). |
| `failed` | La captura ha fallat (terminal). |
| `stream_timeout` | S'ha assolit el deadline del flux del costat del servidor. S'emet amb `data: {"status":"stream_timeout","message":"server closed stream"}` abans de tancar la connexió. |

El flux es tanca quan l'adquisició arriba a qualsevol estat terminal (`succeeded`, `completed`, `failed`, `cancelled`, `error`), quan el client es desconnecta, o quan transcorre el timeout del costat del servidor (`playground_capture_stream_timeout_seconds`).

---

## 6. Catàleg

Els endpoints de catàleg exposen les imatges i workloads que es poden fer servir al playground.

### Llistar imatges de Playground
`GET /catalog/images?role=target` -> `200 OK`
Retorna les imatges base elegibles per al playground per al `role` sol·licitat (per defecte `"target"`).
**Resposta**: `list[dict]` de `catalog.list_playground_images_for_role`.
**Errors**: `400` si el rol no és vàlid.

### Llistar workloads per a una imatge
`GET /catalog/workloads?image_id=<id>` -> `200 OK`
Retorna els workloads compatibles amb la imatge donada, anotats amb l'estat de compatibilitat.
**Resposta**: `list[PlaygroundWorkloadCatalogOut]`.
**Errors**: `404` si la imatge no es troba.

---

## 7. Mètriques

Les mètriques es mostregen per VM via `playground_metrics`. Són best-effort i retornen `503` si el mostreig de libvirt no està disponible.

### Obtenir mètriques de VM
`GET /vms/{vm_id}/metrics` -> `200 OK`
**Resposta**: `PlaygroundVMMetricsOut` (`vm_id`, `state`, `cpu_pct`, `mem_pct`, `mem_used_mb`, `mem_total_mb`, `rx_bytes`, `tx_bytes`, `sampled_at`).
**Errors**: `404` si la VM no és a la sessió activa; `503` si el mostrejador falla.

### Mètriques de sessió per lots
`GET /sessions/current/metrics` -> `200 OK`
Mostreja cada VM `running` de la sessió activa.
**Resposta**: `dict[str, PlaygroundVMMetricsOut]` indexat per `vm_id`. Les VMs el mostreig de les quals falla s'ometen (la fallada es registra però no fa fallar la petició).
**Errors**: `404` si no hi ha sessió de playground activa.

---

## 8. Topologia

Les posicions de layout de topologia es persisteixen per node per a la sessió activa. El frontend fa debounce dels desats per node al costat del client; el servidor emmagatzema una fila per node.

### Obtenir layout de topologia
`GET /sessions/current/topology-layout` -> `200 OK`
**Resposta**: `PlaygroundTopologyLayout` (`nodes: list[PlaygroundTopologyNode]`).
**Errors**: `404` si no hi ha sessió de playground activa.

### Desar node de topologia
`PATCH /sessions/current/topology-layout/{node_id}` -> `200 OK`
**Payload**: `PlaygroundTopologyNode` (`node_id`, `node_type` de `"vm"` o `"network"`, `x`, `y`). El `node_id` del cos ha de coincidir amb el path.
**Resposta**: `PlaygroundTopologyNode`.
**Errors**: `400` si el `node_id` del cos no coincideix amb el path; `404` si no hi ha sessió de playground activa.

---

## 9. Receptes

Les receptes són documents JSON que fan servir `PlaygroundRecipe` (`schema_version: 1`). Capturen xarxes, VMs i posicions de topologia per a labs de Playground repetibles. Les entrades de VM poden incloure `vcpus`, `memory_mb` i `disk_gb`; la importació els passa al mateix camí de spawn que la creació directa de VMs.

### Exportar recepta actual
`GET /sessions/current/recipe` -> `200 OK`
**Resposta**: `PlaygroundRecipe`.
Exporta la sessió activa. Els IDs de xarxa i de VM es tradueixen a noms de recepta estables: els nodes de xarxa fan servir noms de xarxa, els nodes de VM fan servir etiquetes de VM.
**Errors**: `404` si no hi ha sessió de playground activa.

### Importar recepta
`POST /recipes/import` -> `202 Accepted`
**Payload**: `PlaygroundRecipe`.
**Resposta**: `PlaygroundSessionOut`.
Valida la forma de la recepta, crea una sessió si cal, crea xarxes, desplega VMs i emmagatzema les posicions de topologia. Les entrades `networks` de les VMs de la recepta referencien noms de xarxa de la recepta, no IDs de base de dades.
**Errors**: `400` per referències de recepta no vàlides o errors de validació de schema; `409` per col·lisions de nom amb xarxes o etiquetes de VM existents, o si la recepta supera la capacitat de VMs (`code: vm_capacity`) abans de crear xarxes; també es poden manifestar errors específics de creació/spawn de la creació de xarxa o VM.

---

## 10. Paquets d'escenari

Els paquets d'escenari són arxius zip per compartir amb un clic. Contenen:

- `metadata.yaml` — metadades del paquet amb `schema_version: 1` i `package_type: astinuslab.playground.scenario`.
- `scenario.yaml` — dades de recepta de Playground.
- Entrades opcionals de scripts relatives i segures per a futurs fluxos de paquets de catàleg/workload.

Aquestes rutes estan muntades sota `/api/v1/scenario-packages`, no sota `/api/v1/playground`.

### Exportar paquet actual de Playground
`GET /api/v1/scenario-packages/playground/current` -> `200 OK` (`application/zip`)

Exporta la sessió activa de Playground com a paquet zip. La resposta fa servir `Content-Disposition: attachment` amb un nom de fitxer `.zip`.

**Errors**: `404` si no hi ha sessió de playground activa.

### Importar paquet de Playground
`POST /api/v1/scenario-packages/import` -> `202 Accepted`

**Payload**: `multipart/form-data` amb:

- `file` — zip de paquet d'escenari.
- `resource_overrides` — objecte JSON opcional indexat per etiqueta de VM. Els camps permesos per VM són `vcpus`, `memory_mb` i `disk_gb`.

**Resposta**: `PlaygroundSessionOut`.

La importació valida l'arxiu, rebutja rutes insegures, parseja `scenario.yaml`, aplica sobreescriptures de recursos, valida el resultat com a `PlaygroundRecipe` i després crea xarxes i VMs mitjançant el flux normal d'importació de receptes. Els scripts empaquetats es validen/llisten però aquest endpoint no els instal·la al catàleg local.

**Errors**: `400` per zips mal formats, rutes insegures, YAML invàlid, sobreescriptures invàlides o schemas de recepta invàlids; `409` per col·lisions de nom amb xarxes o etiquetes de VM existents.

---

## Models de resposta locals

Aquests models es declaren dins de `src/astinuslab/playground/serializers.py` i els fan servir només les rutes del playground:

### `PlaygroundAcceptedOut`
```json
{
  "status": "accepted",
  "resource_id": "<id or null>",
  "job_id": "<job_id or null>",
  "task_id": "<celery-task-id or null>"
}
```
Retornat per tots els endpoints `202 Accepted` que no retornen un snapshot de recurs.

### `PlaygroundCreateIfNeededSpawnVMRequest`
El schema de petició per a `POST /vms`. Vegeu **Desplegar VM (wrapper crear-si-cal)** a dalt per a la llista completa de camps.

### `PlaygroundCaptureOut`
Retornat per `POST /vms/{vm_id}/captures`. Vegeu **Disparar captura** a dalt.

### `PlaygroundAcquisitionOut`
Retornat per `GET /vms/{vm_id}/captures`. Vegeu **Llistar captures** a dalt.
