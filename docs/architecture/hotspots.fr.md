# Hotspots du code et budgets de taille

Cette page liste les fichiers et fonctions qui dépassent les budgets de taille
souples. Le but est la visibilité, pas une politique stricte : les gros fichiers
sont tolérés lorsque la responsabilité est réellement unique, mais une
croissance illimitée devrait déclencher une extraction ciblée avant le prochain
changement non trivial.

Lorsque vous touchez un élément de cette liste et que le changement ajoute plus
de ~20 lignes nettes, préférez une petite passe d'extraction qui fait baisser le
compte.

Les nombres ci-dessous sont vérifiés par `tests/python/tooling/test_hotspot_budgets.py`,
donc ils ne peuvent pas dériver : si un élément listé grandit, la suite échoue
jusqu'à ce que la colonne LOC soit réécrite, et un fichier ou une fonction qui
franchit un seuil de découverte échoue jusqu'à son ajout au tableau. Les
plafonds souples ne sont **pas** appliqués -- plusieurs entrées dépassent leur
plafond par choix délibéré, et les notes disent pourquoi.

Ce qui *est* imposé, c'est le sens de l'évolution. La même suite enregistre,
par tableau, combien d'entrées dépassent leur plafond, de combien de lignes au
total, et la somme des plafonds. Les trois sont redéclarés exactement, et la
règle de redéclaration est qu'ils ne peuvent que baisser. Relever un plafond
pour rendre une entrée conforme ne fonctionne pas : la somme des plafonds
change et la suite signale que le tableau a empiré. La complexité cyclomatique
a son propre cliquet dans `pyproject.toml` (`lint.mccabe.max-complexity`), fixé
à la pire fonction de l'arbre afin que rien de nouveau ne puisse être pire que
ce qui est déjà le pire.

## Comment les nombres sont mesurés

- **LOC de fichier** est le nombre de lignes physiques, tel que `wc -l` le
  rapporte.
- **LOC de fonction** va de la ligne `def` jusqu'à la dernière ligne du corps.
  Les décorateurs sont exclus, donc en ajouter un ne change pas le nombre.
- Le balayage des fonctions couvre les fonctions au niveau du module et les
  méthodes des classes au niveau du module. Les closures imbriquées dans une
  fonction sont déjà comptées dans leur entrée englobante.
- **LOC de fichier shell** est le nombre de lignes physiques d'un `*.sh`
  suivi par Git.
- **Python embarqué dans le shell** est le nombre de lignes à l'intérieur des
  corps heredoc `python3 - ... <<'PY'` de ce script, trouvés par le même
  helper de découverte que celui des gates de lint, afin que les deux ne
  puissent jamais diverger sur ce qui compte comme Python embarqué.

## Seuils de découverte

Tout ce qui atteint ou dépasse ces tailles **doit** apparaître ci-dessous.
Les deux premiers s'appliquent à `src/astinuslab` ; les deux derniers
s'appliquent à chaque `*.sh` suivi par Git :

- Fichiers : **900** lignes.
- Fonctions : **180** lignes.
- Fichiers shell : **700** lignes.
- Python embarqué dans le shell : **250** lignes.

Ils sont volontairement plus souples que les plafonds par entrée. Un plafond dit
où un module devrait atterrir ; un seuil dit le point à partir duquel rester
hors de cette page cesse d'être une option. Lister quelque chose de plus petit
est toujours permis, et quelques entrées sont suivies de manière proactive tout
en restant sous leur plafond.

## Gates de baseline

Dernière mesure complète 2026-09-05, sur `feature/portable-lab-sharing` :

- `.venv/bin/python -m ruff check .` : passant.
- `.venv/bin/python -m ruff format --check .` : passant, 499 fichiers.
- `.venv/bin/python -m mypy src tests/python` : passant sous mypy 2.1.0, 455 fichiers source.
- `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q` :
  passant, 4005 passed, 18 skipped, couverture totale 86.02%.
- `.venv/bin/python -m pytest tests/python/tooling -q` : passant, 933 passed,
  2 skipped. Revérifié le 2026-09-06 après déplacement de la conception durable
  portable-lab-sharing dans `docs/plans/`, laissant `docs/superpowers/` local aux
  outils et non suivi.

Les plus gros fichiers Python du backend restent les modules de flux
d'orchestration / Volatility 3. Relancez les gates de qualité après avoir touché
un module listé ; le gate de couverture actif est de 86% des lignes
*et des branches*.

## Fichiers longs

| Fichier | LOC actuel | Plafond souple | Notes |
|------|-------------|--------------|-------|
| `src/astinuslab/i18n_data/en.py` | 975 | 1100 | Données de langue avec clés et paramètres équivalents dans les quatre langues. Préserver la terminologie et les contrôles de parité lors du découpage. |
| `src/astinuslab/i18n_data/es.py` | 975 | 1100 | Données de langue avec clés et paramètres équivalents dans les quatre langues. Préserver la terminologie et les contrôles de parité lors du découpage. |
| `src/astinuslab/i18n_data/fr.py` | 975 | 1100 | Données de langue avec clés et paramètres équivalents dans les quatre langues. Préserver la terminologie et les contrôles de parité lors du découpage. |
| `src/astinuslab/i18n_data/ca.py` | 975 | 1100 | Données de langue avec clés et paramètres équivalents dans les quatre langues. Préserver la terminologie et les contrôles de parité lors du découpage. |
| `src/astinuslab/lab_packages/package_service.py` | 1827 | 1000 | Façade du cycle de vie des paquets portables : import, préparation, lancement, réinitialisation, démontage, suppression et réconciliation. Le plus gros nouveau module de la page. Le chemin de réconciliation et la machine à états par action sont les deux blocs cohérents à extraire en premier. |
| `src/astinuslab/lab_packages/prepare_service.py` | 963 | 900 | Admission de Prepare, baux de rétention, réservations d'exécution et propriété des actifs vérifiés. R33 sépare la réutilisation immuable partagée de la mutation exclusive ; préserver l'ordre des verrous et les tests de récupération lors d'une extraction. |
| `src/astinuslab/lab_packages/verification_service.py` | 1463 | 1000 | Vérification de démarrage propre côté créateur : cycle de vie de la session, cloisonnement des workers, évaluation des checkpoints et annulation. Le cloisonnement et l'évaluation sont séparables ; gardez la comptabilité des baux en un seul endroit lors de la découpe. |
| `src/astinuslab/lab_packages/archive.py` | 1270 | 1000 | Lecteur et écrivain `.astinuslab`, plus les contrôles de sûreté de l'archive : traversée de chemins, liens, chemins dupliqués ou en collision de casse, entrées non déclarées et bombes de compression. Ici la longueur est la liste des menaces ; extrayez l'écrivain avant les validateurs. |
| `src/astinuslab/lab_packages/api.py` | 1523 | 900 | Routes FastAPI du flux de paquets. Corps de routes plats, donc découpez par étape du flux -- inspection/import, préparation, lancement, vérification -- à mesure qu'il grossit. |
| `src/astinuslab/forensics/vol3_support.py` | 1207 | 1000 | Construction de symboles Volatility 3 / ISF dans le paquet `forensics/`. La chaîne de construction de symboles est fortement couplée à l'état du fournisseur ; n'extrayez qu'avec une couverture ciblée. |
| `src/astinuslab/playground/repositories.py` | 1269 | 900 | Persistance du Playground : cycle de vie des lignes VM, réseau et NIC, plus les helpers de réclamation compare-and-swap. Surtout des fonctions de requête plates, donc la croissance est peu coûteuse à découper par entité. |
| `src/astinuslab/db/models.py` | 1163 | 1200 | Définitions déclaratives des tables de tout le schéma ; a grossi avec les tables de paquet, d'instance et de vérification. La croissance se fait à raison d'une classe par table plutôt qu'en logique enchevêtrée, donc le plafond généreux est délibéré. Découpez par domaine seulement si le fichier cesse de se lire de haut en bas. |
| `src/astinuslab/catalog.py` | 1096 | 900 | Chargement du catalogue, sélecteurs de compatibilité et inférence de découverte d'images. Gardez les ajouts étroits ou extrayez des helpers cohésifs de parsing/normalisation. |
| `src/astinuslab/jobs/package_instance_coordinator.py` | 1518 | 900 | Coordinateur côté Celery des actions d'instance de paquet : réservation, démarrage verrouillé, allocation d'overlays et nettoyage des revendications périmées. Le nettoyage et la réservation prennent tous deux des verrous ; extrayez-les ensemble ou pas du tout. |
| `src/astinuslab/storage/publication.py` | 1070 | 900 | Publication d'artefacts, hachage et écriture de manifestes. Découpez par type d'artefact si cela regrandit. |
| `src/astinuslab/orchestration/deps.py` | 777 | 1500 | Conteneur de dépendances explicite qui a remplacé le shim de réexport dissous d'`orchestrator.py` ; surtout des closures de câblage par défaut. Plafond délibérément généreux -- voir la note de `JobRunDeps.default` ci-dessous. |
| `src/astinuslab/db/repositories.py` | 1033 | 900 | Requêtes de persistance et contrôles du cycle de vie ; pagination bornée et comptages groupés ajoutés par l’audit du backend. Séparer par entité avant tout ajout. |

## Fonctions longues

| Fonction | LOC | Plafond souple | Notes |
|----------|-----|--------------|-------|
| `src/astinuslab/orchestration/deps.py::JobRunDeps.default` | 623 | 600 | Le bloc de câblage de production : un closure par dépendance injectée. La complexité cyclomatique vaut 1, donc la longueur ici est une propriété du motif de câblage explicite et non d'une logique emmêlée. Traitez la *croissance* comme le signal, pas le nombre absolu ; si elle passe le plafond, découpez le conteneur par domaine plutôt que de raccourcir les closures. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare_windows` | 235 | 180 | Préflight Windows, firmware par instance, bootstrap QGA et nettoyage. Préserver l’ordre des phases et les tests de démarrage. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare` | 210 | 180 | Sélection de plateforme et préparation du démarrage Linux. Préserver la séparation des plateformes et le nettoyage après erreur. |
| `src/astinuslab/forensics/vol3_analysis.py::_run_reserved_analysis` | 417 | 180 | Risque de régression plus élevé que les modules de flux job-run. |
| `src/astinuslab/forensics/capture_support.py::_acquire_and_publish_locked` | 320 | 180 | Porte la sémantique de verrouillage côté capture. |
| `src/astinuslab/forensics/vol3_support.py::run_vol3_symbol_build` | 280 | 180 | La chaîne de construction de symboles est fortement couplée à l'état du fournisseur. |
| `src/astinuslab/jobs/startup_recovery.py::cleanup_stale_playground_resources` | 266 | 180 | Nettoyage au démarrage avec revalidation de la propriété des ressources lors du nettoyage de libvirt, du système de fichiers et de la base de données. Préserve les ressources des paquets, la vérification figée et la garde des captures ; extrayez une étape à la fois uniquement avec une couverture ciblée de la garde des ressources. |
| `src/astinuslab/forensics/vol3_plugin_run.py::run_vol3_plugin` | 395 | 180 | Invocation du plugin, sélection de l'ISF et persistance des résultats dans un seul corps. |
| `src/astinuslab/lab_packages/package_service.py::LabPackageService.reconcile` | 237 | 180 | Réconciliation au démarrage des instances de paquet, des baux de contenu et des actions interrompues. Même classe de risque que `reconcile_libvirt_resources` : un bug ici fuit du stockage et des VM, donc extrayez une étape à la fois avec une couverture ciblée. |
| `src/astinuslab/playground/vm_lifecycle.py::spawn_vm` | 190 | 180 | En baisse depuis 416, et inchangée par la découpe en domaines : elle a changé de module, elle n'a pas été réduite davantage. Il reste le squelette try/except/finally qui possède le bail de réclamation et le nettoyage d'échec, plus les appels aux huit phases extraites d'elle. Chaque coupe supplémentaire ajouterait un site d'appel aussi long que le bloc qu'elle retire. |
| `src/astinuslab/lab_packages/export_service.py::export_locked` | 230 | 180 | Export verrouillé : contrôle d'attestation, gel de la définition, calcul des empreintes de couches et assemblage de l'archive. La moitié empreintes et assemblage est l'extraction la plus propre. |
| `src/astinuslab/db/__init__.py::restore_database` | 209 | 180 | Restauration de sauvegarde avec vérifications de schéma et rollback. Riche en branches par nature ; gardez le chemin de rollback en un seul endroit en le découpant. |
| `src/astinuslab/lab_packages/verification_service.py::_run_verification` | 205 | 180 | Une tentative de vérification de démarrage propre de bout en bout. Très ramifiée parce que chaque étape porte une sortie d'annulation et une sortie d'échec. |
| `src/astinuslab/lab_packages/archive.py::write_archive_to` | 201 | 180 | Écrivain d'archive : ordre des membres, manifeste canonique et streaming des charges utiles en une seule passe pour que la sortie reste déterministe octet par octet. La découper met en péril le déterminisme qu'elle existe pour garantir. |
| `src/astinuslab/lab_packages/archive.py::inspect_archive` | 201 | 180 | Le chemin d'inspection passive : tous les contrôles de sûreté de l'archive exécutés avant tout import. La longueur est la liste des menaces ; gardez les contrôles en une séquence lisible. |
| `src/astinuslab/lab_packages/prepare_service.py::run_prepare` | 41 | 180 | Acquisition du contenu : d'abord les octets embarqués, puis le cache local, puis les sources explicitement épinglées, avec publication atomique et reprise. |
| `src/astinuslab/virtualization/process_support.py::run_command` | 221 | 180 | Bounded process supervision and exception-preserving cleanup; keep custody transitions together. |
| `src/astinuslab/lab_packages/launch_service.py::launch_package` | 197 | 180 | Lancement transactionnel : recalcul du preflight, réservation, création des overlays et rollback à la moindre défaillance. Tout juste au-dessus du plafond ; gardez le chemin de rollback entier. |
| `src/astinuslab/jobs/runtime_support.py::parse_guest_ip` | 156 | 150 | Le parsing réseau de l'invité a de nombreuses branches de repli. Gardez les changements étroits et couverts par des tests de compatibilité. |

## Scripts shell longs

Le shell est la surface la moins vérifiée du projet. ShellCheck et `bash -n`
attrapent la syntaxe et les guillemets ; rien ne lit ces fichiers pour leur
logique, il n'y a pas de mesure de couverture, et le seul harnais
d'acceptation exécutable -- `tests/shell/host/upgrade_matrix.sh` exige toujours un hôte jetable et un humain pour le lancer. Il est désormais déclenchable via `.github/workflows/upgrade-acceptance.yml` au lieu d'être assemblé à la main, mais il n'est délibérément pas planifié : il installe et désinstalle pour de vrai, en root, donc il s'exécute là où quelqu'un a déclaré un hôte sacrifiable. Les deux
défauts bloquants trouvés par l'acceptation hôte étaient dans
`ops/deploy/install.sh`, et l'un était un motif `rsync --exclude` non ancré.
La taille est le seul indicateur bon marché de ce risque, elle est donc
budgétée ici comme la surface Python.

| Script | LOC actuel | Plafond souple | Notes |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh` | 3590 | 2500 | Toujours le fichier aux plus grandes conséquences du dépôt : il s'exécute en root, crée des identités, déplace des ACL et de la politique MAC, et valide ou annule une mise à niveau. 136 fonctions, donc sa longueur est de la composition et non un seul bloc. En baisse depuis 5151 maintenant que les corps d'instantané/restauration d'ACL, de manifeste de traversée, de journal de transactions, de clé SSH, de configuration gérée, de permissions, d'écriture de configuration, de marqueur d'identité et de nettoyage hérité sont de vrais modules. Réduisez-le davantage en sortant des corps de heredoc (voir le tableau suivant), pas en fusionnant des fonctions shell. |
| `tests/shell/host/upgrade_matrix.sh` | 1374 | 1200 | La matrice d'acceptation installation, mise à niveau et rollback. La longueur est le nombre de lignes et la preuve que chacune enregistre ; la scinder scinderait le rapport unique que lit un relecteur de release. Budgétée pour que la croissance reste délibérée. |
| `astinuslab.sh` | 1211 | 900 | Lanceur opérateur et `setup-host`. Mêle le contrôle de services en mode dev et la préparation de l'hôte ; la moitié préparation de l'hôte est la partie séparable. |
| `ops/deploy/configure-privileged-roots.sh` | 782 | 700 | Configuration privilégiée de la racine de stockage, avec journal de transactions et annulation exacte. L'annulation elle-même vit dans `ops/deploy/host_access.py`, que ce script et l'installateur appellent, et la vérification du marqueur d'identité dans `ops/deploy/privileged_state.py`, que les trois points d'entrée privilégiés appellent. 35 % de ce qui reste est encore du Python embarqué. |

## Python embarqué dans le shell

1 581 lignes de Python vivent dans des heredocs `<<'PY'`. Ruff, Mypy et la
couverture se fondent sur `*.py`, donc aucun ne voit ce code ; jusqu'à ce que
le helper de découverte soit élargi aux continuations par barre oblique
inverse, 791 de ces lignes n'étaient même pas vérifiées syntaxiquement.
`tests/python/tooling/test_host_scripts.py` compile désormais chaque corps et
y passe pyflakes, mais c'est un plancher, pas du typage ni de la couverture.
Traitez un nombre croissant ici comme le signal d'expédier la logique en
module réel sous `ops/` et de l'appeler, pas comme une raison de relever le
plafond.

2 419 lignes ont déjà suivi ce chemin : `ops/deploy/acl_snapshot.py`,
`ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`,
`ops/deploy/managed_config.py`, `ops/deploy/permissions.py` et
`ops/deploy/privileged_state.py` ne sont importés par personne et sont invoqués
comme des sous-processus, exactement comme les heredocs, mais Ruff, Mypy et
leurs propres tests unitaires les lisent désormais. `ops/deploy/uninstall.sh` a
quitté les deux tableaux ainsi : il est passé de 819 lignes et 480 de Python
embarqué à 349 et aucune.

| Script | LOC de Python embarqué | Plafond souple | Notes |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh::embedded-python` | 677 | 800 | En baisse depuis 2333 : les corps d'instantané/restauration d'ACL, de manifeste de traversée, de journal de transactions, de clé SSH, de configuration gérée, de permissions, d'écriture de configuration, de marqueur d'identité et de nettoyage hérité de vol3 sont maintenant `ops/deploy/acl_snapshot.py`, `ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`, `ops/deploy/managed_config.py`, `ops/deploy/permissions.py` et `ops/deploy/privileged_state.py`. Ce qui reste est une longue traîne sans aucun grand corps. |
| `ops/deploy/configure-privileged-roots.sh::embedded-python` | 276 | 300 | Sous son plafond depuis que le corps d'annulation de 281 lignes est devenu `ops/deploy/host_access.py`, partagé avec l'installateur qui écrit les enregistrements qu'il lit, et que la vérification du marqueur d'identité est devenue `ops/deploy/privileged_state.py`. La vérification d'autorité de la source d'installation, de 92 lignes, est ce qui reste. |

## Que faire quand cette liste grandit

1. Choisissez le plus petit élément au-dessus de son plafond.
2. Extrayez le plus long bloc contigu à responsabilité unique vers un
   helper frère dans le même module (ou un `_helpers.py` frère si un
   thème clair émerge).
3. Le helper doit prendre toutes les entrées en paramètres et retourner les
   valeurs explicitement. Pas d'état mutable partagé.
4. Lancez la tranche de tests ciblée pour le module touché, puis le
   `ASTINUSLAB_FAKE_LIBVIRT=1 pytest -q` complet.
5. Mettez à jour les nombres de LOC dans ce fichier dans le même commit.
   `tests/python/tooling/test_hotspot_budgets.py` échoue tant que ce n'est pas fait.

Pour une entrée shell, l'étape 2 est différente : la cible d'extraction est
un vrai module `*.py` sous `ops/`, invoqué comme sous-processus, et non une
autre fonction shell. C'est cela qui place le code sous Ruff, Mypy et la
couverture.
