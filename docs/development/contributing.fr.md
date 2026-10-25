# Contribuer

Utilisez cette page pour le workflow de développement. Les documents communautaires du projet restent dans la section Community.

Consultez la [matrice de vérification](verification-matrix.md), maintenue en anglais avec repli pour éviter de dupliquer les instructions. Isolez les tâches simultanées et préservez leurs modifications.

## Avant d'ouvrir une Pull Request

1. Partez d'une branche propre.
2. Gardez les changements ciblés.
3. Ajoutez ou mettez à jour les tests quand le comportement change.
4. Mettez à jour la documentation quand des commandes, APIs, configurations ou workflows changent.
5. Exécutez les commandes de validation locale qui correspondent au changement.

## Validation locale

Pour les changements backend :

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

Pour les changements de docs :

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

Pour les helpers shell hôte :

```bash
shellcheck ops/host/*.sh
```

## Standards de code

- Le code Python doit suivre les patterns existants FastAPI, Pydantic, SQLAlchemy et repositories.
- Les scripts shell doivent utiliser des patterns Bash défensifs et `set -euo pipefail`.
- Les changements frontend doivent suivre la structure existante Jinja2, HTMX et JavaScript vanilla.
- N'ajoutez pas de dépendances larges sauf si elles sont nécessaires au design du dépôt.

### Nommage des champs d'erreur

Trois contextes, trois noms — voir [`docs/architecture/adr-error-field-naming.md`](../architecture/adr-error-field-naming.md). Appliqué par `tools/quality/audit_error_fields.py` (exécuté comme `tests/python/tooling/test_error_field_audit.py`).

## Standards de documentation

- Documentez les commandes qui existent dans le dépôt.
- Gardez séparées les docs d'usage, d'architecture, d'API et de développement.
- Marquez explicitement les inconnues au lieu d'inventer un comportement.
- Préservez les fonctionnalités du thème MkDocs et le style de navigation existants.

## Traduire la documentation

Les docs sont localisées avec le plugin `mkdocs-static-i18n`. English (`en`) est la source de vérité unique ; les locales prises en charge sont `en`, `es`, `fr` et `ca` (comme l'UI web).

- **English fait autorité.** Modifiez toujours `page.md` d'abord. Les pages publiques doivent fournir des siblings localisés pour chaque locale prise en charge afin que les builds par langue ne retombent pas sur du contenu English.
- **Ajoutez une traduction** en créant un fichier sibling avec suffixe de locale à côté de la page English : `index.es.md`, `index.fr.md`, `index.ca.md`, `usage/getting-started.es.md`, etc. Gardez le même nom de fichier, les mêmes liens et la même structure de headings que la source English ; les termes produit (Playground, Jobs, Target, Workload, Attacker, Acquisition, Volatility 3) restent non traduits.
- **Les titres de section** dans la navigation gauche sont traduits via `nav_translations` sous chaque locale dans `docs/mkdocs.yml`. Ajoutez une entrée seulement quand vous introduisez un nouveau groupe de nav ou libellé de page.
- **Ne traduisez pas en masse.** Traduisez une page uniquement quand elle peut rester synchronisée avec sa source English ; gardez les archives internes de planification hors des builds publiés via `exclude_docs` quand elles ne sont pas maintenues comme docs publiques.

Construisez pour confirmer que toutes les locales s'affichent :

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```
