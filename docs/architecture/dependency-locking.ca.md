# Bloqueig de dependències

`pyproject.toml` declara les restriccions. `uv.lock` és l'única resolució
d'aquestes restriccions. Tots dos fitxers de requisits es generen a partir de
`uv.lock` i mai s'editen manualment.

| Fitxer | Generat per | Instal·lat per |
|---|---|---|
| `requirements.lock` | `uv export --frozen --no-dev --no-emit-project -o requirements.lock` | `deploy/install.sh` (producció) |
| `requirements-dev.lock` | `uv export --frozen --extra dev --no-emit-project -o requirements-dev.lock` | els jobs CI `backend`, `frontend` i el nocturn `host-real`, i el desenvolupament local |

Els jobs CI `docs` i `frontend` exporten l'extra `docs` de `uv.lock` com a
restriccions temporals i instal·len MkDocs amb aquestes restriccions. Això fixa
les eines de documentació i les seves dependències sense instal·lar les de
l'aplicació al job exclusiu de documentació. No es manté una resolució separada.
CI audita aquesta selecció juntament amb les dues exportacions de requisits.

## Canviar una dependència

1. Edita `pyproject.toml`.
2. `uv lock`
3. Torna a executar les dues ordres d'exportació anteriors, exactament tal com
   estan escrites — uv registra la seva llista d'arguments a la capçalera de
   cada fitxer, i CI en compara el diff.
4. `uv pip install --python .venv/bin/python -r requirements-dev.lock`
5. Executa els gates: ruff, ruff format, mypy, `pytest tests/python`, `pytest tests/python/tooling`.

## Per què hi ha quatre comprovacions

Abans d'aquests gates, `requirements-dev.lock` es produïa amb `uv pip compile`,
que resolia de nou a partir de `pyproject.toml` independentment de `uv.lock`.
Les dues respostes divergien: deu paquets presents en tots dos fitxers —
incloent-hi `sqlalchemy`, `pydantic`, `fastapi`, `uvicorn` i `libvirt-python` —
tenien pins diferents, de manera que CI mai provava el conjunt de dependències
que publica la release. La mateixa divergència portava `starlette 1.0.0` i
`python-multipart 0.0.27`, set avisos, als jobs CI `backend` i `frontend` i al
job nocturn `host-real`, mentre que producció enviava les versions corregides.

| Gate | On | Què detecta |
|---|---|---|
| `uv lock --check` | job `dependency-audit` | `uv.lock` endarrerit respecte de `pyproject.toml` |
| regeneració + `git diff --exit-code` | job `dependency-audit` | un fitxer de requisits que no és una exportació de `uv.lock`, inclosa qualsevol edició manual |
| `tests/python/tooling/test_lockfile_consistency.py` | job unitari, no necessita uv | pins que discrepen de `uv.lock` o entre els dos fitxers |
| `pip-audit --strict` als dos fitxers | job `dependency-audit` | avisos coneguts en qualsevol dels conjunts |

uv està fixat — `[tool.uv] required-version` localment i `astral-sh/setup-uv` a
CI — perquè la sortida d'`uv export` no té garantida l'estabilitat byte a byte
entre versions d'uv i el gate de diff compara els bytes.
