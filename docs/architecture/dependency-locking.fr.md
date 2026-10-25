# Verrouillage des dépendances

`pyproject.toml` énonce les contraintes. `uv.lock` est l'unique résolution de
ces contraintes. Les deux fichiers de requirements sont générés depuis
`uv.lock` et ne sont jamais modifiés à la main.

| Fichier | Généré par | Installé par |
|---|---|---|
| `requirements.lock` | `uv export --frozen --no-dev --no-emit-project -o requirements.lock` | `deploy/install.sh` (production) |
| `requirements-dev.lock` | `uv export --frozen --extra dev --no-emit-project -o requirements-dev.lock` | les jobs CI `backend`, `frontend` et nocturne `host-real`, ainsi que le développement local |

Les jobs CI `docs` et `frontend` exportent l'extra `docs` de `uv.lock` vers des
contraintes temporaires, puis installent MkDocs avec ces contraintes. Les outils
de documentation et leurs dépendances sont ainsi figés sans installer celles de
l'application dans le job documentaire. Aucune résolution distincte n'est maintenue.
CI audite cette sélection ainsi que les deux exports de dépendances.

## Modifier une dépendance

1. Modifiez `pyproject.toml`.
2. `uv lock`
3. Relancez les deux commandes d'export ci-dessus, exactement telles qu'elles
   sont écrites — uv enregistre sa liste d'arguments dans l'en-tête de chaque
   fichier, et la CI compare le diff.
4. `uv pip install --python .venv/bin/python -r requirements-dev.lock`
5. Exécutez les gates : ruff, ruff format, mypy, `pytest tests/python`, `pytest tests/python/tooling`.

## Pourquoi quatre garde-fous

Avant ces gates, `requirements-dev.lock` était produit par `uv pip compile`, qui
résolvait à nouveau depuis `pyproject.toml` indépendamment de `uv.lock`. Les deux
résultats divergeaient : dix paquets présents dans les deux fichiers — notamment
`sqlalchemy`, `pydantic`, `fastapi`, `uvicorn` et `libvirt-python` — avaient des
pins différents ; la CI ne testait donc jamais l'ensemble de dépendances livré
par la release. La même dérive introduisait `starlette 1.0.0` et
`python-multipart 0.0.27`, sept avis, dans les jobs CI `backend` et `frontend` et
le job nocturne `host-real`, tandis que la production livrait les versions corrigées.

| Gate | Où | Ce qu'il détecte |
|---|---|---|
| `uv lock --check` | job `dependency-audit` | `uv.lock` en retard sur `pyproject.toml` |
| régénération + `git diff --exit-code` | job `dependency-audit` | un fichier de requirements qui n'est pas un export de `uv.lock`, y compris toute modification manuelle |
| `tests/python/tooling/test_lockfile_consistency.py` | job unitaire, sans uv | des pins différents de `uv.lock` ou entre les deux fichiers |
| `pip-audit --strict` sur les deux fichiers | job `dependency-audit` | les avis connus dans l'un ou l'autre ensemble |

uv est verrouillé — `[tool.uv] required-version` localement et
`astral-sh/setup-uv` dans la CI — car la sortie de `uv export` n'est pas garantie
stable octet par octet entre les versions d'uv et le gate de diff compare les octets.
