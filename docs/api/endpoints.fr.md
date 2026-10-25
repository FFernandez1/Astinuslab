# Endpoints

Cette liste est vérifiée par rapport à `src/astinuslab/api/routes.py`, `src/astinuslab/playground/api.py`, `src/astinuslab/playground/routes/*.py` et `src/astinuslab/main.py`.

## Health

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/health` | Check de liveness du processus API, sans dépendances. |
| `GET` | `/ready` | Check de readiness des dépendances ; renvoie 503 uniquement si la base de données ou le stockage inscriptible échoue. |
| `GET` | `/api/v1/readiness` | Exécuter des contrôles authentifiés approfondis des dépendances et permissions. |
| `GET` | `/api/v1/metrics` | Métriques d'activité en lecture seule authentifiées sur une fenêtre de temps. |

### Sondes opérationnelles

- `GET /health` est le check de liveness du processus. Il ne vérifie aucune dépendance et renvoie `200` tant que le processus API peut servir des requêtes.
- `GET /ready` considère la base de données et le stockage inscriptible comme des dépendances critiques. Les échecs des workers ou de libvirt renvoient `200` avec `status: degraded` ; les échecs de la base de données ou du stockage renvoient `503` avec `status: not_ready`.

Les deux endpoints sont publics et renvoient un en-tête `X-Request-ID`. Les réponses n'exposent que des états généraux des composants ; utilisez cet identifiant pour retrouver les détails de diagnostic dans les logs du backend.

### Métriques d'activité

`GET /api/v1/metrics` est le résumé authentifié en lecture seule de l'activité récente, agrégé depuis les lignes existantes de Job et HostAction. Là où `/ready` indique si la plateforme peut travailler, celui-ci indique comment elle a travaillé.

`window_hours` (par défaut `24`, plage 1–2160) sélectionne la fenêtre. La réponse contient `generated_at`, `window_hours`, une liste `actions` (par type d'action : `completed`, `failed`, `p50_seconds`, `p95_seconds`, `max_seconds`), une table `jobs` de comptes par statut et un bloc `queue` (`queued`, `running`, `oldest_queued_age_seconds`, `max_dispatch_attempts`). Les percentiles sont de rang le plus proche sur les actions terminées uniquement. Le bloc `queue` n'est pas limité à la fenêtre. Les mêmes chiffres apparaissent sur la page Paramètres.

### Readiness approfondie

`GET /api/v1/readiness` est le contrôle authentifié de l'opérateur à exécuter avant de démarrer des Jobs. Il renvoie `200` lorsque tous les contrôles applicables ont réussi ou sont `not_applicable`, et `503` lorsqu'au moins un contrôle a échoué. L'ensemble complet des sondes a un délai de réponse de 10 secondes ; à son expiration, il renvoie le contrôle stable `deep_readiness_timeout` au lieu de bloquer la requête.

La réponse contient `ready`, le `status` global et une liste ordonnée `checks`. Chaque contrôle possède un `name` stable et un `code` lisible par machine, un `status` `passed`, `failed` ou `not_applicable`, un `detail` sûr pour l'opérateur et les identités de service `applies_to` (`api`, `runner` ou `analysis`). Les contrôles couvrent la transaction d'écriture de la base de données, le broker Celery, des heartbeats distincts pour le runner et l'analyse, la connexion libvirt, l'écriture et le seuil d'espace libre du stockage, la politique de racines privilégiées, la propriété/mode des helpers, la non-écriture du code applicatif, les outils d'analyse et de build ISF Volatility et l'état du mode fake.

| `name` du contrôle | Valeurs `code` stables non prêtes | `applies_to` |
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
| `volatility_analysis`, `volatility_isf_build` | Codes de readiness Volatility décrits ci-dessous, ou `feature_disabled` | `runner`, `analysis` / `runner` |
| `fake_mode` | `fake_mode_active` | `api`, `runner`, `analysis` |

Le sudo des helpers privilégiés est délibérément limité par service. Le service API s'exécute avec `NoNewPrivileges=true` ; son contrôle sudo est donc toujours `not_applicable` et le processus API n'exécute jamais `sudo -n`. La couverture sudo du runner est évaluée dans le runner et exposée par sa réponse de readiness propre ; si aucune réponse du runner n'arrive, le heartbeat échoue tandis que le contrôle sudo reste `not_applicable`. L'absence de couverture sudo n'est pas non plus un avertissement pour le worker d'analyse.

Les battements des processus de travail ont la même portée de transport. Le contrôle à distance de Celery est un échange de diffusion et le transport par défaut du courtier SQLite n'implémente pas la diffusion, donc aucune réponse ne peut jamais arriver. Sur un tel courtier, `runner_heartbeat`, `analysis_heartbeat` et `privileged_helper_sudo_runner` signalent `not_applicable` avec `remote_control_unsupported` au lieu d'un échec faux permanent ; la vivacité des processus est prouvée par le gestionnaire de services et par de véritables allers-retours de file d'attente.

## UI web

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/volatility` | Ouvrir le workbench Volatility 3 pour la préparation des symboles, les builds ISF, les diagnostics et l'historique de build. |
| `POST` | `/volatility/build` | Créer un job de build de symboles Linux ISF-only depuis le workbench Volatility. Utilise en interne la même queue de jobs et le même contrat `build_vol3_isf` que l'API REST. |

## API JSON Volatility

API canonique du workbench Volatility 3, versionnée sous `/api/v1/volatility`. Les anciens chemins `/volatility/api/*` sont des alias dépréciés qui émettent `Deprecation: true`.

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/api/v1/volatility/readiness` | Indiquer si les outils Volatility 3 et `dwarf2json` fournis par l'opérateur sont utilisables. |
| `GET` | `/api/v1/volatility/dumps` | Lister les dumps mémoire disponibles pour le workbench Volatility. |
| `GET` | `/api/v1/volatility/catalog` | Lister les plugins Volatility pris en charge, éventuellement filtrés par OS. |
| `GET` | `/api/v1/volatility/queue` | Lister les exécutions récentes de plugins du workbench sur tous les dumps. |
| `DELETE` | `/api/v1/volatility/queue` | Nettoyer la queue : supprimer les fichiers de résultats de chaque exécution de plugin (les lignes de rapport et d'artefact sont conservées). |
| `POST` | `/api/v1/volatility/runs` | Mettre en queue une exécution à la demande d'un seul plugin pour le dump d'un job. |
| `GET` | `/api/v1/volatility/runs/latest` | Renvoyer l'exécution terminée la plus récente pour un `job_id`+`plugin`, ou `{"status": "none"}`. |
| `GET` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Sonder une exécution de plugin du workbench et récupérer les résultats persistés. |
| `DELETE` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Supprimer une exécution de plugin de la queue. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Stocke un ISF fourni par l'opérateur depuis la ligne de cible de l'atelier Volatility (détaillé sous Téléversement manuel d'ISF ci-dessous). |

### Readiness des outils

Volatility 3 et `dwarf2json` sont des outils hôtes fournis par l'opérateur. `GET /api/v1/volatility/readiness` rapporte les deux capacités séparément, de sorte qu'un client peut proposer l'analyse mémoire pendant que les builds d'ISF Linux restent indisponibles, ou l'inverse :

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

- `enabled` vaut `false` lorsque l'intégration Volatility est désactivée ; les deux capacités rapportent alors `feature_disabled` sans sonder l'hôte.
- `code` est une valeur stable lisible par machine. La liste complète est dans [Erreurs](errors.md), qui utilise les mêmes codes pour la réponse `503` / `dependency_unavailable`.
- `version` porte la version de la bannière Volatility chaque fois qu'une sonde en a produit une, c'est-à-dire avec `ready` et avec `volatility_version_unsupported`. Elle vaut `null` sinon, et toujours `null` pour `isf_build`, car la vérification de `dwarf2json` contrôle un pin SHA-256 au lieu de sonder une version.
- `untested` vaut `true` uniquement pour un outil d'`analysis` qui est `ready` avec une version égale ou supérieure à la version minimale mais hors des versions testées avec cette version du produit. Il vaut toujours `false` pour `isf_build`.
- La réponse omet délibérément les chemins des exécutables configurés, les hashs attendu et réel de `dwarf2json`, les valeurs d'environnement et la sortie brute de la sonde.

### Version minimale et versions non testées

`2.28.0` est le **minimum** documenté, pas une correspondance exacte. Les versions sont ordonnées selon [PEP 440](https://peps.python.org/pep-0440/), jamais comme des chaînes :

- En dessous du minimum — y compris une préversion de celui-ci, comme `2.28.0rc1`, qui se situe en dessous de `2.28.0` — l'outil est refusé : `ready` vaut `false` avec le code `volatility_version_unsupported`, et aucune analyse ne s'exécute. Une chaîne de version qui ne peut pas être interprétée comme une version est refusée de la même façon, car elle ne peut pas être comparée au minimum.
- Une des versions testées : `ready` vaut `true`, `untested` vaut `false`, et rien ne change.
- Égale ou supérieure au minimum mais sans être une version testée : l'analyse s'exécute tout de même, et la réponse le signale pour qu'un client puisse avertir l'opérateur que les résultats ne sont pas validés :

```json
{
  "ready": true,
  "code": "ready",
  "detail": "Volatility 2.29.0 is newer than the versions tested with this release; analysis will run, but results have not been validated against this version.",
  "version": "2.29.0",
  "untested": true
}
```

La vérification est refaite à chaque appel, et une réponse de readiness n'est jamais traitée comme une autorisation d'exécution : le hash de `dwarf2json` est recalculé immédiatement avant chaque exécution locale et chaque transfert vers le guest.

Comme le reste de l'API JSON, cet endpoint exige le token bearer admin ou le cookie de session du navigateur lorsque `auth.enabled = true`.

## Catalogue

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/api/v1/targets` | Lister les images d'OS cible. |
| `GET` | `/api/v1/scenarios` | Lister les scénarios de job. |
| `GET` | `/api/v1/workloads` | Lister les workloads. Prend en charge les paramètres de requête `cve`, `target_id` et `compatible_only`. |
| `GET` | `/api/v1/cves` | Lister les CVE indexés et les IDs de workload. |

## Paquets de scénario

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/api/v1/scenario-packages/playground/current` | Exporter le Playground actif comme zip partageable avec `metadata.yaml` et `scenario.yaml`. |
| `POST` | `/api/v1/scenario-packages/import` | Importer un zip de paquet de scénario. Le multipart optionnel `resource_overrides` peut redimensionner les VMs par label avec `vcpus`, `memory_mb` et `disk_gb`. |

## Jobs

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | Créer un job. |
| `GET` | `/api/v1/jobs` | Lister les jobs. |
| `GET` | `/api/v1/jobs/{job_id}` | Obtenir les détails d'un job. |
| `DELETE` | `/api/v1/jobs/{job_id}` | Supprimer seulement les métadonnées vides d'un Job terminal après confirmation du nettoyage. `delete_files=true` ne supprime pas les preuves et ne contourne pas leur protection. |
| `GET` | `/api/v1/jobs/{job_id}/logs` | Lister les événements d'un job. |
| `GET` | `/api/v1/jobs/{job_id}/artifact` | Télécharger l'artefact mémoire. |
| `GET` | `/api/v1/jobs/{job_id}/disk-artifact` | Télécharger l'artefact disque. |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Demander l'annulation. |
| `POST` | `/api/v1/jobs/{job_id}/capture` | Déclencher une capture manuelle mémoire, disque ou les deux. |
| `POST` | `/api/v1/jobs/{job_id}/acquire-memory-dump` | Alias de la capture manuelle. |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Créer un nouveau job à partir de la configuration d'un job échoué. |
| `POST` | `/api/v1/jobs/{job_id}/force-fail` | Forcer un job à l'état échoué et tuer les sous-processus actifs. |
| `GET` | `/api/v1/jobs/{job_id}/lab-info` | Obtenir les URLs de workload, les identifiants et les métadonnées de labo. |
| `POST` | `/api/v1/jobs/{job_id}/reset-lab` | Demander la restauration depuis le snapshot propre du labo. |
| `GET` | `/api/v1/jobs/{job_id}/runtimes` | Lister les enregistrements de runtime par VM. |
| `POST` | `/api/v1/jobs/{job_id}/runtimes/{role}/{action}` | Demander une action de runtime telle que `reboot` ou `shutdown`. |

La suppression renvoie `204` quand le Job terminal ne conserve plus de preuves et que son nettoyage est confirmé. Elle renvoie `404` s'il n'existe pas ; `409 conflict` s'il n'est pas terminal ; `409 evidence_retained` s'il conserve des preuves ou un historique de garde ; `409 cleanup_pending` si des ressources restent à nettoyer. Supprimez séparément les preuves admissibles dans **Storage**, dans le respect de leurs protections, avant de réessayer. Le paramètre `delete_files` ne change pas cette séquence.

## Acquisitions et actions hôte

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/api/v1/acquisitions` | Lister les acquisitions. |
| `POST` | `/api/v1/acquisitions` | Créer un enregistrement d'acquisition. |
| `GET` | `/api/v1/acquisitions/{acquisition_id}` | Obtenir les détails d'une acquisition. |
| `PATCH` | `/api/v1/acquisitions/{acquisition_id}/status` | Mettre à jour le statut d'acquisition à `live`, `paused` ou `sealed`. |
| `GET` | `/api/v1/cases` | Lister les cas. |
| `POST` | `/api/v1/cases` | Créer un cas. |
| `GET` | `/api/v1/cases/{case_id}` | Obtenir les détails d'un cas. |
| `PATCH` | `/api/v1/cases/{case_id}/status` | Mettre à jour le statut du cas à `open`, `closed` ou `sealed`. |
| `GET` | `/api/v1/artifacts` | Lister les artefacts. Prend en charge les paramètres de requête `case_id`, `job_id` et `acquisition_id`. |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Obtenir identité, somme de contrôle et provenance ; utiliser les endpoints de téléchargement pour les sidecars. |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | Télécharger le fichier de l'artefact par son ID. |
| `GET` | `/api/v1/artifacts/{artifact_id}/manifest` | Télécharger le sidecar de manifeste de l'artefact. |
| `GET` | `/api/v1/artifacts/{artifact_id}/sha256` | Télécharger le sidecar SHA-256 de l'artefact. |
| `GET` | `/api/v1/reports` | Lister les rapports. Prend en charge les paramètres de requête `case_id`, `job_id`, `acquisition_id`, `input_artifact_id` et `output_artifact_id`. |
| `GET` | `/api/v1/reports/{report_id}` | Obtenir les métadonnées d'un rapport. |
| `GET` | `/api/v1/host-actions` | Lister les enregistrements d'actions hôte. |
| `POST` | `/api/v1/run-retention` | Déclencher le nettoyage de rétention. |

## Sessions et réseaux du Playground

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `POST` | `/api/v1/playground/sessions` | Créer une session de playground. |
| `GET` | `/api/v1/playground/sessions/current` | Obtenir la session de playground active. |
| `GET` | `/api/v1/playground/sessions/current-or-null` | Obtenir la session de playground active, ou `null` lorsqu'il n'y en a aucune, sans renvoyer de 404. |
| `GET` | `/api/v1/playground/sessions/recent-failure` | Obtenir le dernier démontage Playground en échec, ou `null` lorsqu'aucun n'est enregistré. |
| `DELETE` | `/api/v1/playground/sessions/{job_id}` | Détruire la session de playground active. |
| `POST` | `/api/v1/playground/networks` | Créer un réseau, en créant une session si nécessaire. |
| `GET` | `/api/v1/playground/networks/suggest-cidr` | Suggérer le prochain CIDR de playground disponible. Les paramètres optionnels `job_id` et `extra` répété réservent des CIDR existants ou proposés. |
| `POST` | `/api/v1/playground/sessions/{job_id}/networks` | Créer un réseau dans une session active existante. |
| `DELETE` | `/api/v1/playground/networks/{network_id}` | Supprimer un réseau qui n'a pas de VMs attachées. |
| `GET` | `/api/v1/playground/sessions/current/topology-layout` | Obtenir les positions enregistrées des nœuds de topologie. |
| `PATCH` | `/api/v1/playground/sessions/current/topology-layout/{node_id}` | Enregistrer la position d'un nœud de topologie. |
| `GET` | `/api/v1/playground/sessions/current/recipe` | Exporter la session active comme une recette JSON de playground. |
| `POST` | `/api/v1/playground/recipes/import` | Importer une recette JSON de playground dans la session active, en en créant une si nécessaire. |

## VMs, NICs, captures et métriques du Playground

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `POST` | `/api/v1/playground/vms` | Lancer une VM, en créant une session et un réseau par défaut si nécessaire. |
| `POST` | `/api/v1/playground/sessions/{job_id}/vms` | Lancer une VM dans une session active existante. |
| `DELETE` | `/api/v1/playground/vms/{vm_id}` | Détruire une VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/actions` | Demander une action d'alimentation de VM : `start`, `stop` ou `reboot`. |
| `POST` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics` | Attacher une NIC à une VM. |
| `DELETE` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}` | Détacher une NIC d'une VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/captures` | Déclencher une capture mémoire ou disque pour une VM en cours d'exécution. |
| `GET` | `/api/v1/playground/vms/{vm_id}/captures` | Lister les captures d'une VM. |
| `GET` | `/api/v1/playground/captures/{acq_id}/events` | Diffuser les événements de capture comme Server-Sent Events. |
| `GET` | `/api/v1/playground/vms/{vm_id}/metrics` | Obtenir les métriques en direct d'une VM. |
| `GET` | `/api/v1/playground/sessions/current/metrics` | Obtenir les métriques en direct des VMs en cours d'exécution de la session actuelle. |

## Catalogue du Playground

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `GET` | `/api/v1/playground/catalog/images` | Lister les images éligibles au playground. Le paramètre optionnel `role` vaut `target` par défaut. |
| `GET` | `/api/v1/playground/catalog/workloads` | Lister les workloads compatibles avec `image_id`. |

## Overrides avancés

Échappatoires opérateur pour fournir des artefacts manuellement (enregistrés avec une provenance non vérifiée). Elles exigent le token bearer admin quand l'authentification est activée.

| Méthode | Chemin | Objectif |
| --- | --- | --- |
| `POST` | `/api/v1/jobs/{job_id}/isf/upload` | Stocker un ISF fourni par l'opérateur pour un job, en contournant l'enforcement du GNU build-id. |
| `POST` | `/api/v1/jobs/{job_id}/dump/upload` | Stocker un dump mémoire fourni par l'opérateur pour un job, en contournant la validation de complétude ELF. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Stocker un ISF fourni par l'opérateur depuis la ligne target du workbench Volatility. |

## Exemples de requête

Créer un job :

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "capture_type": "memory",
  "build_vol3_isf": false
}
```

Déclencher une capture manuelle :

```json
{
  "capture_type": "both",
  "exploit_tags": ["manual-lab"],
  "exploit_notes": "Operator-triggered capture"
}
```

Créer un réseau de playground :

```json
{
  "name": "internal",
  "subnet_cidr": "10.42.10.0/24",
  "isolated": true
}
```

Lancer une VM de playground :

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
