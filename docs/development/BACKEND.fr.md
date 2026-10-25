# Architecture du backend et développement

Le backend d'AstinusLab est un système asynchrone à haute performance conçu pour gérer le cycle de vie complet des VMs de labo forensique.

## Stack technique

*   **Langage** : Python 3.12 / 3.13 (matrice testée)
*   **Framework web** : FastAPI (API REST asynchrone + templates d'UI Jinja2)
*   **File de tâches** : Celery (orchestration asynchrone de jobs en arrière-plan)
*   **Base de données** : SQLite (ORM SQLAlchemy)
*   **Virtualisation** : libvirt (QEMU/KVM)
*   **Communication** : SSH (pour l'interaction avec le guest), virtiofs (pour le partage de fichiers hôte-guest)
*   **Inspection d'images** : `virt-inspector` de `libguestfs-tools` lorsqu'il est disponible, avec des heuristiques de nom de fichier en fallback

---

## Prérequis de l'hôte pour l'extraction d'ISF

L'extraction de symboles ISF (Volatility 3) nécessite les paquets suivants sur
l'hôte. L'orchestrateur exécute une vérification préflight au démarrage du job et
abandonne avec un message actionnable si l'un d'eux manque.

```bash
sudo apt install \
  rpm2cpio cpio \
  libguestfs-tools \
  qemu-utils \
  libvirt-clients
```

`rpm2cpio` est requis pour extraire `vmlinux` des RPM kernel-debuginfo de
Fedora/RHEL. `libguestfs-tools` fournit `virt-customize` et `virt-inspector`.
`qemu-utils` fournit `qemu-img`. `libvirt-clients` fournit `virsh`.

---

## Vue d'ensemble de l'architecture

```text
+-----------------------+
|  Frontend UI / HTMX   |
+-----------------------+
            |
            v
+-----------------------+
|      FastAPI API      |
+-----------------------+
     |             |
     v             v
+----------+  +-------------------------------+
|  SQLite  |  |  Broker Celery (file SQLite)  |
+----------+  +-------------------------------+
                               |
                               v
              +-------------------------------+
              |  Celery Worker / Orchestrator  |
              +-------------------------------+
                 |             |             |
                 v             v             v
          +------------+  +----------+  +----------+
          |  libvirt / |  | VM Guest |  |  SQLite  |
          |    QEMU    |  | via SSH  |  |          |
          +------------+  +----------+  +----------+
```

Les paramètres de l'application sont chargés depuis `astinuslab.toml` dans le
checkout du code source / répertoire d'installation. `src/astinuslab/core/config.py`
applique aussi les surcharges d'environnement `ASTINUSLAB_<SETTINGS_FIELD>` pour les
champs du modèle aplati `Settings`.

### Composants principaux

| Composant | Responsabilité |
| :--- | :--- |
| **API (`api/routes.py`)** | Gère les requêtes REST, la validation d'entrée et la soumission de jobs. |
| **Services de domaine (`jobs/job_services.py`, `forensics/forensic_services.py`, `forensics/case_services.py`)** | Orchestrent la logique métier à travers les repositories et les tâches. |
| **Repository (`src/astinuslab/db/repositories.py`)** | Gère la persistance en DB et applique la **machine à états de Job**. |
| **Flux d'exécution de Job (`orchestration/runners/boot_capture_runner.py`, `orchestration/deps.py`)** | Le moteur du cycle de vie de la VM (Boot, Wait, Capture, Teardown). |
| **Cycle de vie VM (`virtualization/vm_lifecycle.py`)** | Génération de XML de domaine libvirt bas niveau et gestion des overlays de disque. |
| **Support des workloads (`workloads/workload_support.py`)** | Gère la livraison des scripts, l'exécution et la surveillance de santé. |
| **Package Playground (`playground/`)** | Gère l'assemblage de l'API Playground, les routes, l'orchestration, les helpers de persistance, les workloads, la réconciliation et les recettes JSON. Les façades racine `playground_*.py` ont été supprimées. |

---

## 1. La machine à états de Job

La machine à états est la source de vérité pour toutes les activités du job. Les transitions sont strictement appliquées dans `JobRepository.transition_job_status()`.

**États clés :**
*   `queued` : État initial.
*   `preparing_image` : Création des overlays QCOW2 et des seeds cloud-init.
*   `waiting_for_guest_access` : VM démarrée, en attente de résolution d'IP et de SSH.
*   `mounting_output_share` : Préparation du montage virtiofs ou SSH pour la livraison des artefacts.
*   `generating_isf` : Construction ou réutilisation d'un ISF Volatility 3 sans capturer mémoire/disque.
*   `running_scenario_script` : Exécution de scripts personnalisés post-boot ou de scénario.
*   `provisioning_workload` : Exécution des scripts d'installation et vérification de la santé.
*   `waiting_for_capture` : VM prête pour l'opérateur.
*   `capture_requested` : Tâche d'acquisition déclenchée via l'UI ou l'API.
*   `acquiring_memory` : Exécution de `virDomainCoreDump` (capture RAM).
*   `acquiring_disk` : Exécution de `qemu-img convert` (capture disque).
*   `publishing_artifact` : Finalisation et déplacement des artefacts capturés vers le stockage de l'hôte.
*   `cancel_requested` : Annulation en attente avant le démantèlement complet. Notez que les actions `cancel` et `force-fail` terminent désormais immédiatement et de force tout sous-processus OS actif sous-jacent s'exécutant dans le worker pour ce job.
*   `completed` / `failed` / `cancelled` : États terminaux.

---

## 2. Exécution des tâches en arrière-plan

Les opérations de longue durée (comme le démarrage de VM ou le dump mémoire) sont dispatchées comme des tâches Celery.

*   **Broker** : Basé sur SQLite (via `kombu.transport.sqlite`).
*   **Définition de tâche** : Située dans `src/astinuslab/jobs/celery_app.py`.
*   **Mapping** : `TASK_MAP` mappe les types d'action aux fonctions Python.

---

## 3. Communication hôte-guest

AstinusLab évite les agents « lourds » à l'intérieur du guest. À la place, il utilise :
*   **SSH** : Transport principal pour l'exécution de commandes.
*   **virtiofs** : Partage de répertoires de l'hôte à haute performance (monté sur `/mnt/astinuslab` dans le guest).
*   **QEMU Guest Agent (optionnel)** : Utilisé pour les arrêts propres et la collecte avancée de métadonnées s'il est présent.
*   **Événements libvirt** : Utilisés pour détecter les changements d'état d'alimentation de la VM.

---

## 4. Flux de développement

### Ajouter un nouvel endpoint d'API
1.  Définissez le schema Pydantic dans `src/astinuslab/api/schemas.py`.
2.  Ajoutez la route à `src/astinuslab/api/routes.py`.
3.  Implémentez la logique dans `src/astinuslab/jobs/job_services.py` (Jobs), `src/astinuslab/forensics/forensic_services.py` (Acquisitions), `src/astinuslab/playground/` (Playground).
4.  Ajoutez des tests dans `tests/python/api/test_api.py`.

### Ajouter une nouvelle action en arrière-plan
1.  Implémentez la logique dans `src/astinuslab/orchestration/runners/boot_capture_runner.py` ou un module de support.
2.  Ajoutez une chaîne de type d'action à `TASK_MAP` dans `src/astinuslab/jobs/celery_app.py`.
3.  Assurez-vous que la machine à états du job dans `src/astinuslab/db/repositories.py` prend en charge les transitions requises.

---

## 5. Mandats de sécurité

*   **Assainissement des artefacts** : Utilisez toujours `src/astinuslab/jobs/job_services.py:safe_artifact_path` pour empêcher le path traversal lors du téléchargement des dumps.
*   **Gestion des identifiants** : Ne stockez jamais de mots de passe en clair dans la DB ; utilisez le `Keystore` pour les clés SSH.
*   **Isolation réseau** : Utilisez les hooks `iptables` dans `workload_support.py` pour garantir que les VMs vulnérables ne puissent pas attaquer l'hôte ou le LAN.

---

## 6. Tests

Exécutez la suite de tests complète avant de commiter :
```bash
.venv/bin/python -m pytest tests/python/
```

Tests de modules individuels :
```bash
.venv/bin/python -m pytest tests/python/jobs/test_repositories.py
.venv/bin/python -m pytest tests/python/orchestration/test_lab_orchestrator.py
```
