# Développement de workloads et scénarios

Ce guide explique comment créer, configurer et déboguer des workloads et des scénarios (tâches automatisées) dans AstinusLab.

## Vue d'ensemble

Une **VM Task** dans AstinusLab peut être soit un workload persistant, soit un script de scénario autonome :

1.  **Workload** : Un ensemble de services ou de configurations installés pour un usage interactif (par ex. une application web vulnérable). Utilisé avec le mode `manual_trigger`.
2.  **Scénario** : Une configuration de cycle de vie qui détermine quand une capture se produit. Les scénarios personnalisés utilisent le mode `post_script` pour exécuter une tâche autonome et capturer immédiatement à son achèvement.

---

## 1. Créer un workload unique

### Étape A : Créer le script d'installation
Placez votre script bash dans `guest/workloads/<workload-id>/install.sh`.

**Recommandations pour les scripts d'installation :**
*   **Utilisez `set -euo pipefail`** : Assurez-vous que le script se termine en cas d'erreur.
*   **Non interactif** : Utilisez `export DEBIAN_FRONTEND=noninteractive` et les flags `-y` pour les gestionnaires de paquets (par ex. `apt-get install -y`).
*   **Idempotence** : Les scripts devraient idéalement pouvoir être exécutés plusieurs fois, bien qu'AstinusLab les exécute généralement une fois sur une image propre.
*   **Logging** : Utilisez `echo "[my-workload] message"` pour fournir des mises à jour de progression qui apparaissent dans l'UI d'AstinusLab.
*   **ShellCheck** : AstinusLab exécute `shellcheck` sur votre script avant l'exécution. Évitez les variables inutilisées ou les erreurs de syntaxe.

**Exemple (`guest/workloads/hello-world/install.sh`) :**
```bash
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[hello-world] Installing Nginx..."
sudo apt-get update -qq
sudo apt-get install -y nginx

echo "[hello-world] Configuring landing page..."
echo "<h1>AstinusLab Workload Ready</h1>" | sudo tee /var/www/html/index.html

echo "[hello-world] Installation complete."
```

### Étape B : Enregistrer dans `config/catalog/workloads.yaml`
Ajoutez une entrée à la liste `workloads` :

```yaml
- id: hello-world
  enabled: true
  display_name: Hello World Nginx
  tags:
    - web
    - tutorial
  install_script: guest/workloads/hello-world/install.sh
  health_check:
    url_path: /
    port: 80
    expected_status: 200
    timeout_seconds: 60
  services:
    - name: HTTP
      port: 80
      url_path: /
  compatibility:
    include:
      - family: debian
```

---

## 2. Créer un stack de workloads

Les stacks vous permettent de combiner des workloads existants sans dupliquer les scripts.

**Exemple (`config/catalog/workloads.yaml`) :**
```yaml
- id: lamp-stack
  type: stack
  enabled: true
  display_name: Full LAMP Stack
  components:
    - mysql-base
    - apache-php
    - wordpress-6.4
  compatibility:
    include:
      - family: debian
```

---

## 3. Référence de configuration

| Champ | Type | Description |
| :--- | :--- | :--- |
| `id` | string | Identifiant unique utilisé dans l'API et la DB. |
| `type` | string | `stack` pour les workloads multi-composants, à omettre pour un seul. |
| `display_name` | string | Nom lisible affiché dans l'UI. |
| `install_script` | path | Chemin relatif à la racine du dépôt. |
| `health_check` | object | (Optionnel) URL à sonder pour vérifier que le workload est prêt. |
| `services` | list | Services à afficher dans le panneau « Lab Services » de l'UI. |
| `cve_ids` | list | CVE associés à ce workload (pour la recherche). |
| `network_isolation` | bool | Si true, bloque l'accès sortant de la VM à internet/LAN après le provisioning. |
| `compatibility` | object | Règles pour faire correspondre ce workload aux images d'OS cible. |

### Règles de compatibilité
Les workloads peuvent être restreints à des cibles spécifiques selon `family` (par ex. `debian`, `redhat`), `distro` (par ex. `ubuntu`) ou `version`.

```yaml
compatibility:
  include:
    - family: debian
      version: '22.04'
  exclude:
    - distro: kali
```

---

## 4. Le cycle de vie du provisioning

Lorsqu'un utilisateur lance un workload :
1.  **Préparation de l'image** : AstinusLab crée un overlay QCOW2 de l'image de base cible.
2.  **Démarrage de la VM** : La VM est démarrée avec libvirt.
3.  **Attente SSH** : Le runner attend que le guest devienne accessible via SSH.
4.  **Synchronisation du workload** : Le dépôt est monté dans le guest via `virtiofs` (ou copié via SSH si le fallback est utilisé).
5.  **Exécution** : Le `install_script` est exécuté dans le guest.
6.  **Health check** : Le runner sonde l'URL `health_check` jusqu'à ce qu'elle renvoie le `expected_status`.
7.  **Snapshot** : Un snapshot libvirt (`lab-clean`) est créé.
8.  **Prêt** : Le job entre dans l'état `waiting_for_capture` et est remis à l'utilisateur.

---

## 5. Créer des scénarios personnalisés (tâches)

Alors que les workloads sont destinés aux labos interactifs, les **Scénarios** vous permettent d'automatiser une tâche (par ex. simuler une attaque ou un événement système) et de capturer l'état de la VM automatiquement une fois terminé.

### Étape A : Créer le script du scénario
Les scripts de scénario sont des scripts bash standard placés dans `guest/scripts/`.

**Exemple (`guest/scripts/simulate-infection.sh`) :**
```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[task] Simulating a malicious process..."
# In a real scenario, this might download a sample or execute a payload
sleep 10
nohup python3 -m http.server 9999 &

echo "[task] Simulation complete. Signalling for capture."
```

### Étape B : Enregistrer dans `config/catalog/scenarios.yaml`
Ajoutez un nouveau scénario avec `mode: post_script` :

```yaml
- id: auto-infect-capture
  enabled: true
  mode: post_script
  description: Simulates an infection and captures immediately
  script:
    enabled: true
    path: guest/scripts/simulate-infection.sh
    timeout_seconds: 300
    fail_job_on_nonzero_exit: true
```

### Quand utiliser des scénarios vs. des workloads ?
*   **Utilisez un Scénario** lorsque vous voulez une tâche « fire and forget » qui mène à une capture automatisée.
*   **Utilisez un Workload** lorsque vous voulez provisionner un environnement stable pour une exploration manuelle ou plusieurs points de capture.

---

## 6. Dépannage et débogage

### Voir les journaux
*   **Côté hôte** : Consultez `storage/logs/runner.log` pour les erreurs d'exécution du script et les codes de sortie.
*   **Côté UI** : Le « Event Log » sur la page de détail du Job affiche la sortie en temps réel du script d'installation.

### Provisioning échoué
Si un workload échoue :
1.  Vérifiez si `shellcheck` a signalé des erreurs dans le Event Log.
2.  Consultez le « Error Summary » pour les codes de sortie.
3.  Mettez `keep_failed_vm = true` dans `config/catalog/targets.yaml` pour la cible que vous testez. Cela empêche AstinusLab de supprimer la VM en cas d'échec, vous permettant de vous connecter en SSH manuellement et de déboguer :
    ```bash
    ssh -i storage/ssh_keys/key <guest_user>@<guest_ip>
    ```

### Problèmes courants
*   **Permission Denied** : Assurez-vous que votre script utilise `sudo` pour les commandes nécessitant root, car les scripts s'exécutent en tant que `guest_user` par défaut (généralement `ubuntu` ou `cloud-user`).
*   **Dépendances manquantes** : Rappelez-vous que les images cloud de base sont souvent minimales. Vous devrez peut-être exécuter `apt-get update` avant d'installer des paquets.
*   **Timeouts réseau** : Si votre script télécharge de gros fichiers, assurez-vous que le `timeout_seconds` dans le `health_check` est assez élevé pour couvrir tout le temps d'installation.
