# Endpoints

Aquesta llista està verificada contra `src/astinuslab/api/routes.py`, `src/astinuslab/playground/api.py`, `src/astinuslab/playground/routes/*.py` i `src/astinuslab/main.py`.

## Health

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/health` | Check de liveness del procés de l'API, sense dependències. |
| `GET` | `/ready` | Check de readiness de dependències; retorna 503 només si fallen la base de dades o l'emmagatzematge amb escriptura. |
| `GET` | `/api/v1/readiness` | Executar comprovacions autenticades profundes de dependències i permisos. |
| `GET` | `/api/v1/metrics` | Mètriques d'activitat de només lectura autenticades sobre una finestra temporal. |

### Sondes operatives

- `GET /health` és el check de liveness del procés. No comprova dependències i retorna `200` mentre el procés de l'API pugui atendre peticions.
- `GET /ready` tracta la base de dades i l'emmagatzematge amb escriptura com a dependències crítiques. Els errors dels workers o de libvirt retornen `200` amb `status: degraded`; els errors de la base de dades o de l'emmagatzematge retornen `503` amb `status: not_ready`.

Tots dos endpoints són públics i retornen una capçalera `X-Request-ID`. Les respostes només exposen estats generals dels components; fes servir aquest identificador per trobar els detalls de diagnòstic als logs del backend.

### Mètriques d'activitat

`GET /api/v1/metrics` és el resum autenticat de només lectura de l'activitat recent, agregat des de les files existents de Job i HostAction. Mentre `/ready` respon si la plataforma pot treballar, aquest respon com ha estat treballant.

`window_hours` (per defecte `24`, rang 1–2160) selecciona la finestra. La resposta conté `generated_at`, `window_hours`, una llista `actions` (per tipus d'acció: `completed`, `failed`, `p50_seconds`, `p95_seconds`, `max_seconds`), un mapa `jobs` amb recomptes per estat i un bloc `queue` (`queued`, `running`, `oldest_queued_age_seconds`, `max_dispatch_attempts`). Els percentils són de rang més proper sobre accions completades únicament. El bloc `queue` no es limita a la finestra. Les mateixes xifres apareixen a la pàgina de Configuració.

### Readiness profunda

`GET /api/v1/readiness` és la comprovació autenticada de l'operador que s'ha d'executar abans d'iniciar Jobs. Retorna `200` quan totes les comprovacions aplicables han passat o són `not_applicable`, i `503` quan n'ha fallat almenys una. El conjunt complet de sondeigs té un termini de resposta de 10 segons; si venç, retorna la comprovació estable `deep_readiness_timeout` en lloc de deixar la petició bloquejada.

La resposta conté `ready`, l'`status` global i una llista ordenada `checks`. Cada comprovació té un `name` estable i un `code` llegible per màquina, un `status` `passed`, `failed` o `not_applicable`, un `detail` segur per a l'operador i les identitats de servei `applies_to` (`api`, `runner` o `analysis`). Les comprovacions cobreixen la transacció d'escriptura de la base de dades, el broker Celery, heartbeats separats del runner i de l'analysis, la connexió libvirt, l'escriptura i el guardrail d'espai lliure de l'emmagatzematge, la política d'arrels privilegiades, la propietat/mode dels helpers, la no-escriptura del codi de l'aplicació, les eines d'anàlisi i build ISF de Volatility i l'estat del mode fake.

| `name` de la comprovació | Valors `code` estables no preparats | `applies_to` |
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
| `volatility_analysis`, `volatility_isf_build` | Codis de readiness de Volatility descrits més avall, o `feature_disabled` | `runner`, `analysis` / `runner` |
| `fake_mode` | `fake_mode_active` | `api`, `runner`, `analysis` |

El sudo dels helpers privilegiats està delimitat deliberadament per servei. El servei API s'executa amb `NoNewPrivileges=true`, per tant la seva comprovació sudo sempre és `not_applicable` i el procés API mai no executa `sudo -n`. La cobertura sudo del runner s'avalua dins del runner i s'exposa mitjançant la seva resposta de readiness pròpia; si no arriba cap resposta del runner, falla el heartbeat mentre la comprovació sudo continua sent `not_applicable`. La manca de cobertura sudo tampoc és un avís per al worker d'anàlisi.

Els batecs dels processos de treball tenen el mateix abast de transport. El control remot de Celery és un intercanvi de difusió i el transport per defecte del broker SQLite no implementa la difusió, de manera que mai no pot arribar cap resposta. En un broker així, `runner_heartbeat`, `analysis_heartbeat` i `privileged_helper_sudo_runner` informen `not_applicable` amb `remote_control_unsupported` en lloc d'un error fals permanent; la vivesa dels processos es demostra amb el gestor de serveis i amb anades i tornades reals de la cua.

## UI web

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/volatility` | Obrir el workbench de Volatility 3 per a preparació de símbols, builds ISF, diagnòstics i historial de builds. |
| `POST` | `/volatility/build` | Crear un job només-ISF de build de símbols Linux des del workbench de Volatility. Internament fa servir la mateixa cua de jobs i contracte `build_vol3_isf` que l'API REST. |

## API JSON de Volatility

API canònica del workbench de Volatility 3, versionada sota `/api/v1/volatility`. Les rutes antigues `/volatility/api/*` són àlies deprecats que emeten `Deprecation: true`.

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/api/v1/volatility/readiness` | Informar si les eines Volatility 3 i `dwarf2json` subministrades per l'operador es poden fer servir. |
| `GET` | `/api/v1/volatility/dumps` | Llistar dumps de memòria disponibles per al workbench de Volatility. |
| `GET` | `/api/v1/volatility/catalog` | Llistar plugins de Volatility suportats, opcionalment filtrats per OS. |
| `GET` | `/api/v1/volatility/queue` | Llistar execucions recents de plugins del workbench entre tots els dumps. |
| `DELETE` | `/api/v1/volatility/queue` | Netejar la cua: eliminar els fitxers de resultats de cada execució de plugin (es conserven files de reportes i artefactes). |
| `POST` | `/api/v1/volatility/runs` | Encolar una execució sota demanda d'un sol plugin per al dump d'un job. |
| `GET` | `/api/v1/volatility/runs/latest` | Retornar l'execució completada més recent per a un `job_id`+`plugin`, o `{"status": "none"}`. |
| `GET` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Sondejar una execució de plugin del workbench i recuperar resultats persistits. |
| `DELETE` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Eliminar una execució de plugin de la cua. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Emmagatzema un ISF proporcionat per l'operador des de la fila d'objectiu del banc de treball de Volatility (detallat a Càrrega manual d'ISF més avall). |

### Readiness de les eines

Volatility 3 i `dwarf2json` són eines de host subministrades per l'operador. `GET /api/v1/volatility/readiness` informa de les dues capacitats per separat, de manera que un client pot oferir anàlisi de memòria mentre els builds d'ISF de Linux continuen no disponibles, o a l'inrevés:

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

- `enabled` és `false` quan la integració amb Volatility està desactivada; llavors les dues capacitats informen `feature_disabled` sense sondejar el host.
- `code` és un valor estable llegible per màquina. La llista completa és a [Errors](errors.md), que fa servir els mateixos codis per a la resposta `503` / `dependency_unavailable`.
- `version` porta la versió del banner de Volatility sempre que un sondeig n'hagi produït una, és a dir, amb `ready` i amb `volatility_version_unsupported`. En qualsevol altre cas és `null`, i sempre és `null` per a `isf_build`, perquè la comprovació de `dwarf2json` verifica un pin SHA-256 en lloc de sondejar una versió.
- `untested` és `true` només per a una eina d'`analysis` que està `ready` amb una versió igual o superior a la versió mínima però fora de les versions provades amb aquesta versió del producte. Sempre és `false` per a `isf_build`.
- La resposta omet deliberadament les rutes dels executables configurats, els hashos esperat i real de `dwarf2json`, els valors d'entorn i la sortida crua del sondeig.

### Versió mínima i versions no provades

`2.28.0` és el **mínim** documentat, no una coincidència exacta. Les versions s'ordenen segons [PEP 440](https://peps.python.org/pep-0440/), mai com a cadenes:

- Per sota del mínim —inclosa una preversió d'ell, com ara `2.28.0rc1`, que queda per sota de `2.28.0`— l'eina es rebutja: `ready` és `false` amb el codi `volatility_version_unsupported`, i no s'executa cap anàlisi. Una cadena de versió que no es pot interpretar com a versió es rebutja igual, perquè no es pot comparar amb el mínim.
- Una de les versions provades: `ready` és `true`, `untested` és `false`, i no canvia res.
- Igual o superior al mínim però sense ser una versió provada: l'anàlisi s'executa igualment, i la resposta ho marca perquè un client pugui avisar l'operador que els resultats no estan validats:

```json
{
  "ready": true,
  "code": "ready",
  "detail": "Volatility 2.29.0 is newer than the versions tested with this release; analysis will run, but results have not been validated against this version.",
  "version": "2.29.0",
  "untested": true
}
```

La comprovació es fa de nou a cada crida, i una resposta de readiness mai no es tracta com a autorització d'execució: el hash de `dwarf2json` es recalcula immediatament abans de cada execució local i de cada transferència al guest.

Com la resta de l'API JSON, aquest endpoint requereix el token bearer d'admin o la cookie de sessió del navegador quan `auth.enabled = true`.

## Catàleg

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/api/v1/targets` | Llistar imatges de SO target. |
| `GET` | `/api/v1/scenarios` | Llistar escenaris de job. |
| `GET` | `/api/v1/workloads` | Llistar workloads. Suporta els paràmetres de consulta `cve`, `target_id` i `compatible_only`. |
| `GET` | `/api/v1/cves` | Llistar CVEs indexats i IDs de workload. |

## Paquets d'escenari

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/api/v1/scenario-packages/playground/current` | Exportar el Playground actiu com a zip compartible amb `metadata.yaml` i `scenario.yaml`. |
| `POST` | `/api/v1/scenario-packages/import` | Importar un zip de paquet d'escenari. El multipart opcional `resource_overrides` pot redimensionar VMs per etiqueta amb `vcpus`, `memory_mb` i `disk_gb`. |

## Jobs

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | Crear un job. |
| `GET` | `/api/v1/jobs` | Llistar jobs. |
| `GET` | `/api/v1/jobs/{job_id}` | Obtenir els detalls d'un job. |
| `DELETE` | `/api/v1/jobs/{job_id}` | Eliminar només metadades buides d'un Job terminal després de confirmar la neteja. `delete_files=true` no elimina evidències ni evita les proteccions de custòdia. |
| `GET` | `/api/v1/jobs/{job_id}/logs` | Llistar els esdeveniments d'un job. |
| `GET` | `/api/v1/jobs/{job_id}/artifact` | Descarregar l'artefacte de memòria. |
| `GET` | `/api/v1/jobs/{job_id}/disk-artifact` | Descarregar l'artefacte de disc. |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Sol·licitar la cancel·lació. |
| `POST` | `/api/v1/jobs/{job_id}/capture` | Disparar captura manual de memòria, disc o tots dos. |
| `POST` | `/api/v1/jobs/{job_id}/acquire-memory-dump` | Àlies de la captura manual. |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Crear un nou job a partir de la configuració d'un job fallit. |
| `POST` | `/api/v1/jobs/{job_id}/force-fail` | Forçar un job a l'estat fallit i matar els subprocessos actius. |
| `GET` | `/api/v1/jobs/{job_id}/lab-info` | Obtenir URLs de workload, credencials i metadades de lab. |
| `POST` | `/api/v1/jobs/{job_id}/reset-lab` | Sol·licitar la restauració des del snapshot net del lab. |
| `GET` | `/api/v1/jobs/{job_id}/runtimes` | Llistar els registres de runtime per VM. |
| `POST` | `/api/v1/jobs/{job_id}/runtimes/{role}/{action}` | Sol·licitar una acció de runtime com ara `reboot` o `shutdown`. |

L'eliminació retorna `204` quan el Job terminal ja no conserva evidències i la neteja està confirmada. Retorna `404` si no existeix; `409 conflict` si no és terminal; `409 evidence_retained` si conserva evidències o historial de custòdia; i `409 cleanup_pending` si queden recursos pendents. Elimina les evidències elegibles per **Storage**, respectant-ne les proteccions, abans de tornar-ho a provar. El paràmetre `delete_files` no canvia aquesta seqüència.

## Adquisicions i accions d'host

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/api/v1/acquisitions` | Llistar adquisicions. |
| `POST` | `/api/v1/acquisitions` | Crear un registre d'adquisició. |
| `GET` | `/api/v1/acquisitions/{acquisition_id}` | Obtenir els detalls d'una adquisició. |
| `PATCH` | `/api/v1/acquisitions/{acquisition_id}/status` | Actualitzar l'estat de l'adquisició a `live`, `paused` o `sealed`. |
| `GET` | `/api/v1/cases` | Llistar casos. |
| `POST` | `/api/v1/cases` | Crear un cas. |
| `GET` | `/api/v1/cases/{case_id}` | Obtenir els detalls d'un cas. |
| `PATCH` | `/api/v1/cases/{case_id}/status` | Actualitzar l'estat del cas a `open`, `closed` o `sealed`. |
| `GET` | `/api/v1/artifacts` | Llistar artefactes. Suporta els paràmetres de consulta `case_id`, `job_id` i `acquisition_id`. |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Obtenir identitat, checksum i procedència; utilitzar els endpoints de descàrrega per als sidecars. |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | Descarregar el fitxer de l'artefacte pel seu ID. |
| `GET` | `/api/v1/artifacts/{artifact_id}/manifest` | Descarregar el sidecar de manifest de l'artefacte. |
| `GET` | `/api/v1/artifacts/{artifact_id}/sha256` | Descarregar el sidecar SHA-256 de l'artefacte. |
| `GET` | `/api/v1/reports` | Llistar informes. Suporta els paràmetres de consulta `case_id`, `job_id`, `acquisition_id`, `input_artifact_id` i `output_artifact_id`. |
| `GET` | `/api/v1/reports/{report_id}` | Obtenir les metadades d'un informe. |
| `GET` | `/api/v1/host-actions` | Llistar els registres d'accions d'host. |
| `POST` | `/api/v1/run-retention` | Disparar la neteja de retenció. |

## Sessions i xarxes del Playground

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `POST` | `/api/v1/playground/sessions` | Crear una sessió de playground. |
| `GET` | `/api/v1/playground/sessions/current` | Obtenir la sessió de playground activa. |
| `GET` | `/api/v1/playground/sessions/current-or-null` | Obtenir la sessió de playground activa, o `null` quan no n'hi ha cap, sense retornar un 404. |
| `GET` | `/api/v1/playground/sessions/recent-failure` | Obtenir el desmuntatge fallit més recent del Playground, o `null` quan no n'hi ha cap de registrat. |
| `DELETE` | `/api/v1/playground/sessions/{job_id}` | Destruir la sessió de playground activa. |
| `POST` | `/api/v1/playground/networks` | Crear una xarxa, creant una sessió si cal. |
| `GET` | `/api/v1/playground/networks/suggest-cidr` | Suggerir el següent CIDR de playground disponible. Els paràmetres opcionals `job_id` i `extra` repetit reserven CIDRs existents o proposats. |
| `POST` | `/api/v1/playground/sessions/{job_id}/networks` | Crear una xarxa en una sessió activa existent. |
| `DELETE` | `/api/v1/playground/networks/{network_id}` | Eliminar una xarxa que no té VMs connectades. |
| `GET` | `/api/v1/playground/sessions/current/topology-layout` | Obtenir les posicions desades dels nodes de topologia. |
| `PATCH` | `/api/v1/playground/sessions/current/topology-layout/{node_id}` | Desar la posició d'un node de topologia. |
| `GET` | `/api/v1/playground/sessions/current/recipe` | Exportar la sessió activa com una recepta JSON de playground. |
| `POST` | `/api/v1/playground/recipes/import` | Importar una recepta JSON de playground a la sessió activa, creant-ne una si cal. |

## VMs, NICs, captures i mètriques del Playground

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `POST` | `/api/v1/playground/vms` | Desplegar una VM, creant una sessió i una xarxa per defecte si cal. |
| `POST` | `/api/v1/playground/sessions/{job_id}/vms` | Desplegar una VM en una sessió activa existent. |
| `DELETE` | `/api/v1/playground/vms/{vm_id}` | Destruir una VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/actions` | Sol·licitar una acció d'energia de VM: `start`, `stop` o `reboot`. |
| `POST` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics` | Connectar una NIC a una VM. |
| `DELETE` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}` | Desconnectar una NIC d'una VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/captures` | Disparar captura de memòria o disc per a una VM en execució. |
| `GET` | `/api/v1/playground/vms/{vm_id}/captures` | Llistar les captures d'una VM. |
| `GET` | `/api/v1/playground/captures/{acq_id}/events` | Fer streaming d'esdeveniments de captura com a Server-Sent Events. |
| `GET` | `/api/v1/playground/vms/{vm_id}/metrics` | Obtenir mètriques en viu d'una VM. |
| `GET` | `/api/v1/playground/sessions/current/metrics` | Obtenir mètriques en viu de les VMs en execució de la sessió actual. |

## Catàleg del Playground

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `GET` | `/api/v1/playground/catalog/images` | Llistar les imatges elegibles per al playground. El paràmetre opcional `role` per defecte és `target`. |
| `GET` | `/api/v1/playground/catalog/workloads` | Llistar els workloads compatibles amb `image_id`. |

## Overrides avançats

Vies d'escapament per a operadors que subministren artefactes manualment (registrats amb procedència no verificada). Requereixen el token bearer d'admin quan l'autenticació està activada.

| Mètode | Ruta | Propòsit |
| --- | --- | --- |
| `POST` | `/api/v1/jobs/{job_id}/isf/upload` | Emmagatzemar un ISF subministrat per l'operador per a un job, ometent l'enforcement de GNU build-id. |
| `POST` | `/api/v1/jobs/{job_id}/dump/upload` | Emmagatzemar un dump de memòria subministrat per l'operador per a un job, ometent la validació de completitud ELF. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Emmagatzemar un ISF subministrat per l'operador des de la fila de target del workbench de Volatility. |

## Exemples de petició

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

Crear una xarxa de playground:

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
