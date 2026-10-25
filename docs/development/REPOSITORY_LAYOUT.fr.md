# Structure du dépôt

AstinusLab conserve le code source, les catalogues, la documentation, les tests et l'état de runtime local dans des emplacements stables. Cette carte est seulement un guide contributeur ; elle ne déplace pas les chemins de runtime.

> L'organisation cible par étapes est approuvée, mais cette page continue de décrire les chemins déjà intégrés. Consultez la [structure cible approuvée du dépôt](../architecture/repository-target-layout.md) et sa matrice de migration ; n'utilisez pas les futurs chemins avant l'intégration de leur étape.

## Code source

```text
src/astinuslab/*.py: 10

src
src/astinuslab
src/astinuslab/api
src/astinuslab/core
src/astinuslab/db
src/astinuslab/forensics
src/astinuslab/i18n_data
src/astinuslab/images
src/astinuslab/jobs
src/astinuslab/live_session
src/astinuslab/migrations
src/astinuslab/orchestration
src/astinuslab/playground
src/astinuslab/storage
src/astinuslab/ui
src/astinuslab/virtualization
src/astinuslab/web
src/astinuslab/workloads
```

- `src/astinuslab/` : application FastAPI installable, tâches Celery, modèles SQLAlchemy, helpers d'orchestration, migrations Alembic appartenant au package et ressources web sous `web/`.
- `tests/` : suites Python applicatives et d'outillage, contrats shell et tests navigateur.
- `guest/` : scripts, templates et helpers côté guest utilisés dans les VMs de lab. Les exemples Autounattend de préparation d'image Windows vivent dans `guest/windows/`.
- `ops/host/` : helpers hôte installés pour libvirt, le stockage, les ACL et le build Windows portable.
- `ops/images/` : helpers opérateur pour le téléchargement d'images et les métadonnées de provenance.
- `ops/deploy/` : implémentations canoniques d'installation, de configuration des racines privilégiées et de désinstallation.
- `ops/systemd/` : modèles source des services API, runner et analyse installés.
- `tools/dev/` : helpers de développement local réservés aux contributeurs ; ils ne sont jamais installés dans une release.
- `tools/quality/` : contrôles de politique qualité du dépôt invoqués directement par la CI.
- `tools/release/` : outils versionnés de preuve, validation et revue de release ; ils restent dans l'archive source mais pas dans l'installation.

## Configuration et catalogues

- `astinuslab.toml` : fichier de configuration local par défaut.
- `config/catalog/` : métadonnées des catalogues de cibles, attaquants, workloads et images.
- `images/definitions/*.meta.json` : petites métadonnées de provenance/catalogue d'images VM qui peuvent être relues et commitées.

## Documentation

- `docs/` : pages source MkDocs. Taxonomie canonique : `docs/user/` pour les workflows humains, `docs/api/` pour la documentation API centrée endpoints, `docs/reference/` pour les faits stables de configuration/spécification, `docs/development/` pour les internes contributeur et `docs/community/` pour gouvernance/auteurs.
- `docs/mkdocs.yml` : navigation, thème et configuration i18n de la documentation.

## Runtime local et fichiers générés

Ces chemins sont de l'état local de runtime/généré. Ne les déplacez pas dans le cadre d'un nettoyage structurel et ne les supprimez pas pendant que des jobs AstinusLab, sessions Playground, workers Celery ou serveurs docs sont actifs.

- `storage/` : bases SQLite, bases broker/result Celery, fichiers PID, logs, dumps, clés SSH, fichiers temporaires, cache de workloads et état Playground. `storage/.gitkeep` garde le dossier présent.
- `build/` : scratch racine ignoré pour la sortie locale/générée de build. Les helpers Windows réutilisables vivent dans `ops/host/windows_build/` ; les templates guest Windows réutilisables vivent dans `guest/windows/`. Préférez `storage/tmp/windows-build/<id>/` pour les nouvelles sorties par exécution de build d'image Windows.
- `images/definitions/*.{img,qcow2,raw}` : binaires locaux d'images VM. Les sidecars de métadonnées peuvent être suivis ; les gros binaires restent locaux sauf exception explicite avec provenance et checksum.
- `vol3/symbols/` : symboles Volatility 3 générés, logs de build et sortie d'extraction.
- Les exécutables Volatility 3 et `dwarf2json` installés par l'opérateur restent
  hors du dépôt et ne sont pas des fichiers générés de l'arbre source.
- `site/` : sortie de build MkDocs.

Consultez [Fichiers générés et de runtime](GENERATED_RUNTIME_FILES.md) pour les règles de nettoyage et les checks de sécurité.

## Tests

- `tests/python/` : suites pytest Python pour APIs backend, orchestration, storage, forensics, Playground et helpers hôte.
- `tests/python/api/` : tests de surface REST (`test_api.py`, `test_api_v1_extended.py`, `test_openapi_contract.py`).
- `tests/python/jobs/` : tests du domaine Job — repository, machine d'état, transitions, runtimes (`test_repositories.py`, `test_job_state_support.py`, `test_job_transitions.py`, `test_runtimes.py`).
- `tests/python/host/` : tests des helpers/scripts hôte (`test_host_acl_helper.py`, `test_host_helpers.py`, `test_sudoers_helper_install.py`, `test_network_isolation_helper.py`, `test_libvirt_storage_admin_helper.py`).
- `tests/python/forensics/` : tests du pipeline forensics/capture.
- `tests/python/playground/` : tests de session/VM/réseau Playground.
- `tests/python/integration/` : tests d'intégration transverses.
- `tests/python/ui/` : tests des routes FastAPI/Jinja et templates rendus côté serveur.
- `tests/python/e2e/` : couverture end-to-end pytest détenue par le backend.
- `tests/python/tooling/` : tests pytest des scripts et outils du dépôt ; exclus de l'exécution par défaut de `tests/python`, mais exécutables avec `.venv/bin/python -m pytest tests/python/tooling -q`.
- `tests/shell/host/` : tests shell d'acceptation de l'hôte, dont la matrice installation/mise à niveau/rollback.
- `tests/browser/` : smoke/integration/full tests navigateur Playwright.

### Organisation des tests

Tous les tests applicatifs appartiennent à des dossiers de domaine sous `tests/python/`.
La racine contient uniquement `conftest.py`, les marqueurs de paquet et les modules de support.
Les tests du dépôt vivent dans `tooling/` et doivent être sélectionnés explicitement.

## État local des outils

- `.claude/`, `.agents/`, `.hermes/`, `.superpowers/`, `.antigravitycli/` et `.worktrees/` : état local des agents et des outils, ignoré par Git.
- Les fichiers d'instructions d'agents (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`) et les fichiers d'exclusion d'outils comme `.cursorignore` relèvent de la configuration personnelle et ne sont pas versionnés.

## Templates et assets frontend

- `src/astinuslab/web/templates/admin_*.html` : pages admin/workbench comme storage, images, logs, settings, workloads et dumps.
- `src/astinuslab/web/templates/job_*.html` : pages Job.
- `src/astinuslab/web/templates/partials/` : fragments HTMX et partials réutilisables.
- `src/astinuslab/web/templates/playground.html` : template front-door Playground.
- `src/astinuslab/web/static/css/layout.css` : feuille de style globale de compatibilité chargée par les templates base.
- `src/astinuslab/web/static/css/` : tokens, layout, composants et styles partagés ou par page.
- `src/astinuslab/web/static/fonts/` : les deux familles typographiques de marque auto-hébergées (woff2), servies depuis la même origine pour respecter la CSP `font-src 'self'`.
- `src/astinuslab/web/static/playground/` : modules JavaScript Playground.

Préservez les routes publiques, noms de templates et URLs statiques pendant le travail structurel sauf si un plan de compatibilité dédié dit le contraire.

### Propriété des templates

- `src/astinuslab/web/templates/admin_*.html` : pages admin/workbench.
- `src/astinuslab/web/templates/job_*.html` : pages job.
- `src/astinuslab/web/templates/partials/` : fragments HTMX et partials réutilisables.
