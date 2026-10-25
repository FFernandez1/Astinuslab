# Tests

Les tests automatisés sont configurés dans `pyproject.toml` et GitHub Actions.

Après `./astinuslab.sh setup`, installez la chaîne frontend verrouillée et Chromium pour les tests navigateur :

```bash
npm ci
npx playwright install chromium
```

Consultez la [matrice de vérification](verification-matrix.md), maintenue en anglais avec repli pour éviter de dupliquer les instructions. Isolez les tâches simultanées et préservez leurs modifications.

## Tests du backend

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
```

Exécuter un fichier spécifique :

```bash
.venv/bin/python -m pytest tests/python/api/test_api.py -q
```

Exécuter le gate de couverture CI :

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

Le seuil de couverture est `84` à la fois dans `pyproject.toml` et `.github/workflows/ci.yml`.

La mesure est la couverture de **branches** (`branch = true` dans `pyproject.toml`), pas de lignes. Ce code est dense en clauses de garde, et une garde dont le corps n'est jamais exécuté compte quand même comme ligne couverte. Compter les arcs a donné 84,3 % là où les lignes donnaient 86,7 %, donc le seuil a été ramené au chiffre mesuré. Relevez-le à mesure que les arcs se ferment ; ne le baissez pas.

### Organisation du dossier de tests backend

Les tests sous `tests/python/` sont organisés en dossiers de domaine : API, forensics, jobs, playground, lab_packages, host, UI, security, integration, e2e et `host_real`. Exécutez un dossier de domaine isolément avec des commandes comme :

```bash
.venv/bin/python -m pytest tests/python/playground -q
.venv/bin/python -m pytest tests/python/forensics -q
.venv/bin/python -m pytest tests/python/jobs -q
ASTINUSLAB_REAL_HOST=1 .venv/bin/python -m pytest tests/python/host_real -q  # hôte KVM/libvirt réel seulement
```

`tests/python/ui/` contient des tests pytest de routes FastAPI/Jinja rendues côté serveur ; `tests/browser/` contient les tests navigateur Playwright.

## Tests unitaires du dépôt

Les tests unitaires de l'installateur, des helpers hôte, du launcher et du thème de documentation vivent dans `tests/python/tooling` :

```bash
.venv/bin/python -m pytest tests/python/tooling -q
```

## Niveaux de test

Utilisez le niveau sûr le plus bas qui couvre le changement :

| Niveau | Commande | Portée | Sécurité |
| --- | --- | --- | --- |
| Niveau 0 statique | `.venv/bin/python -m ruff check .`, `.venv/bin/python -m mypy src tests/python ops/deploy`, `.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict`, `npm run lint:js` | Syntaxe, imports, typage, documentation et lint JavaScript | Aucune VM ni mutation de l'hôte |
| Niveau 1 backend | `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q` | Tests unitaires, services, dépôts, API et UI rendue côté serveur | Utilise des fakes/fixtures ; les fichiers e2e sont ignorés sauf demande explicite |
| Niveau 1b tooling dépôt | `.venv/bin/python -m pytest tests/python/tooling -q` | Tests unitaires de l'installateur, du launcher, des helpers hôte et du thème docs | Utilise des fichiers temporaires et des scripts factices |
| Niveau 2 backend e2e | `.venv/bin/python -m pytest tests/python/e2e -q` | Couverture navigateur Playwright portée par le backend contre un backend fake-libvirt | S'exécute en CI après les smoke tests frontend |
| Niveau 2 smoke UI | `npm run test:smoke` | Smoke tests navigateur avec mocks de routes/API | Aucun POST de provisionnement ni mutation libvirt |
| Niveau 2 intégration UI | `npm run test:integration` | Checks de lecture/validation avec backend réel | Manuel pour l'instant ; garder isolé de l'état runtime développeur |
| Niveau 3 e2e VM complet | `ASTINUSLAB_E2E_FULL=1 npm run test:e2e:full` | Flows complets navigateur + backend + VMs libvirt | Manuel uniquement ; peut muter libvirt et le stockage hôte |

### Exécuter un job au-delà de l'admission

Créer un Job est sans risque partout. L'*exécuter* ne l'est pas : une exécution
installe une image de base via `sudo libvirt_storage_admin.sh` et définit un
domaine, donc sur un hôte où cette règle sudo est configurée — l'état normal
d'une machine qui fait tourner ce produit — lancer un job depuis un test
provisionne une vraie VM avec un vrai disque. Ni `ASTINUSLAB_FAKE_LIBVIRT` ni
`ASTINUSLAB_FAKE_PLAYGROUND` ne couvrent ce chemin.

```bash
ASTINUSLAB_FAKE_ORCHESTRATION=1
```

remplace les dépendances de `JobRunDeps` qui atteignent l'hôte, et rien d'autre :
les runners parcourent le vrai graphe de statuts et écrivent les vrais
événements. Un job `manual_trigger` atteint `waiting_for_capture`, donc la
progression des étapes, l'annulation et le déclencheur de capture deviennent
accessibles. Un job de capture effectue le même démarrage puis **échoue** là où
il aurait produit une preuve : simuler un démarrage, c'est simuler une
observation ; simuler une acquisition, c'est falsifier un artefact, et ce flag ne
le fait pas.

Toute commande qui franchirait la limite de l'hôte lève une erreur au lieu de
renvoyer un succès factice, si bien qu'un chemin non anticipé fait échouer le
test plutôt que d'atteindre la machine. Le flag est inactif sauf s'il est défini,
il est rapporté par `/health` et la bannière fake mode, et il est délibérément
séparé des deux autres pour que le niveau Jobs continue de tester l'admission
contre un backend qui n'exécute jamais.

Les niveaux navigateur Playwright utilisent `venv/bin/python` par défaut et peuvent pointer vers un autre interpréteur quand un hôte de release ou une image CI utilise un autre chemin de venv:

```bash
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:smoke
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:integration
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:e2e:full
```

N'exécutez jamais le Niveau 3 sur un hôte avec des labs actifs avant d'avoir vérifié les VMs, réseaux et chemins de stockage en cours. Gardez l'e2e VM complet hors du CI PR par défaut sauf si un runner libvirt dédié existe.

## Preuve de release (libvirt réel)

La CI simule libvirt ; avant de taguer une release, exécutez les chemins VM réels sur un hôte libvirt et consignez le résultat. Une suite pytest verte est obligatoire, mais elle ne suffit pas pour un GO car la CI ne couvre pas tous les chemins libvirt, invité, capture et artefact de production.

1. Avant de collecter les preuves hôte, partez du commit de release : `git status --short` doit être vide et la valeur exacte de `git rev-parse HEAD` doit être consignée dans les notes de release ou le paquet de preuves.
2. Préparez une image (backing file présent dans `images/definitions/`) et une clé SSH active générée sur la page Security (`/ssh-keys`).
3. Lancez le Tier-3 full E2E : `npm run test:e2e:full`.
4. Exécutez les workflows vérifiés sur hôte depuis le commit de release consigné et préparez le JSON des sept lignes ainsi que les preuves décrites dans la procédure interne d'acceptation.
5. Collectez puis validez indépendamment le bundle privé avec `collect_release_evidence.sh` et `validate_release_evidence.py` selon cette procédure.
6. Gardez le bundle privé sous le contrôle du responsable de release. Seule sa projection `public/`, générée et relue, peut être publiée.

| # | Workflow | Comment l'exécuter | Preuve requise |
| --- | --- | --- | --- |
| 1 | Jobs / `immediate`, mémoire | API ou UI, n'importe quelle cible | Job terminé, trio d'artefact mémoire, sha256 indépendant et preuve du workflow |
| 2 | Jobs / `post_script` ou interactif, disque ou `both` | API ou UI, n'importe quelle cible (le disque est rejeté en mode `immediate`) | Job terminé, trio d'artefact disque, sha256 indépendant et preuve du workflow |
| 3 | Jobs / `post_script` | Scénario `workload-sample-post-script` | Job terminé, trio d'artefact, sha256 indépendant et journal de `running_scenario_script` à `waiting_for_capture` |
| 4 | Capture interactive de Job | Déclencher la capture depuis l'UI | Job terminé, trio d'artefact, trace Playwright et capture d'écran |
| 5 | Installation de workload Playground | Installer DVWA depuis une image locale déjà préparée, sans sortie externe | Identité du workload terminé et preuve d'accessibilité ou backend équivalente ; aucun trio d'artefact |
| 6 | Capture mémoire Playground | Bouton de capture de l'UI | Acquisition terminée, trio d'artefact mémoire, trace Playwright et capture d'écran |
| 7 | Capture disque Playground | Bouton de capture de l'UI | Acquisition terminée, trio d'artefact disque, preuve de reprise, trace Playwright et capture d'écran |

GO exige des preuves pour les sept lignes produites sur le commit de release sur un hôte KVM réel. Une ligne absente sans waiver complet à six champs impose NO-GO ; tout waiver complet et non expiré limite le verdict à GO-WITH-RISKS. Aucun waiver n'est prévu pour 1.0.0.

Le collecteur valide les artefacts bruts sur place mais ne les copie jamais. Le fichier privé `evidence.json` fait autorité ; manifestes, sidecars, preuves, checksums et `HOST_WORKFLOW_CHECKLIST.md` sont du matériel d'intégrité ou des vues générées. Ne modifiez jamais le checklist pour le rendre PASS. Le validateur indépendant doit annoncer PASS pour le candidat exact et propre avant la revue de release.

## Marqueurs pytest

Les marqueurs de base sont enregistrés dans `pyproject.toml` pour garder les exécutions ciblées explicites :

```bash
.venv/bin/python -m pytest --markers
.venv/bin/python -m pytest tests/python -m "not e2e" --collect-only -q
.venv/bin/python -m pytest tests/python/e2e -m e2e --collect-only -q
.venv/bin/python -m pytest tests/python/tooling -q
```

## Lint et type check

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
```

Ruff sélectionne `E9`, `F`, `I`, `UP`, `B` et `S`. Mypy est configuré pour Python 3.12 avec `check_untyped_defs = true`.

## Scripts shell

Lorsque ShellCheck est installé :

```bash
shellcheck ops/host/*.sh
```

## Documentation

```bash
.venv/bin/python -m pip install "mkdocs-material>=9.5" "mkdocs-static-i18n>=1.2"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

## CI

`.github/workflows/ci.yml` s'exécute sur push et pull request. Le job backend s'exécute sur Python 3.12 et 3.13 ; le job frontend/Playwright, y compris pytest e2e backend, s'exécute sur Python 3.12 :

1. Installer `libvirt-dev` et `pkg-config`.
2. Installez `requirements-dev.lock` — généré depuis `uv.lock`, jamais modifié à la main.
   Consultez [Verrouillage des dépendances](../architecture/dependency-locking.md).
3. Installer le projet avec `python -m pip install -e . --no-deps`.
4. Exécuter le lint et la vérification de format Ruff.
5. Exécuter Mypy.
6. Exécuter pytest du backend avec le gate de couverture.
7. Exécuter les tests de tooling du dépôt depuis `tests/python/tooling`.
8. Exécuter le lint frontend, la vérification de format, les tests unitaires JavaScript et les smoke tests Playwright.
9. Installer Chromium pour Python Playwright.
10. Exécuter pytest e2e backend depuis `tests/python/e2e`.
11. Téléverser les artefacts HTML de couverture et du rapport Playwright.

`.github/workflows/vol3-symbols.yml` s'exécute sur les pull requests touchant des chemins liés à Volatility 3 et aux symboles.
