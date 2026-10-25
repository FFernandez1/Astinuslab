# Configuration

Les packages portables sont désactivés par défaut, expérimentaux et non acceptés pour 1.0.0. Activez-les uniquement pour des essais contrôlés ; consultez les [limites d’acceptation](../user/PORTABLE_LABS.md).

AstinusLab a trois couches de configuration :

| Couche | Fichiers | Objectif |
| --- | --- | --- |
| Paramètres statiques | `astinuslab.toml`, variables d'environnement `ASTINUSLAB_*`, `config/astinuslab.env` | Paramètres de stockage du backend, libvirt, catalogue, Celery, Volatility 3, authentification et bind de l'API. |
| Paramètres d'exécution | Table `app_settings` de la base de données de l'application (`db_path`) | Rétention, seuil d'espace libre et paramètres des images modifiés dans l'UI. |
| Catalogues | `config/catalog/*.yaml` | Images cibles, scénarios, workloads, images d'attaquant, Network Nodes et profils. |

## Précédence de configuration

Les paramètres statiques utilisent cette chaîne : variables d'environnement `ASTINUSLAB_*`, puis `astinuslab.toml`, puis valeurs par défaut du code. `config/astinuslab.env` est aussi exporté vers l'API server, le worker et la CLI DB via `astinuslab.sh` et systemd ; ses valeurs participent donc à la couche de plus haute précédence.

Les paramètres de l'UI sont conservés dans la table `app_settings` de la base configurée. La rétention et le seuil d'espace libre utilisent la clé `app_settings` ; les images utilisent leurs propres clés. Le chargeur actuel ne lit ni n'importe `<storage_dir>/settings.json` : modifier ou copier ce fichier ne change ni ne sauvegarde la politique active. Conservez une sauvegarde vérifiée de la base, de la configuration et des preuves ; changer `storage_dir` ne déplace pas `db_path`. Voir [Migration et restauration](../reference/ADVANCED_CONFIG.md#migration-de-schema-et-retour-arriere).

Les installations split fixent les bases SQLite gérées dans
`<install_dir>/storage/databases/` via `config/astinuslab.env`. Les tableaux
décrivent les valeurs par défaut du code ; la maintenance doit charger les
surcharges installées. Voir [les chemins installés](../reference/ADVANCED_CONFIG.md#installed-split-database-paths).

## Paramètres de l'application

`src/astinuslab/core/config.py` charge `astinuslab.toml` depuis la racine du dépôt ou de l'installation. Les chemins relatifs sont résolus depuis cette racine. Les valeurs peuvent aussi être remplacées par des variables d'environnement `ASTINUSLAB_<SETTINGS_FIELD>`, où le nom du champ est le champ aplati de `Settings`, par exemple `ASTINUSLAB_STORAGE_DIR` ou `ASTINUSLAB_CELERY_BROKER_URL`. Les sections ou clés TOML inconnues font échouer le démarrage ; gardez les valeurs personnalisées dans les clés documentées ou les overrides `ASTINUSLAB_<SETTINGS_FIELD>`.

Le `astinuslab.toml` par défaut contient ces sections :

| Section | Paramètres clés |
| --- | --- |
| `[orchestration]` | `api_host`, `api_port`, `api_log_level`, `log_to_file`, `log_max_bytes`, `log_backup_count`, `log_format`, `libvirt_uri`, `libvirt_network`, `max_concurrent_vms` |
| `[storage]` | `storage_dir`, `db_path`, `images_dir`, `playground_disk_dir`, `playground_capture_dir`, `libvirt_image_base_dir` |
| `[catalogs]` | `targets`, `scenarios`, `workloads`, `attackers`, `network_nodes` |
| `[vol3]` | `enabled`, `symbols_path`, `dwarf2json_path`, `volatility_bin`, limites des fournisseurs et URLs de mirror |
| `[vol3.provider_timeouts]` | timeouts spécifiques aux fournisseurs pour `prebuilt_isf`, `debuginfod`, `host_fetch` et `live_guest` |
| `[celery]` | `broker_url`, `result_backend`, paramètres de réconciliation |
| `[image_inspection]` | `enabled`, `timeout_seconds`, `virt_inspector_bin` |
| `[auth]` | `enabled`, `admin_api_token_hash`, override de sécurité pour bind réseau |

`playground_disk_dir` et `playground_capture_dir` valent par défaut `<repo>/storage/playground/disks` et `<repo>/storage/playground/captures`, même si les clés TOML sont absentes. Ils doivent être différents et ne doivent pas être imbriqués. Le modèle de paramètres rejette les chemins imbriqués car la récupération au démarrage nettoie les overlays de disque et ne doit jamais nettoyer les artefacts de capture. `libvirt_image_base_dir` vaut par défaut `/var/lib/libvirt/images/astinuslab` car libvirt/qemu doit traverser ce chemin système.

## Environnement du lanceur

Copiez le fichier d'exemple lorsque vous devez surcharger les valeurs locales du lanceur :

```bash
cp config/astinuslab.env.example config/astinuslab.env
```

`astinuslab.sh` lit :

| Variable | Valeur par défaut dans le script | Objectif |
| --- | --- | --- |
| `ASTINUSLAB_API_HOST` | `127.0.0.1` | Hôte de bind de l'API lu par Settings et utilisé par `astinuslab.serve`. |
| `ASTINUSLAB_API_PORT` | `8000` | Port de l'API et de l'UI lu par Settings et utilisé par `astinuslab.serve`. |
| `ASTINUSLAB_API_LOG_LEVEL` | `info` | Niveau de log Uvicorn lu par Settings et utilisé par `astinuslab.serve`. |
| `ASTINUSLAB_AUTH_ENABLED` | `false` | Active l'auth d'admin unique pour toutes les routes HTTP sauf `/health`, `/ready`, `/login`, `/logout` et `/static/*`. Requis avant les binds non-loopback sauf override dangereux. |
| `ASTINUSLAB_ADMIN_API_TOKEN_HASH` | non défini | Hash SHA-256 du token d'API admin, format `sha256:<64-hex>`. |
| `ASTINUSLAB_ALLOW_UNSAFE_NETWORK_BIND_WITHOUT_AUTH` | `false` | Échappatoire explicite seulement pour expériences locales contrôlées. |
| `ASTINUSLAB_DOCS_PORT` | `9000` | Port du serveur de développement MkDocs avec `start --docs`. |
| `ASTINUSLAB_LIBVIRT_URI` | `qemu:///system` | URI libvirt utilisé par les helpers de nettoyage du lanceur. |
| `ASTINUSLAB_LIBVIRT_NETWORK` | `default` | Réseau libvirt utilisé par les helpers de nettoyage du lanceur. |
| `ASTINUSLAB_SERVICE_USER` | utilisateur actuel | Utilisateur de l'instance systemd vérifié par `status` et protégé par `start`. |

L'unité systemd de l'API lit aussi `/opt/astinuslab/config/astinuslab.env` après l'installation et transmet l'hôte, le port et le niveau de log de l'API à Uvicorn. Si `ASTINUSLAB_API_HOST` est une adresse LAN, configurez d'abord `ASTINUSLAB_AUTH_ENABLED=true` et `ASTINUSLAB_ADMIN_API_TOKEN_HASH`.

### Authentification admin bearer-token

Quand `[auth].enabled` ou `ASTINUSLAB_AUTH_ENABLED` vaut `true`, AstinusLab exige une authentification admin pour chaque route HTTP sauf la liste publique explicite : `/health`, `/ready`, `/login`, `/logout` et les assets statiques sous `/static/*`. Les clients API doivent envoyer `Authorization: Bearer <admin-token>` pour les routes JSON comme `/api/v1/*` et `/volatility/api/*`.

La connexion émet un cookie de session opaque et indépendant (HttpOnly, SameSite=Strict), valable 12 heures fixes, dont seul le hash est stocké côté serveur. Ce cookie ne contient pas le token admin de l’API ; la déconnexion révoque la session côté serveur. Voir [Authentification](../api/authentication.md).

## Console d'administration

La console d'administration rendue par le serveur est accessible depuis la barre latérale gauche. En mode release, ces pages nécessitent la connexion administrateur ; les checkouts de développement les servent normalement sans authentification sur loopback.

| Page | Chemin | Objectif |
| --- | --- | --- |
| Images | `/images` | Lister les images guest locales, téléverser ou récupérer des images, inspecter leurs métadonnées et les supprimer. |
| Workloads | `/workloads` | Rechercher, créer, modifier et supprimer des modèles de workload. Les changements sont écrits dans le catalogue de workloads actif. |
| Storage | `/storage` | Inspecter la capacité de stockage et les buckets d'artefacts. Les onglets renvoient aussi vers Dumps et Workload Cache. |
| Dumps | `/dumps` | Examiner les artefacts mémoire et disque, télécharger les preuves, supprimer des artefacts sélectionnés et nettoyer les fichiers orphelins. La suppression est irréversible. |
| Workload Cache | `/workload-cache` | Inspecter les images snapshot de workloads en cache et supprimer des entrées individuelles, sélectionnées ou toutes. |
| Logs | `/logs` | Consulter les logs de l'application et des jobs, filtrer par niveau, rechercher dans le fichier courant, suivre les mises à jour, copier le log visible ou le télécharger. |
| Security | `/ssh-keys` | Générer, sélectionner et supprimer les paires de clés SSH utilisées par les VMs du labo. La page propose aussi la génération de clés API lorsque l'authentification admin est activée. |
| Settings | `/settings` | Inspecter l'état du service, du réseau et du stockage, et appliquer les paramètres opérationnels. |

Settings comprend l'état et les contrôles du service, les informations du réseau libvirt géré et des baux DHCP, les fenêtres de rétention, l'aperçu et le nettoyage immédiat de la rétention, un seuil minimal d'espace disque libre et l'effacement du cache de symboles/ISF Volatility. Le garde-fou disque bloque les nouveaux jobs sous le seuil configuré ; utilisez `0` pour le désactiver. Effacer le cache de symboles réutilisable ne supprime pas les sorties d'analyse propres au job.

Traitez `storage/ssh_keys/` comme un magasin d'identifiants : ses clés privées sans phrase secrète accordent un accès automatisé aux guests du labo et ne doivent pas être copiées avec les preuves ni placées sur un stockage partagé. La suppression de preuves depuis Dumps, le nettoyage de rétention et le nettoyage du Workload Cache sont irréversibles. Suivez [Intégrité forensique](../reference/FORENSIC_INTEGRITY.md) lors de l'export des preuves.

### Sondes opérationnelles

Utilisez le `GET /health` public pour le liveness du processus sans dépendances et `GET /ready` pour le readiness des dépendances. `/ready` renvoie `503` uniquement lorsque la base de données ou le stockage inscriptible est indisponible ; les échecs des workers ou de libvirt renvoient `200` avec `status: degraded`. Les deux réponses incluent `X-Request-ID` pour les corréler avec les logs du backend. Voir [Endpoints API](../api/endpoints.md#sondes-operationnelles).

## Catalogues

L'app lit les chemins de catalogue depuis `[catalogs]` dans `astinuslab.toml`.

| Fichier | Définit |
| --- | --- |
| `config/catalog/targets.yaml` | Images de base Linux, noms d'utilisateur, métadonnées de distro, ressources VM, comportement cloud-init et flags de cycle de vie. |
| `config/catalog/scenarios.yaml` | Modes d'exécution des jobs tels que `immediate`, `post_script` et `manual_trigger`. |
| `config/catalog/workloads.yaml` | Scripts d'installation de workloads, services, étiquettes CVE, health checks et métadonnées de compatibilité. |
| `config/catalog/attackers.yaml` | Images de VM attaquante utilisables avec les sessions de labo manuel. |
| `config/catalog/profiles.yaml` | Métadonnées de profil utilisées par la couche catalogue. |

Des catalogues d'exemple sont fournis avec des suffixes `.example.yaml`. La configuration de développement les copie généralement vers des fichiers YAML actifs avant le premier démarrage.

## Inspection d'images

Lorsque `virt-inspector` est disponible, AstinusLab inspecte les images dans `images/definitions/` et écrit des sidecars `<image>.meta.json`. Si l'inspection est désactivée ou indisponible, des heuristiques sur le nom de fichier sont utilisées.

## Volatility 3

Les ISF Linux réutilisables sont stockés sous `vol3/symbols/linux/`. La sortie des plugins par job est écrite sous `storage/dumps/<job_id>/vol3/`. Les mirrors d'ISF prebuilt doivent utiliser `https://` dans les déploiements normaux ; `file://` est destiné aux tests locaux et aux environnements hors ligne contrôlés.

Volatility 3 Framework 2.28.0 et `dwarf2json` sont des outils installés par l'opérateur
hors du checkout. Configurez `volatility_bin`, `dwarf2json_path` et
`dwarf2json_sha256`, redémarrez les services et vérifiez readiness ; AstinusLab ne
télécharge pas ces outils et n'accepte pas leurs licences. Conservez la table des
variables générée avec `tools/dev/generate_env_reference.py`.

## Racines de stockage privilégiées

Les helpers ACL installés n'acceptent que les racines enregistrées dans
`/etc/astinuslab/acl-policy.json`, contrôlé par root. Enregistrez une racine
absolue, existante et sans lien symbolique :

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab --storage-root /srv/astinuslab
```

Configurez `[storage] storage_dir` ou `ASTINUSLAB_STORAGE_DIR` avec le même chemin avant son enregistrement. Seule la destination des futures sorties de Jobs change : les preuves existantes et leurs références en base ne sont pas déplacées. Les captures et disques Playground, ainsi que `db_path`, ont des racines indépendantes. Conservez les anciennes racines jusqu'à une migration planifiée vérifiant fichiers, sidecars, références et accès des services. L'enregistrement seul ne migre pas les données et ne permet pas leur suppression ; voir [Politique de stockage](../reference/ADVANCED_CONFIG.md#politique-de-stockage-privilegie).

Utilisez l'identité de service actuelle et le groupe stable `astinuslab` pour la
racine (mode `2770`). Chaque parent doit appartenir à root et ne pas être
inscriptible par le groupe ni par les autres ; utilisez `root:astinuslab` en mode
`0750` lorsque le groupe doit le traverser. Cette disposition reste valable après une migration vers des identités séparées.

`--check` signale les divergences sans modifier l'hôte ; aucun redémarrage n'a
lieu sans `--restart`. Les unités systemd sont rendues avec la vraie racine.
Sous `/home`, chaque unité de service utilise `ProtectHome=tmpfs` et un montage
`BindPaths=` pour la racine configurée.

## Liste des origines navigateur autorisées

Les mutations navigateur (`POST`, `PUT`, `PATCH`, `DELETE`) doivent porter une `Origin` reconnue par
AstinusLab et renvoyer le cookie `astinuslab_csrf` dans l'en-tête `X-CSRF-Token` ou le champ de
formulaire `csrf_token`. Sans configuration, les origines acceptées sont celles en loopback sur
`api_port` : `http://127.0.0.1:<port>`, `http://localhost:<port>` et `http://[::1]:<port>`.

Servir l'UI derrière un proxy inverse ou sous un autre nom d'hôte impose de lister cette origine, ce
qui **remplace** les valeurs loopback par défaut :

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

Les entrées sont un schéma, un hôte et un port exacts ; chemins, identifiants et jokers sont rejetés
au démarrage. Les clients d'API sans en-tête `Origin`, et les requêtes portant un bearer token
admin, ne sont pas affectés. Voir
[Authentification](../api/authentication.md#protection-des-mutations-du-navigateur).

## Référence des variables d'environnement

La référence générée des variables `ASTINUSLAB_*` vit dans la source English de cette page. Gardez cette table générée depuis `tools/dev/generate_env_reference.py` et synchronisez les descriptions localisées quand la source change. Les paramètres de type liste acceptent des valeurs séparées par des virgules ou un tableau JSON ; les paramètres dict acceptent un objet JSON.
