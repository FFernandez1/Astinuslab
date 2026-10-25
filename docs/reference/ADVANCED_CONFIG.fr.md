# Configuration avancée

Ceci est la référence détaillée des paramètres du backend. Pour le guide de configuration côté opérateur, voir [Configuration](../usage/configuration.md).

AstinusLab lit `astinuslab.toml` depuis le checkout du code source ou le répertoire
d'installation. `src/astinuslab/core/config.py` applique ensuite les surcharges
d'environnement `ASTINUSLAB_<SETTINGS_FIELD>` pour les champs du modèle aplati
`Settings`. Par exemple, `ASTINUSLAB_STORAGE_DIR` surcharge `storage_dir` et
`ASTINUSLAB_CELERY_BROKER_URL` surcharge `celery_broker_url`.

---

## 1. Paramètres globaux (`astinuslab.toml`)

Le fichier TOML est la source de vérité pour les paramètres d'infrastructure de la plateforme.

### [orchestration]
| Clé | Par défaut | Description |
| :--- | :--- | :--- |
| `api_host` | `127.0.0.1` | L'adresse à laquelle le serveur de l'API se lie. |
| `api_port` | `8000` | Le port pour l'API et l'UI. |
| `log_to_file` | `false` | Écrit aussi les enregistrements du service sous `storage/logs/`, lu par la page Logs. Les installations avec des identités séparées utilisent des [répertoires par rôle](#installed-split-service-logs). Les unités systemd l'activent car stdout va au journal ; le lanceur de développement le désactive car il redirige déjà stdout vers les fichiers à plat. |
| `log_max_bytes` | `10485760` | Limite de taille par fichier de log avant rotation. `0` désactive la rotation. |
| `log_backup_count` | `5` | Nombre de fichiers alternés conservés par service. |
| `log_format` | `text` | Format des enregistrements : `text` (format lisible affiché par la page Logs) ou `json` (un objet par enregistrement, champs de corrélation nommés). Une valeur non reconnue revient à `text`. |
| `libvirt_uri` | `qemu:///system` | Chaîne de connexion pour libvirt. |
| `libvirt_network` | `default` | Le réseau libvirt utilisé pour les VMs guest. |

### [auth]
| Clé | Par défaut | Description |
| :--- | :--- | :--- |
| `enabled` | `false` | Requiert un bearer token admin pour `/api/v1/*`, `/api/v1/playground/*` et la documentation OpenAPI. |
| `admin_api_token_hash` | `null` | Hash SHA-256 du token d'API admin. Utilisez le format `sha256:<64-hex>`. Requis quand `enabled=true`. |
| `allow_unsafe_network_bind_without_auth` | `false` | Échappatoire explicite seulement pour expériences locales contrôlées. Quand `false`, les `api_host` non-loopback requièrent auth. |
| `browser_allowed_origins` | `[]` | Origines exactes acceptées sur les mutations navigateur. Vide signifie les origines loopback sur `api_port`. Séparées par des virgules ou liste JSON dans l'environnement ; une entrée avec chemin, identifiants ou joker échoue à la validation au démarrage. |

Les releases installées activent l'authentification lors d'une installation propre :
`ops/deploy/install.sh` génère un token, n'écrit que son hash dans `astinuslab.toml` et affiche le token
une seule fois. Relancer l'installateur conserve un hash valide existant et refuse de le faire
tourner silencieusement. Une copie de travail de développement garde `enabled = false` et l'écoute
loopback. Voir [Authentification](../api/authentication.md).

Générez un hash sans afficher le token brut :

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

### [storage]
| Clé | Par défaut | Description |
| :--- | :--- | :--- |
| `storage_dir` | `storage` | `storage_dir` sélectionne la destination des futures preuves Jobs ; le modifier ou enregistrer des racines privilégiées ne copie pas les anciens octets et ne réécrit pas leurs références en base. `playground_capture_dir`, `playground_disk_dir` et `db_path` sont indépendants. Planifiez et validez une migration séparée avant de retirer une ancienne racine. |
| `db_path` | `storage/astinuslab.db` | Chemin vers la base de données SQLite. |
| `images_dir` | `images/definitions` | Chemin où sont stockées les images cloud de base. |

Les requêtes de job qui fournissent un `image_path` explicite ne sont acceptées que
lorsque le chemin résolu est à l'intérieur de `images_dir`. Le helper de stockage
libvirt applique la même vérification de préfixe de source autorisé avant de copier
une image de base dans le stockage libvirt.

### [celery]
| Clé | Description |
| :--- | :--- |
| `broker_url` | URL de connexion pour le broker Celery (par ex. `sqla+sqlite:///...`). |
| `result_backend` | Où les résultats des tâches sont stockés. |

### [vol3]
| Clé | Par défaut | Description |
| :--- | :--- | :--- |
| `enabled` | `true` | Active les flux d'ISF et d'analyse Volatility 3. |
| `symbols_path` | `vol3/symbols` | Racine du cache global réutilisable d'ISF. Les symboles Linux sont stockés sous `linux/`. |
| `dwarf2json_path` | unset | Exécutable `dwarf2json` installé par l'opérateur hors du checkout; il exige le pin `dwarf2json_sha256`. |
| `dwarf2json_sha256` | unset | Pin SHA-256 exact vérifié avant chaque exécution et transfert au guest. |
| `volatility_bin` | unset | Exécutable Volatility 3 Framework 2.28.0 installé par l'opérateur hors du checkout. |
| `default_plugins` | Plugins de processus/module Linux | Liste de plugins séparée par des virgules pour l'analyse locale au job. |
| `fetch_timeout_seconds` | `1800` | Timeout de récupération de paquets de débogage côté hôte. Les gros paquets de débogage des distros peuvent dépasser 1 Go. |
| `dwarf2json_timeout_seconds` | `600` | Timeout de build `dwarf2json` côté hôte avant tentative du fallback guest en direct. |
| `debuginfod_urls` | `[]` | Endpoints debuginfod supplémentaires. Fedora, Arch et openSUSE obtiennent des valeurs par défaut de la distro quand aucun endpoint explicite n'est configuré. |
| `allow_invasive_guest_symbol_install` | `false` | Lorsque `true`, les builds de symboles sur le guest en direct peuvent installer des paquets de débogage dans le guest. La valeur par défaut `false` autorise uniquement un `vmlinux` correspondant avec DWARF ou debuginfod et passe `VOL3_ALLOW_GUEST_PACKAGE_INSTALL=0` ; `dwarf2json` n'accepte pas une entrée contenant uniquement du BTF. |

AstinusLab vérifie `<volatility_bin> -q frameworkinfo.FrameworkInfo` avant l'exécution des plugins et enregistre la première ligne de sortie comme `vol3_version` dans `analysis-summary.json`. Gardez ce binaire épinglé au niveau du déploiement ; les déploiements testés devraient consigner le paquet ou le commit Volatility 3 installé dans leurs notes de version.

### [image_inspection]
Contrôle l'identification automatique de distro des images de disque téléversées ou
téléchargées dans `images_dir`. Lorsqu'il est activé, AstinusLab exécute
`virt-inspector` (de `libguestfs-tools`) sur chaque nouvelle image, analyse
`/etc/os-release` et consorts, et persiste le résultat comme un sidecar
`<image>.meta.json` à côté de l'image. Le sidecar est la source de vérité pour
l'auto-découverte du catalogue et l'UI d'administration des images ; les
heuristiques de nom de fichier ne sont utilisées qu'en fallback quand l'inspection
est indisponible.

| Clé | Par défaut | Description |
| :--- | :--- | :--- |
| `enabled` | `true` | Mettez `false` pour ignorer entièrement l'inspection et toujours utiliser les heuristiques de nom de fichier. |
| `timeout_seconds` | `60` | Temps d'horloge maximal pour une seule invocation de `virt-inspector`. |
| `virt_inspector_bin` | `virt-inspector` | Surchargez si le binaire est en dehors de `$PATH`. |

Si `virt-inspector` est manquant ou échoue, le sidecar est tout de même écrit avec
`source: "filename"` et un champ `inspect_error` pour que l'UI l'affiche ; rien ne
bloque. Les utilisateurs peuvent éditer les champs via la page d'administration des
images ou déclencher une réinspection — les champs enregistrés dans
`user_edited_fields` survivent aux réinspections ultérieures.

---

## 2. Chemins de catalogue

Les chemins de catalogue sont configurés dans la section `[catalogs]` de `astinuslab.toml`.
Utilisez des chemins absolus ou des chemins relatifs au checkout du code source / répertoire d'installation.

---

## 3. Paramètres du lanceur et du service

`config/astinuslab.env` est sourcé par `astinuslab.sh` et par la ligne de commande
de l'unité systemd de l'API après l'installation de release. C'est principalement un
fichier de lanceur pour des valeurs comme l'hôte et le port de bind de l'API. Comme
ces valeurs sont exportées dans l'environnement du processus, les noms qui
correspondent aussi à des champs de `Settings` peuvent aussi affecter les paramètres
du backend.

### Cache des ressources statiques (`AL_ASSET_VERSION`)

Les gabarits invalident le cache de leurs propres liens avec `?v=<version de
ressource>`, résolue depuis `AL_ASSET_VERSION`, sinon depuis le SHA court de git
et enfin `dev`. Le montage `/static` envoie `Cache-Control: no-cache` par
défaut : les navigateurs continuent de mettre en cache, mais revalident avant
usage, de sorte qu'un déploiement est pris en compte au chargement de page
suivant. Cela compte parce que les spécificateurs `import` ESM dans les modules
de la page sont délibérément non versionnés : leurs URL ne changent pas d'un
déploiement à l'autre et, sans politique explicite, le navigateur appliquerait
une fraîcheur heuristique et continuerait d'exécuter l'ancien module.

Définir `AL_ASSET_VERSION` sur une valeur choisie par le déploiement (une
étiquette de release, un identifiant de build) rend en outre les requêtes
portant ce jeton exact cacheables un an en `immutable`. Un SHA de git
n'active **pas** cela : il ne bouge pas pendant qu'un arbre de travail est
modifié, donc une copie de développement figerait des octets obsolètes sous un
jeton qu'elle réutilise ensuite. Ne le définissez que là où les ressources d'un
jeton donné ne changent plus jamais.

## 4. Réglage de la concurrence

Utilisez le lanceur ou les unités systemd installées. Gardez le worker de cycle de vie avec `--pool=threads --concurrency=1` et Beat activé ; gardez le worker séparé `analysis` à concurrence 1. Ne modifiez pas cette concurrence pour augmenter la capacité VM. Utilisez `[orchestration] max_concurrent_vms` pour l’admission ; démarrages et captures restent sérialisés. Plusieurs workers exigent une autre conception du broker, des verrous et de la réconciliation.

## 5. Frontière de sécurité

### Posture de déploiement

V1 est localhost/local-de-confiance par défaut. Gardez `api_host` lié à `127.0.0.1` pour l'usage local normal. Si vous liez à une adresse LAN, activez `[auth] enabled = true` avec `admin_api_token_hash`; AstinusLab refuse les binds non-loopback sans authentification sauf si `allow_unsafe_network_bind_without_auth = true` est défini explicitement. Même avec l'authentification d'API admin, utilisez TLS, VPN/firewall et reverse proxy pour les réseaux partagés. Les workloads guest sont intentionnellement vulnérables ; préférez des réseaux Playground isolés pour les labos hostiles.

Les recettes Playground actuelles sont des entrées JSON locales de confiance pour une configuration de labo répétable. Traitez les fichiers de recette comme de confiance car ils peuvent décrire des workloads, des services et du comportement côté guest. La prise en charge des recettes YAML reste une direction de la feuille de route.

### Frontière des mutations du navigateur

L'accessibilité en loopback n'est pas une autorisation : n'importe quelle page du navigateur de
l'opérateur peut poster vers `http://127.0.0.1:8000`, et sans authentification cette requête ne
porte aucune information d'authentification. Chaque `POST`, `PUT`, `PATCH` et `DELETE` passe donc
deux gardes, que l'authentification soit activée ou non.

- **Origin.** `Sec-Fetch-Site: cross-site`, ou une `Origin` hors de `browser_allowed_origins`, est
  refusée avec `403` / `cross_origin_forbidden`. Une requête sans en-tête `Origin` n'est pas une
  navigation et n'est pas affectée : `curl` et les SDK continuent de fonctionner.
- **CSRF.** Le cookie `astinuslab_csrf` doit être renvoyé dans l'en-tête `X-CSRF-Token` ou le champ
  `csrf_token` : `403` / `csrf_required` s'il manque, `403` / `csrf_invalid` s'il ne correspond pas.
  Les requêtes portant un bearer token admin sont exemptées, car un bearer n'est pas une autorité
  ambiante du navigateur.

Détail complet côté client dans
[Authentification](../api/authentication.md#protection-des-mutations-du-navigateur).

### Politique de stockage privilégié

Les helpers ACL installés s'exécutent en root via `sudo -n` et n'acceptent plus de racine de
confiance venant de l'appelant. Les racines proviennent de `/etc/astinuslab/acl-policy.json`, qui
doit appartenir à root, ne pas être un lien symbolique et n'être inscriptible ni par le groupe ni
par les autres, avec le schéma `astinuslab-acl-policy/v1` et les clés `allowed_roots`,
`allowed_callers` et `storage_groups` (facultatif). Une invocation dont la cible sort de toutes les
racines autorisées, ou dont l'appelant n'est pas listé, échoue au lieu d'appliquer une ACL.

Enregistrez une racine avec le script installé, jamais en éditant la politique à la main :

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab \
  --storage-root /srv/astinuslab
```

Configurez `storage_dir` pour les futurs artefacts Jobs. Cela ne migre pas les anciens fichiers ni leurs références en base. Les racines Playground et `db_path` restent indépendantes.
L'assistant ACL refuse les disques et captures Playground hors de toute racine enregistrée ; placez aussi `playground_disk_dir` et `playground_capture_dir` dans une racine enregistrée. Tant que les deux ne sont pas couverts, la disponibilité approfondie signale `acl_policy_playground_dir_mismatch` pour `root_policy`.
La racine doit appartenir à l'identité de service actuelle et au groupe stable
`astinuslab` (mode `2770`). Chaque parent doit appartenir à root et ne pas être
inscriptible par le groupe ni par les autres ; utilisez `root:astinuslab` en mode
`0750` lorsque le groupe doit le traverser. Le chemin reste ainsi utilisable après une migration vers des identités séparées.

Une racine enregistrable est absolue, existante et un répertoire sans lien symbolique. `/` et les
préfixes système (`/etc`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/boot`, `/root`, `/tmp`, `/dev`,
`/proc`, `/sys`, `/run`) sont refusés ; `/home` et `/var` sont refusés comme racines elles-mêmes,
tandis que des sous-répertoires dédiés en dessous sont acceptés. `--check` signale les divergences
de politique et d'unités sans modifier l'hôte, et aucun service ne redémarre sans `--restart`.

Dans une racine approuvée, le helper parcourt le chemin par descripteur de répertoire
(`O_NOFOLLOW | O_DIRECTORY`), vérifie par ce descripteur le propriétaire et les bits d'écriture de
chaque composant, n'accorde que les entrées de traversée et de lecture nécessaires à libvirt,
confirme que chaque entrée dans la cible résout toujours vers le même inode, puis relit l'ACL et
échoue si le masque a rogné une entrée : une entrée présente mais non *effective* est un échec. L'écriture de
groupe sur un composant n'est tolérée que pour un groupe nommé dans `storage_groups` (l'installation
met le répertoire des bases en mode `2770` pour créer les fichiers WAL et SHM).
En mode split, la racine extérieure conserve le bit sticky (`3770`) pour protéger
les magasins de paquets ; ne le retirez pas pour réparer les permissions SQLite.

`ops/deploy/uninstall.sh` supprime le fichier de politique et tout l'ensemble connu de helpers, et
échoue bruyamment s'il reste un fichier inconnu dans `/usr/local/libexec/astinuslab`.

Atteindre une capture exige `x` sur chaque répertoire de `/` jusqu'à la racine de stockage, et ce
parcours traverse des répertoires que le produit ne possède pas. L'installateur enregistre chaque
entrée qu'il ajoute dans `/etc/astinuslab/traversal-acl.manifest` et laisse de côté celles qui
existaient déjà ; la désinstallation ne retire que ce que ce manifeste nomme.

### Disposition de la version installée

Une version installée est immuable et adressée par son contenu. `ops/deploy/install.sh` prépare les
sources et construit leur environnement virtuel sous `releases/<version>-<empreinte>/`, puis déplace
le lien `current` par un renommage, de sorte qu'un service en cours ne résout jamais un instant sans
`current`. Réinstaller les mêmes sources résout vers la version déjà présente au lieu d'en préparer
une seconde copie.

```text
/opt/astinuslab/
├── releases/1.0.0-<empreinte>/ sources et environnement virtuel immuables, propriété de root
├── current -> releases/1.0.0-<empreinte>
├── storage/                    preuves, bases de données, espace du playground
├── config/                     catalogues et fichier d'environnement du lanceur
├── images/definitions/         images de base fournies par l'opérateur
├── vol3/symbols/               cache de symboles Volatility
└── astinuslab.toml             configuration de l'opérateur
```

Les chemins mutables restent à la racine d'installation et survivent à chaque bascule. Cette racine
est aussi ce que lie la politique privilégiée : `install_sudoers_astinuslab.sh` inscrit son
périphérique et son inode dans l'assistant de stockage et, comme la racine ne bouge jamais,
l'assistant continue de fonctionner après une mise à jour et après un retour arrière.

Les unités systemd s'exécutent depuis `current` et savent où vit l'état mutable grâce à
`Environment=ASTINUSLAB_DATA_DIR=/opt/astinuslab`. L'application garde les deux racines distinctes :
`base_dir` est la racine de code et `data_dir` la racine d'installation.

Mettre à jour une installation antérieure à cette disposition déplace ses sources vers une version
et supprime les copies remplacées à la racine, sans toucher aux chemins mutables. Comme `systemctl
start` ne fait rien sur une unité déjà active, un changement de version redémarre toujours les
unités.

### Bases installées avec des identités séparées {#installed-split-database-paths}

L'installateur split déplace les trois bases SQLite gérées vers
`<install_dir>/storage/databases/` et fixe leurs chemins avec `ASTINUSLAB_DB_PATH`,
`ASTINUSLAB_CELERY_BROKER_URL` et `ASTINUSLAB_CELERY_RESULT_BACKEND` dans
`config/astinuslab.env`. Les valeurs par défaut du code et les installations à
identité unique restent sous `storage/`. La racine des preuves est indépendante.
Utilisez l'évaluateur de l'environnement installé pour la maintenance ; un chemin
fixe peut sélectionner une autre base. L'action d'effacement refuse une base
déplacée avant de supprimer la base ou les clés SSH. Arrêtez les services pour
cette maintenance. L'installateur conserve les chemins explicites compatibles
et refuse les dispositions ambiguës ou non prises en charge. Conservez les
dossiers de récupération et les familles SQLite sous `backups/` ; ne déplacez pas
les fichiers et ne créez pas de liens symboliques manuellement. Réconciliez toute
transaction de disposition en attente avant de relancer l'installation.

### Journaux installés avec des identités séparées {#installed-split-service-logs}

Les services écrivent dans `storage/logs/api/api.log`,
`storage/logs/runner/runner.log` et `storage/logs/analysis/analysis-worker.log`.
Chaque worker gère la rotation de ses journaux ; l'API peut les lire et les vider
sans remplacer ses fichiers. La page Logs et `./astinuslab.sh logs` résolvent ces
chemins. Les checkouts et installations à identité unique gardent les fichiers à plat.

Les anciens journaux restent accessibles en lecture seule sous les noms
`legacy-api.log`, `legacy-runner.log` et `legacy-analysis-worker.log`.
Le nettoyage du stockage d'exécution conserve ces répertoires et leur historique.
Ne supprimez pas le marqueur et ne changez pas les propriétaires manuellement :
si l'installation échoue, elle conserve les nouveaux journaux pour récupération
avant de supprimer les comptes.

### Migration de schéma et retour arrière

Changer `current` change le code, pas le schéma de la base de données. Un retour
arrière exige un schéma compatible ou la restauration de sa sauvegarde vérifiée
antérieure à la migration, avec tous les processus d'écriture arrêtés. Un lien vers
une ancienne version arbitraire ne suffit pas. La mise à jour sauvegarde d'abord
la base et restaure les deux parties si une étape ultérieure échoue :

La sauvegarde, migration, restauration et readiness lisent `config/astinuslab.env`
via le même évaluateur sans shell : environnement du service, puis TOML, puis valeurs
par défaut. Les `ASTINUSLAB_*` ambiants de l'installateur sont supprimés et
`effective_database=...` est enregistré. Le fichier n'est pas exécuté comme root.
Ne changez pas la configuration pendant l'opération. Une migration échouée arrête
la publication de la version, même avec `--no-start`. Pour la CLI manuelle, utilisez
l'évaluateur et l'utilisateur de service réels selon [le guide canonique](ADVANCED_CONFIG.md).


1. **Mise au repos.** Toutes les unités sont arrêtées, de sorte que le schéma n'est jamais migré
   avec un écrivain attaché et que rien ne s'exécute pendant que `current` bouge.
2. **Sauvegarde.** La base est copiée via l'API de sauvegarde de SQLite après un
   `wal_checkpoint(TRUNCATE)` ; une simple copie de fichier peut manquer des lignes validées, car la
   base fonctionne en mode WAL. La copie est vérifiée par `integrity_check`, créée en mode `0600`,
   et son chemin, son SHA-256 et sa révision sont enregistrés. L'installateur refuse de migrer si
   cette étape échoue.
3. **Migration**, puis démarrage des services et contrôle de disponibilité approfondi.
4. **Retour arrière en cas d'échec.** La base est restaurée *avant* le redémarrage des services, et
   `current` revient à la version précédente. Une restauration échouée nomme le fichier de
   sauvegarde. Le retour arrière vérifie ensuite la version restaurée comme une mise à jour vérifie la
   nouvelle : `systemctl` rend la main dès qu'il a démarré les unités, avant que la version
   restaurée ne serve, et un hôte qui ne revient pas serait sinon annoncé comme rétabli.

La bascule est une séquence ordonnée, identique pour chaque installation et chaque mise à jour :
arrêter toutes les unités, déplacer `current` par un renommage, `daemon-reload`, puis **redémarrer**
chaque unité. `restart` est le seul verbe : `systemctl start` ne fait rien sur une unité déjà active,
donc une mise à jour qui l'utilisait laissait les anciens processus attachés à une version déjà
remplacée.

`--no-start` arrête les services, déplace `current` et les laisse arrêtés et activés. Le contrôle de
disponibilité approfondi est ignoré avec un avertissement indiquant que la bascule n'est pas
vérifiée.

Les sauvegardes vivent dans `/opt/astinuslab/backups/`, mode `0700` — à côté de l'arborescence de
stockage, jamais dedans : c'est de là que sont tirés les paquets de preuves et elle contient
`storage/ssh_keys/`.

`downgrade` exige une révision explicite ; les cibles relatives comme `-1` et les alias `head`,
`heads` et `base` sont refusés, car `-1` se résout par rapport au head du moment.

Revenir en arrière sur le schéma **n'annule pas** les preuves déjà écrites : les captures, les
manifestes et les fichiers SHA-256 publiés restent en place.

### Propriété de l'arbre d'installation

Le code et `.venv` restent la propriété de l'installateur et ne sont pas inscriptibles par
l'identité d'exécution. `ops/deploy/install.sh` réconcilie cela aussi lors des mises à jour : il retire
les ACL, efface les bits d'écriture groupe/autres et setgid, et attribue `src`, `ops`, `tools`,
`deploy`, `guest`, `.venv`, `pyproject.toml` et les lockfiles à `root:root`
**dans la version active**. Seuls
`storage`, `config`, `images/definitions` et `vol3/symbols` appartiennent à l'exécution.

Les unités systemd imposent la même forme à l'exécution : `ReadOnlyPaths` couvre le répertoire
d'installation et `UMask=0007` évite des modes lisibles par tous sur les nouvelles preuves tout en
préservant l'écriture de groupe dont les fichiers SQLite partagés ont besoin. Chaque service reçoit
uniquement les chemins mutables que son propre code écrit.

| Unité | `ReadWritePaths` | Raison |
|---|---|---|
| `astinuslab-api` | storage, `config`, `images/definitions` | prépare les images téléversées et téléchargées, réécrit le catalogue YAML |
| `astinuslab-runner` | storage, `images/definitions`, `vol3/symbols`, `/var/lib/libvirt/images` | crée les overlays de VM et construit les symboles ISF ; l'assistant de stockage sudo-root s'exécute dans l'espace de noms de montage de cette unité |
| `astinuslab-analysis` | storage, `vol3/symbols` | ne consomme que la file d'analyse ; il ne crée jamais de VM et n'atteint donc aucun chemin de virtualisation |

Les trois unités définissent aussi `LockPersonality`, `ProtectKernelTunables`,
`ProtectKernelModules`, `ProtectKernelLogs` et `ProtectControlGroups`. `RestrictSUIDSGID` n'est
délibérément **pas** défini : son filtre rejette tout `chmod` dont le mode porte setgid, et
l'arborescence de stockage est setgid pour que le groupe de service soit hérité — l'activer
transforme chaque capture mémoire en `Operation not permitted`. `NoNewPrivileges` reste réservé à
l'API, car il fait échouer tout appel `sudo -n` ; l'identité d'analyse séparée l'obtient parce
qu'elle n'a aucun sudo.

Une racine de stockage sous `/home` rend toutes les unités avec
`ProtectHome=tmpfs` et un montage `BindPaths=` pour cette racine. Les autres
répertoires personnels restent masqués, tandis que le stockage configuré reste
accessible en écriture.

### Politique réseau des téléchargements d'images

Les téléchargements par URL sont validés au niveau du socket, pas seulement de l'URL. AstinusLab
résout lui-même le nom et, avec la politique par défaut `network_policy = "safe"`, refuse toute
réponse qui n'est pas une adresse globale — loopback, RFC1918, link-local, unique-local et l'adresse
de métadonnées `169.254.169.254`, y compris les formes IPv4-mapped. Il se connecte ensuite à cette
IP épinglée, vérifie `getpeername()` avant d'envoyer la requête et valide le certificat TLS contre
le hostname d'origine. Un rebind DNS entre validation et connexion ne peut pas changer le pair.

Les redirections sont suivies manuellement : chaque saut est revalidé par la même politique, cinq
sauts au maximum, et une URL répétée est traitée comme une boucle et échoue. `source_policy` choisit
les domaines acceptables — `official_only` (les miroirs de distribution intégrés), `allowlist`
(`allowed_domains`, qui ne peut être vide) ou une politique sans restriction — et un domaine refusé
échoue avec `403` avant toute connexion. Les URL portant des identifiants, un schéma autre que
`http(s)` ou le port `0` sont refusées, et `max_size_gb` borne le transfert.

Chaque téléchargement est préparé dans un répertoire privé par tentative (`images/.staging/<uuid>`,
mode `0700`) et publié en liant le fichier terminé dans `images_dir` : le lien échoue si le nom est
déjà pris, et l'inode publié est comparé à l'inode préparé. Deux téléchargements concurrents de la
même image ne peuvent donc ni partager ni détruire le fichier partiel de l'autre, et le nettoyage
est épinglé à l'inode du répertoire de l'opération elle-même.
