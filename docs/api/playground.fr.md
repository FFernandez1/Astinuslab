# Référence de l'API Playground

> **Interne mais à contrat stable. Le frontend dépend de cette surface.**

Le Playground expose une API REST (v1) pour les sessions de labo interactives : VMs ad-hoc, réseaux multi-segments, captures à la demande et métriques en direct. Toutes les routes sont montées sous le tag `playground`.

**URL de base** : `http://<host>:8000/api/v1/playground` pour les routes limitées au Playground. Les routes de paquets de scénario sont montées sur `http://<host>:8000/api/v1/scenario-packages`.

Les corps de requête et de réponse référencent des schemas Pydantic définis dans `src/astinuslab/api/schemas.py` (par ex. `PlaygroundSessionOut`, `PlaygroundVMOut`). Les modèles de réponse locaux déclarés dans `src/astinuslab/playground/serializers.py` et `src/astinuslab/playground/routes/*.py` (`PlaygroundAcceptedOut`, `PlaygroundCaptureOut`, `PlaygroundAcquisitionOut`, `PlaygroundCreateIfNeededSpawnVMRequest`) sont documentés inline.

Les routes qui mettent en file du travail Celery renvoient HTTP `202 Accepted`. Les conflits de session unique et de nommage renvoient `409 Conflict`. Les sessions inexistantes ou non actives renvoient `404 Not Found`. Les échecs de mise en file du broker se manifestent comme `503 Service Unavailable` avec détail `operation could not be queued`.

## Champs identifiants de session

Les réponses Playground exposent maintenant `session_id` comme identifiant canonique de session playground. Le `job_id` legacy reste présent avec la même valeur, est marqué déprécié dans OpenAPI et sera supprimé dans la prochaine version mineure. Les nouveaux clients frontend/API doivent lire `session_id` et ne retomber sur `job_id` que pour les anciens serveurs.

---

## 1. Sessions

Une session de playground est à slot unique : une seule session peut être active à la fois, et elle ne peut pas coexister avec un job forensique actif.

### Créer une session
`POST /sessions` -> `202 Accepted`
Crée une nouvelle session de playground vide.
**Réponse** : `PlaygroundSessionOut`.
**Erreurs** : `409` si une session de playground est déjà active, ou si un job forensique est actuellement actif.

### Obtenir la session actuelle
`GET /sessions/current` -> `200 OK`
Renvoie la session active actuelle, y compris toutes les VMs et réseaux.
**Réponse** : `PlaygroundSessionOut`.
**Erreurs** : `404` s'il n'y a pas de session de playground active.

### Obtenir la session actuelle ou null
`GET /sessions/current-or-null` -> `200 OK`
Renvoie la session active actuelle, avec toutes les VMs et réseaux, ou JSON `null` quand aucune session active n'existe. Le frontend l'utilise pour le polling normal sans traiter un Playground vide comme une erreur.
**Réponse** : `PlaygroundSessionOut` ou `null`.

### Obtenir l'échec de destruction le plus récent
`GET /sessions/recent-failure` -> `200 OK`
Renvoie la session Playground terminée la plus récente dont le démontage a échoué, ou JSON `null` lorsqu'aucun démontage en échec n'a été enregistré. Le frontend l'utilise pour afficher une alerte de nettoyage non bloquante après la fin d'une session.
**Réponse** : `PlaygroundFailureOut` ou `null`. Un objet d'échec contient `session_id`, `status` (`destroy_failed`) et les champs optionnels `error_summary` et `finished_at`.

### Détruire la session
`DELETE /sessions/{job_id}` -> `202 Accepted`
Marque la session `destroy_requested` et met en file le démantèlement de toutes les VMs et réseaux.
**Réponse** : `PlaygroundAcceptedOut`.
**Erreurs** : `404` si la session n'est pas trouvée ou n'est pas active.

---

## 2. Réseaux

Les réseaux sont des segments adossés à libvirt et délimités à une session. Chaque réseau a un nom unique dans sa session, un CIDR et un flag d'isolation.

### Suggérer un CIDR
`GET /networks/suggest-cidr?job_id=<session-id>&extra=10.42.2.0/24` -> `200 OK`
**Réponse** : `PlaygroundCidrSuggestionOut` (`subnet_cidr`).
Renvoie le prochain CIDR `10.42.x.0/24` libre. `job_id` est optionnel et réserve les réseaux existants de cette session. `extra` est optionnel et répétable ; utilisez-le pour réserver des CIDR brouillon de l'assistant non encore persistés.
**Erreurs** : `409` si le pool de CIDR disponible est épuisé.

### Créer un réseau (sans friction, créer-ou-réutiliser la session)
`POST /networks` -> `202 Accepted`
**Payload** : `PlaygroundCreateNetworkRequest` (`name`, `subnet_cidr`, `isolated`).
**Réponse** : `PlaygroundSessionOut` — l'état complet de la session après l'enregistrement du réseau comme en attente.

Réutilise la session de playground active si elle existe, sinon crée d'abord une nouvelle session. Le réseau est enregistré dans l'état `pending` avant la mise en file de la tâche Celery `create_network`, donc le frontend peut sortir de la vue « créer le premier réseau » en un seul aller-retour et sonder l'état de la session pour l'activation. Cela diffère de la variante délimitée à la session ci-dessous, qui requiert que la session existe déjà.

**Erreurs** :
- `409` si un job forensique est actif, si le nom du réseau existe déjà dans la session, ou si un conflit de CIDR est détecté.
- `503` si la mise en file du broker échoue après l'enregistrement du réseau (le réseau est marqué `failed`, et toute session créée dans cet appel est démantelée).

### Créer un réseau (délimité à la session)
`POST /sessions/{job_id}/networks` -> `202 Accepted`
**Payload** : `PlaygroundCreateNetworkRequest`.
**Réponse** : `PlaygroundNetworkOut`.
Requiert que la session cible soit la session active. Enregistre un réseau en attente et met en file la tâche de création.
**Erreurs** : `404` si la session n'est pas active ; `409` pour les collisions de nom, conflits de CIDR ou jobs forensiques actifs ; `503` en cas d'échec du broker.

### Supprimer un réseau
`DELETE /networks/{network_id}` -> `202 Accepted`
Marque le réseau `destroying` et met en file le démantèlement.
**Réponse** : `PlaygroundAcceptedOut`.
**Erreurs** : `404` si le réseau n'est pas trouvé dans la session active ; `409` si une VM y est encore attachée.

---

## 3. VMs

Les VMs sont démarrées depuis des images du catalogue et attachées à un ou plusieurs réseaux via des NIC. Les labels doivent être uniques dans une session.

### Lancer une VM (wrapper créer-si-nécessaire)
`POST /vms` -> `202 Accepted`
**Payload** : `PlaygroundCreateIfNeededSpawnVMRequest` :

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

`role` est `"target"` ou `"attacker"` (par défaut `"target"`). `network_ids` est optionnel : lorsqu'il est omis sans `new_networks`, le wrapper s'assure qu'un réseau isolé par défaut existe (`name="default"`, `subnet_cidr="10.42.1.0/24"`) et y attache la VM. `new_networks` est optionnel et contient des objets `PlaygroundCreateNetworkRequest` inline ; chaque réseau inline est créé d'abord, puis attaché à la VM avec tout `network_ids` existant. Lorsque `network_ids` est fourni, chaque réseau référencé doit déjà exister dans la session active et être dans l'état `active`.

**Réponse** : `PlaygroundVMOut`.

Crée une session de playground si aucune n'est active, s'assure que les réseaux demandés (ou un réseau par défaut) sont présents, puis délègue à l'endpoint de spawn délimité à la session. Toute session créée par cet appel est démantelée en cas d'échec.

**Erreurs** :
- `400` si un réseau ou une image référencé est inconnu, ou si la validation du workload échoue.
- `404` si un réseau référencé est manquant.
- `409` si le label est déjà utilisé, le réseau n'est pas utilisable, ou l'unicité de session/réseau/VM est violée.
- `500` en cas d'échec interne (toute session créée dans cet appel est démantelée).
- `503` si la mise en file du broker échoue.

### Lancer une VM (délimité à la session)
`POST /sessions/{job_id}/vms` -> `202 Accepted`
**Payload** : `PlaygroundSpawnVMRequest` (`label`, `image_id`, `nics`, optionnel `vcpus`/`memory_mb`/`disk_gb`, `role`, optionnel `workload_id`).
**Réponse** : `PlaygroundVMOut`.
Enregistre une VM en attente avec une NIC par entrée dans `nics` et met en file la tâche de spawn.
**Erreurs** : `400` pour les réseaux inconnus ou les échecs de validation du workload ; `404` si la session n'est pas active ; `409` pour les collisions de label ou un état de réseau inutilisable ; `503` si la mise en file du broker échoue (la VM est marquée `failed`).

### Détruire une VM
`DELETE /vms/{vm_id}` -> `202 Accepted`
Marque la VM `destroying` et met en file le démantèlement.
**Réponse** : `PlaygroundAcceptedOut`.
**Erreurs** : `404` si la VM n'est pas trouvée dans la session active ; `503` si la mise en file du broker échoue (l'état de la VM est annulé).

### Action d'alimentation de VM
`POST /vms/{vm_id}/actions` -> `202 Accepted`
**Payload** : `PlaygroundVMActionRequest` (`action` : `"start"`, `"stop"` ou `"reboot"`).
**Réponse** : `PlaygroundAcceptedOut`.
Exécute l'action libvirt correspondante contre le domaine de la VM. `stop` correspond à `virsh shutdown` ; `start` et `reboot` utilisent les actions virsh correspondantes. L'état de la VM est mis à jour de façon optimiste après le succès de la commande.
**Erreurs** : `404` si la VM n'est pas trouvée dans la session active ; `409` si l'action demandée n'est pas disponible pour l'état actuel de la VM/session ; `500` si la commande libvirt échoue.

---

## 4. NICs

L'attach et le detach de NIC opèrent sur la session active.

### Attacher une NIC
`POST /sessions/current/vms/{vm_id}/nics` -> `202 Accepted`
**Payload** : `PlaygroundAttachNicRequest` (`network_id`).
**Réponse** : `PlaygroundAcceptedOut`.
**Erreurs** : `404` s'il n'y a pas de session active, VM ou réseau ; `503` si la mise en file du broker échoue.

### Détacher une NIC
`DELETE /sessions/current/vms/{vm_id}/nics/{nic_id}?force=false` -> `202 Accepted`
**Réponse** : `PlaygroundAcceptedOut`.
**Erreurs** : `404` s'il n'y a pas de session active, VM ou NIC ; `503` si la mise en file du broker échoue. Si `force=true` et que la mise en file échoue, la ligne NIC est supprimée localement et l'endpoint renvoie `200 OK`.

---

## 5. Captures

Les captures produisent des acquisitions mémoire ou disque liées à une VM et à la session parente.

### Déclencher une capture
`POST /vms/{vm_id}/captures` -> `202 Accepted`
**Payload** : `PlaygroundCaptureRequest` (`kind` : `"memory"` ou `"disk"`).
**Réponse** (modèle local `PlaygroundCaptureOut`) :

```json
{
  "acquisition_id": "<hex>",
  "status": "queued",
  "task_id": "<celery-task-id-or-null>"
}
```

Une nouvelle ligne `Acquisition` est créée dans l'état `queued` et un événement `queued` est enregistré avant la mise en file de la tâche de capture.

**Erreurs** : `404` si la VM n'est pas dans la session active ; `409` si la VM n'est pas `running` ; `503` si la mise en file du broker échoue (l'acquisition est marquée `failed` et un événement `failed` est enregistré).

### Lister les captures
`GET /vms/{vm_id}/captures` -> `200 OK`
**Réponse** : `list[PlaygroundAcquisitionOut]` (modèle local). Chaque entrée :

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

Triées de la plus récente à la plus ancienne.

### Diffuser les événements de capture (SSE)
`GET /captures/{acq_id}/events` -> `200 OK` (`text/event-stream`)
Flux de Server-Sent Events des lignes `AcquisitionEvent` pour l'acquisition donnée.

**Erreurs** : `404` si l'acquisition n'est pas trouvée.

Chaque événement est émis au format SSE standard :

```
event: <status>
data: {"id":<int>,"status":"<status>","message":"<text>","created_at":"<iso8601>"}

```

**Types d'événements** :

| Événement | Signification |
| --- | --- |
| `queued` | La capture a été enregistrée et la tâche Celery est mise en file. |
| `running` | La tâche de capture a commencé à s'exécuter. |
| `completed` | La capture s'est terminée avec succès (terminal). |
| `failed` | La capture a échoué (terminal). |
| `stream_timeout` | Le deadline du flux côté serveur a été atteint. Émis avec `data: {"status":"stream_timeout","message":"server closed stream"}` avant la fermeture de la connexion. |

Le flux se ferme lorsque l'acquisition atteint un état terminal (`succeeded`, `completed`, `failed`, `cancelled`, `error`), lorsque le client se déconnecte, ou lorsque le timeout côté serveur (`playground_capture_stream_timeout_seconds`) s'écoule.

---

## 6. Catalogue

Les endpoints de catalogue exposent les images et workloads utilisables dans le playground.

### Lister les images Playground
`GET /catalog/images?role=target` -> `200 OK`
Renvoie les images de base éligibles au playground pour le `role` demandé (par défaut `"target"`).
**Réponse** : `list[dict]` de `catalog.list_playground_images_for_role`.
**Erreurs** : `400` si le rôle est invalide.

### Lister les workloads pour une image
`GET /catalog/workloads?image_id=<id>` -> `200 OK`
Renvoie les workloads compatibles avec l'image donnée, annotés avec le statut de compatibilité.
**Réponse** : `list[PlaygroundWorkloadCatalogOut]`.
**Erreurs** : `404` si l'image n'est pas trouvée.

---

## 7. Métriques

Les métriques sont échantillonnées par VM via `playground_metrics`. Elles sont best-effort et renvoient `503` si l'échantillonnage libvirt est indisponible.

### Obtenir les métriques d'une VM
`GET /vms/{vm_id}/metrics` -> `200 OK`
**Réponse** : `PlaygroundVMMetricsOut` (`vm_id`, `state`, `cpu_pct`, `mem_pct`, `mem_used_mb`, `mem_total_mb`, `rx_bytes`, `tx_bytes`, `sampled_at`).
**Erreurs** : `404` si la VM n'est pas dans la session active ; `503` si l'échantillonneur échoue.

### Métriques de session par lot
`GET /sessions/current/metrics` -> `200 OK`
Échantillonne chaque VM `running` de la session active.
**Réponse** : `dict[str, PlaygroundVMMetricsOut]` indexé par `vm_id`. Les VMs dont l'échantillon échoue sont omises (l'échec est journalisé mais ne fait pas échouer la requête).
**Erreurs** : `404` s'il n'y a pas de session de playground active.

---

## 8. Topologie

Les positions de layout de topologie sont persistées par nœud pour la session active. Le frontend applique un debounce des sauvegardes par nœud côté client ; le serveur stocke une ligne par nœud.

### Obtenir le layout de topologie
`GET /sessions/current/topology-layout` -> `200 OK`
**Réponse** : `PlaygroundTopologyLayout` (`nodes: list[PlaygroundTopologyNode]`).
**Erreurs** : `404` s'il n'y a pas de session de playground active.

### Enregistrer un nœud de topologie
`PATCH /sessions/current/topology-layout/{node_id}` -> `200 OK`
**Payload** : `PlaygroundTopologyNode` (`node_id`, `node_type` de `"vm"` ou `"network"`, `x`, `y`). Le `node_id` du corps doit correspondre au path.
**Réponse** : `PlaygroundTopologyNode`.
**Erreurs** : `400` si le `node_id` du corps ne correspond pas au path ; `404` s'il n'y a pas de session de playground active.

---

## 9. Recettes

Les recettes sont des documents JSON utilisant `PlaygroundRecipe` (`schema_version: 1`). Elles capturent les réseaux, les VMs et les positions de topologie pour des labos de Playground répétables. Les entrées de VM peuvent inclure `vcpus`, `memory_mb` et `disk_gb` ; l'import les transmet au même chemin de spawn que la création directe de VM.

### Exporter la recette actuelle
`GET /sessions/current/recipe` -> `200 OK`
**Réponse** : `PlaygroundRecipe`.
Exporte la session active. Les IDs de réseau et de VM sont traduits en noms de recette stables : les nœuds de réseau utilisent les noms de réseau, les nœuds de VM utilisent les labels de VM.
**Erreurs** : `404` s'il n'y a pas de session de playground active.

### Importer une recette
`POST /recipes/import` -> `202 Accepted`
**Payload** : `PlaygroundRecipe`.
**Réponse** : `PlaygroundSessionOut`.
Valide la forme de la recette, crée une session si nécessaire, crée les réseaux, lance les VMs et stocke les positions de topologie. Les entrées `networks` des VMs de la recette référencent des noms de réseau de recette, pas des IDs de base de données.
**Erreurs** : `400` pour des références de recette invalides ou des erreurs de validation de schema ; `409` pour des collisions de nom avec des réseaux ou labels de VM existants, ou si la recette dépasse la capacité de VMs (`code: vm_capacity`) avant la création des réseaux ; des erreurs spécifiques de création/spawn de la création de réseau ou de VM peuvent aussi se manifester.

---

## 10. Paquets de scénario

Les paquets de scénario sont des archives zip pour le partage en un clic. Ils contiennent :

- `metadata.yaml` — métadonnées du paquet avec `schema_version: 1` et `package_type: astinuslab.playground.scenario`.
- `scenario.yaml` — données de recette Playground.
- Entrées optionnelles de scripts relatives et sûres pour de futurs workflows de paquets catalogue/workload.

Ces routes sont montées sous `/api/v1/scenario-packages`, pas sous `/api/v1/playground`.

### Exporter le paquet Playground actuel
`GET /api/v1/scenario-packages/playground/current` -> `200 OK` (`application/zip`)

Exporte la session Playground active en paquet zip. La réponse utilise `Content-Disposition: attachment` avec un nom de fichier `.zip`.

**Erreurs** : `404` s'il n'y a pas de session de playground active.

### Importer un paquet Playground
`POST /api/v1/scenario-packages/import` -> `202 Accepted`

**Payload** : `multipart/form-data` avec :

- `file` — zip de paquet de scénario.
- `resource_overrides` — objet JSON optionnel indexé par label de VM. Les champs autorisés par VM sont `vcpus`, `memory_mb` et `disk_gb`.

**Réponse** : `PlaygroundSessionOut`.

L'import valide l'archive, rejette les chemins dangereux, parse `scenario.yaml`, applique les surcharges de ressources, valide le résultat comme `PlaygroundRecipe`, puis crée réseaux et VMs via le flux normal d'import de recette. Les scripts empaquetés sont validés/listés mais cet endpoint ne les installe pas dans le catalogue local.

**Erreurs** : `400` pour zips malformés, chemins dangereux, YAML invalide, surcharges invalides ou schemas de recette invalides ; `409` pour collisions de nom avec réseaux ou labels de VM existants.

---

## Modèles de réponse locaux

Ces modèles sont déclarés à l'intérieur de `src/astinuslab/playground/serializers.py` et utilisés uniquement par les routes du playground :

### `PlaygroundAcceptedOut`
```json
{
  "status": "accepted",
  "resource_id": "<id or null>",
  "job_id": "<job_id or null>",
  "task_id": "<celery-task-id or null>"
}
```
Renvoyé par tous les endpoints `202 Accepted` qui ne renvoient pas de snapshot de ressource.

### `PlaygroundCreateIfNeededSpawnVMRequest`
Le schema de requête pour `POST /vms`. Voir **Lancer une VM (wrapper créer-si-nécessaire)** ci-dessus pour la liste complète des champs.

### `PlaygroundCaptureOut`
Renvoyé par `POST /vms/{vm_id}/captures`. Voir **Déclencher une capture** ci-dessus.

### `PlaygroundAcquisitionOut`
Renvoyé par `GET /vms/{vm_id}/captures`. Voir **Lister les captures** ci-dessus.
