# Exécution locale

L’hôte testé est Ubuntu 24.04 LTS x86_64 avec KVM. Les autres recettes sont expérimentales. L’espace de téléchargement est distinct de la capacité opérationnelle : le smoke strict avertit sous 150 Go libres ou 16 Go de RAM ; chaque laboratoire nécessite son propre budget.

Cette page est le guide opérationnel pour installer et exécuter AstinusLab à partir de ce dépôt en tant que service systemd.

Si vous prévoyez de modifier le code ou d'exécuter des tests, consultez [Configuration de développement](../development/setup.md).

## Prérequis

### Paquets système

La machine hôte doit exécuter Linux et prendre en charge la virtualisation matérielle KVM. Installez les paquets requis pour votre distribution :

**Sous Debian / Ubuntu :**
```bash
sudo apt update
sudo apt install -y   qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients libvirt-dev   virtinst cloud-image-utils cloud-init pkg-config python3 python3-venv   python3-pip rsync openssh-client sudo

# Optional but recommended for image auto-detection:
sudo apt install -y libguestfs-tools
# Required for ISF symbol extraction from Fedora/RHEL targets:
sudo apt install -y rpm2cpio cpio
```

**Sous Fedora / RHEL :**
```bash
sudo dnf install -y   qemu-kvm qemu-img libvirt libvirt-client libvirt-devel virt-install   cloud-utils pkgconf python3 python3-pip rsync openssh-clients sudo   policycoreutils-python-utils

sudo dnf install -y libguestfs-tools rpm-build cpio
```

**Sous Arch Linux :**
```bash
sudo pacman -S --needed   qemu-desktop libvirt virt-install cloud-guest-utils pkgconf   python python-pip rsync openssh sudo cpio libguestfs
```

### Contrôle d'accès obligatoire (AppArmor / SELinux)

Libvirt utilise des mécanismes MAC pour confiner les VM. AstinusLab exige que les VM libvirt puissent lire et écrire dans le répertoire de stockage (`/opt/astinuslab/storage` par défaut). Avec AppArmor, `ops/deploy/install.sh` modifie le modèle libvirt ; avec SELinux, il attribue au stockage le type `svirt_image_t` et exécute `restorecon`.

### Appartenance aux groupes

```bash
# Add your user to the libvirt and kvm groups
sudo usermod -aG libvirt,kvm "$USER"
# Log out and back in for group membership to take effect
```

Vérifiez que KVM est utilisable :

```bash
virsh -c qemu:///system list
```

### Python

Python 3.12 ou 3.13 est requis (`python3 --version`). AstinusLab 1.0.0 n'est
testé que sur ces deux runtimes. La ligne `requires-python = ">=3.12"` de
`pyproject.toml` est le minimum d'empaquetage, et non une déclaration de prise en
charge de 3.14 ou ultérieur.

### Clé SSH

AstinusLab utilise une paire de clés ED25519 pour se connecter aux VM invitées. Le script d'installation peut en générer une automatiquement, ou vous pouvez utiliser une clé existante. En mode développement, la clé doit être disponible avant de lancer des Jobs (elle peut être configurée dans la section Sécurité de l'interface).

## Installation

Exécutez le programme d'installation en tant que root depuis une copie de travail propre. `SERVICE_USER` désigne l'utilisateur local qui possédera le service et recevra l'autorisation sudoers `NOPASSWD` pour les utilitaires libvirt :

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh   --install-dir /opt/astinuslab   --service-user "$USER"
```

Pour utiliser un stockage hors de l'installation par défaut, enregistrez la
racine en tant que root :

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab --storage-root /srv/astinuslab
```

Configurez `[storage] storage_dir` ou `ASTINUSLAB_STORAGE_DIR` avec le même chemin avant son enregistrement. Seule la destination des futures sorties de Jobs change : les preuves existantes et leurs références en base ne sont pas déplacées. Les captures et disques Playground, ainsi que `db_path`, ont des racines indépendantes. Conservez les anciennes racines jusqu'à une migration planifiée vérifiant fichiers, sidecars, références et accès des services. L'enregistrement seul ne migre pas les données et ne permet pas leur suppression ; voir [Politique de stockage](../reference/ADVANCED_CONFIG.md#politique-de-stockage-privilegie).

`--check` signale les divergences sans modifier l'hôte ; le redémarrage n'a
lieu qu'avec `--restart`. Les unités sont rendues avec la racine configurée.
Sous `/home`, chaque unité de service utilise `ProtectHome=tmpfs` et un montage
`BindPaths=` pour la racine configurée.
Sa dernière ligne est `READY`, `READY WITH WARNINGS` ou `NOT READY` ; une exigence manquante renvoie un code non nul.

Options utiles du programme d'installation :

```bash
# Preflight only; no writes, no root required for read-only checks
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"

# Update an existing release tree and restart units
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --restart

# Install/update without enabling or starting services
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --no-enable --no-start

# Noninteractive SSH-key behavior
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --yes
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --no-ssh-key
```

Après une installation ou une mise à jour, vérifiez l'état :

```bash
cd /opt/astinuslab/current
./astinuslab.sh smoke --require-running
./astinuslab.sh guide-volatility  # facultatif ; guide en lecture seule
```

Interface web : `http://localhost:8000`.

Commandes utiles :

```bash
# Logs
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f

# Lifecycle
sudo systemctl restart astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo systemctl stop    astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo systemctl disable astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # do not start at boot

# Update after a git pull (re-runs rsync, venv, helpers, sudoers, units)
sudo INSTALL_DIR=/opt/astinuslab SERVICE_USER=$USER bash ops/deploy/install.sh

# Remove
sudo bash ops/deploy/uninstall.sh
```

## Images cloud

AstinusLab nécessite des images cloud standard pour provisionner les machines virtuelles. En mode
publication, `/opt/astinuslab/current/ops/images/download_images.sh` les enregistre dans la racine de
données stable `/opt/astinuslab/images/definitions` ; en développement, le script utilise
`images/definitions/` dans le checkout. Un argument facultatif remplace le répertoire de destination.
Les téléchargements représentent environ 3 Go compressés ; gardez au moins 10 Go libres.

```bash
# Mode publication avec une seule identité
cd /opt/astinuslab/current && sudo -u "$USER" ./ops/images/download_images.sh

# Mode publication avec --split-users
cd /opt/astinuslab/current && sudo -u astinuslab-runner ./ops/images/download_images.sh

# Checkout de développement
./ops/images/download_images.sh
```

Vous pouvez également télécharger manuellement des images dans `images/definitions/`. Consultez [Fichiers générés et d'exécution](../development/GENERATED_RUNTIME_FILES.md) pour connaître les règles de nettoyage et de traçabilité.

## Docker ou Compose

Ce dépôt ne contient ni Dockerfile ni fichier Compose. Les flux de travail pris en charge sont le mode de publication via `ops/deploy/install.sh` avec systemd et le mode développement via `./astinuslab.sh`.

## Erreurs courantes

| Symptôme | Cause probable | Correction |
| --- | --- | --- |
| `failed to connect to the hypervisor` | L'utilisateur n'a pas accès à libvirt | Ajoutez l'utilisateur à `libvirt,kvm`, puis déconnectez-vous et reconnectez-vous. |
| Le Runner s'arrête avec une erreur sudo sans mot de passe | Les scripts utilitaires ou la règle sudoers manquent | Exécutez `sudo ./astinuslab.sh setup-host`. |
| La VM démarre, mais SSH ne se connecte jamais | Nom d'utilisateur du catalogue ou profil cloud-init incorrect | Vérifiez `default_username`, `cloud_init_profile` et `inject_network_config` dans `targets.yaml`. |
| L'image apparaît comme unknown | `virt-inspector` manque ou la détection par nom de fichier a échoué | Installez `libguestfs-tools` ou ajoutez des métadonnées explicites au catalogue. |
| `mixed/conflict` dans `./astinuslab.sh status` | Les modes développement et systemd sont actifs en même temps | Arrêtez l'un des modes avant de lancer des Jobs. |
| Dépendance de documentation manquante | `mkdocs` n'est pas installé dans `.venv` | Exécutez `.venv/bin/python -m pip install -e ".[docs]"`. |
| `smoke: config missing/unreadable: config/astinuslab.env` | Le fichier initialisé manque ou n'est pas lisible | Relancez `./astinuslab.sh setup` ; la configuration existante n'est pas remplacée. |
L’installateur conserve le code et `.venv` en lecture seule pour les services; les écritures d’exécution se limitent à storage, la configuration mutable, les définitions d’images et les symboles Volatility.

Une installation propre active l'authentification admin et affiche le token lors de sa création puis dans le bloc final ;
enregistrez-le car seul son hash est conservé. Une mise à jour conserve
le hash existant et refuse de le faire tourner silencieusement : ne passez
`--allow-unauthenticated-local` que pour une installation explicitement de développement. La mise à
jour conserve aussi la racine de stockage enregistrée avec `configure-privileged-roots.sh` :
l'installateur la relit depuis la politique au lieu de revenir à `<install-dir>/storage`.

Pour diriger les futures sorties de Jobs vers une autre racine, configurez `storage_dir` et enregistrez-la avec `configure-privileged-roots.sh`. Cela ne migre pas les preuves existantes. Ne désinstallez pas pour changer de racine : la désinstallation supprime la politique ACL, les helpers et la règle sudoers.

Revenir volontairement à une version antérieure exige un schéma compatible ou la restauration d'une sauvegarde vérifiée précédant la migration, tous les processus écrivains étant arrêtés. Réinstaller le code ne rétablit pas l'ancien schéma. La récupération automatique après échec d'une mise à jour restaure sa propre sauvegarde ; c'est une opération distincte. Préservez les preuves et réconciliez les enregistrements créés après la sauvegarde. Suivez [Migration et restauration](../reference/ADVANCED_CONFIG.md#migration-de-schema-et-retour-arriere).