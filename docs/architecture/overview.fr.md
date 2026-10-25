# Vue d'ensemble de l'architecture

AstinusLab est un système d'orchestration de labo local. L'API et l'UI acceptent les requêtes de l'opérateur, persistent l'état des jobs dans SQLite, mettent en file le travail de longue durée via Celery, et un host runner contrôle libvirt, SSH, la capture de disque, la capture de mémoire et la publication des artefacts.

```text
Browser / API clients
        |
        v
FastAPI -----> SQLite application state
        |
        v
Celery SQLite broker
        |                           |
        v                           v
Lifecycle runner + Beat       Analysis worker
(concurrency 1, threads)       (analysis queue, concurrency 1)
        |                           |
        +--> libvirt / QEMU / KVM    +--> Volatility plugins
        +--> guest setup via SSH
        +--> capture / ISF work
        +--> artifacts and logs <---+--> reports
```

## Frontières

| Frontière | Responsabilité |
| --- | --- |
| App FastAPI | API REST, UI HTML, validation des requêtes, création de jobs, téléchargements d'artefacts. |
| Base de données SQLite | Enregistrements de jobs, transitions, événements, actions hôte, runtimes, acquisitions, état du playground. |
| Worker Celery | Exécute les actions d'orchestration privilégiées et de longue durée hors du flux requête/réponse. |
| Scripts auxiliaires hôte | Effectuent des opérations root restreintes via des entrées d'allow-list sudoers installées. |
| libvirt/QEMU | Cycle de vie des VMs, réseaux, overlays de disque, dumps mémoire et conversion de disque. |
| Catalogues | Définitions déclaratives de cible, scénario, workload et VM attaquante. |
| Stockage | Artefacts capturés, journaux, fichiers temporaires, bases de données Celery, clés SSH, cache de workloads. |

## Modes d'exploitation

Le mode développement s'exécute depuis le dépôt via `./astinuslab.sh`. Le mode release copie l'app dans `/opt/astinuslab`, crée un virtualenv, installe les scripts auxiliaires et les unités systemd, et exécute les services en tant que `astinuslab-api@USER` et `astinuslab-runner@USER`.

Les deux modes ne doivent pas s'exécuter ensemble car les deux runners peuvent consommer la même file et contrôler les mêmes ressources libvirt de l'hôte.

## Modèle de sécurité

AstinusLab dispose d'une authentification d'API optionnelle à rôle d'administrateur unique. Par défaut, il reste conçu pour des laboratoires locaux de confiance. Le mode de développement écoute sur loopback sauf substitution explicite ; les liaisons non-loopback requièrent l'authentification d'API ou une dérogation non sécurisée explicite. Pour les réseaux partagés, placez-le derrière TLS, un reverse proxy, un VPN et des règles de pare-feu. La connexion dans le navigateur (`/login`) génère un cookie de session indépendant haché côté serveur.
