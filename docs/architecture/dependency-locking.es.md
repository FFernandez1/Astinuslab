# Bloqueo de dependencias

`pyproject.toml` declara las restricciones. `uv.lock` es la única resolución de
estas. Ambos archivos de requisitos se generan a partir de `uv.lock` y nunca se
editan manualmente.

| Archivo | Generado por | Instalado por |
|---|---|---|
| `requirements.lock` | `uv export --frozen --no-dev --no-emit-project -o requirements.lock` | `deploy/install.sh` (producción) |
| `requirements-dev.lock` | `uv export --frozen --extra dev --no-emit-project -o requirements-dev.lock` | los jobs CI `backend`, `frontend` y el nocturno `host-real`, y el desarrollo local |

Los jobs CI `docs` y `frontend` exportan el extra `docs` de `uv.lock` como
restricciones temporales e instalan MkDocs con ellas. Así se fijan las herramientas
de documentación y sus dependencias sin instalar las de la aplicación en el job
exclusivo de documentación. No se mantiene una resolución independiente.
CI audita esta selección junto con las dos exportaciones de requisitos.

## Cambiar una dependencia

1. Edita `pyproject.toml`.
2. `uv lock`
3. Vuelve a ejecutar los dos comandos de exportación anteriores, exactamente
   como están escritos — uv registra su propia lista de argumentos en la
   cabecera de cada archivo, y CI compara el diff.
4. `uv pip install --python .venv/bin/python -r requirements-dev.lock`
5. Ejecuta los gates: ruff, ruff format, mypy, `pytest tests/python`, `pytest tests/python/tooling`.

## Por qué hay cuatro comprobaciones

Antes de estos gates, `requirements-dev.lock` se producía mediante `uv pip compile`,
que volvía a resolver desde `pyproject.toml` de forma independiente de `uv.lock`.
Las dos respuestas divergían: diez paquetes presentes en ambos archivos —
incluidos `sqlalchemy`, `pydantic`, `fastapi`, `uvicorn` y `libvirt-python` —
tenían pins diferentes, por lo que CI nunca probaba el conjunto de dependencias
que publica la release. La misma divergencia llevaba `starlette 1.0.0` y
`python-multipart 0.0.27`, siete avisos, a los jobs CI `backend` y `frontend` y
al job nocturno `host-real`, mientras producción enviaba las versiones corregidas.

| Gate | Dónde | Qué detecta |
|---|---|---|
| `uv lock --check` | job `dependency-audit` | `uv.lock` desactualizado respecto a `pyproject.toml` |
| regeneración + `git diff --exit-code` | job `dependency-audit` | un archivo de requisitos que no es una exportación de `uv.lock`, incluida cualquier edición manual |
| `tests/python/tooling/test_lockfile_consistency.py` | job unitario, no necesita uv | pins que discrepan de `uv.lock` o entre ambos archivos |
| `pip-audit --strict` en ambos archivos | job `dependency-audit` | avisos conocidos en cualquiera de los conjuntos |

uv está fijado — `[tool.uv] required-version` localmente y `astral-sh/setup-uv` en
CI — porque la salida de `uv export` no tiene estabilidad byte a byte garantizada
entre versiones de uv y el gate de diff compara los bytes.
