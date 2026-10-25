# Contribuir

Usa esta página para el workflow de desarrollo. Los documentos comunitarios del proyecto permanecen bajo la sección Community.

Consulta la [matriz de verificación](verification-matrix.md), mantenida en inglés con fallback para evitar duplicar las instrucciones. Aísla el trabajo concurrente y conserva los cambios de otras tareas.

## Antes de abrir un Pull Request

1. Empieza desde una rama limpia.
2. Mantén los cambios acotados.
3. Añade o actualiza tests cuando cambie el comportamiento.
4. Actualiza la documentación cuando cambien comandos, APIs, configuración o workflows.
5. Ejecuta los comandos de validación local que correspondan al cambio.

## Validación local

Para cambios de backend:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

Para cambios de docs:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

Para helpers shell de host:

```bash
shellcheck ops/host/*.sh
```

## Estándares de código

- El código Python debe seguir los patrones existentes de FastAPI, Pydantic, SQLAlchemy y repositorios.
- Los scripts shell deben usar patrones Bash defensivos y `set -euo pipefail`.
- Los cambios de frontend deben seguir la estructura existente de Jinja2, HTMX y JavaScript vanilla.
- No añadas dependencias amplias salvo que las necesite el diseño del repositorio.

### Nombres de campos de error

Tres contextos, tres nombres: consulta [`docs/architecture/adr-error-field-naming.md`](../architecture/adr-error-field-naming.md). Lo aplica `tools/quality/audit_error_fields.py` (se ejecuta como `tests/python/tooling/test_error_field_audit.py`).

## Estándares de documentación

- Documenta comandos que existan en el repositorio.
- Mantén separadas las docs de uso, arquitectura, API y desarrollo.
- Marca los desconocidos explícitamente en lugar de inventar comportamiento.
- Conserva las funciones del tema MkDocs y el estilo de navegación existentes.

## Traducir la documentación

Las docs se localizan con el plugin `mkdocs-static-i18n`. English (`en`) es la única fuente de verdad; los locales soportados son `en`, `es`, `fr` y `ca` (en línea con la UI web).

- **English es autoritativo.** Edita siempre `page.md` primero. Las páginas públicas deben incluir siblings localizados para cada locale soportado para que los builds por idioma no caigan a contenido en English.
- **Añade una traducción** creando un archivo sibling con sufijo de locale junto a la página en English: `index.es.md`, `index.fr.md`, `index.ca.md`, `usage/getting-started.es.md`, etc. Mantén el mismo filename, enlaces y estructura de headings que la fuente English; los términos de producto (Playground, Jobs, Target, Workload, Attacker, Acquisition, Volatility 3) permanecen sin traducir.
- **Los títulos de sección** en la navegación izquierda se traducen mediante `nav_translations` bajo cada locale en `docs/mkdocs.yml`. Añade una entrada solo cuando introduzcas un nuevo grupo de nav o etiqueta de página.
- **No traduzcas en masa.** Traduce una página solo cuando pueda mantenerse sincronizada con su fuente English; mantén los archivos internos de planificación fuera de los builds publicados mediante `exclude_docs` cuando no se mantengan como docs públicas.

Construye para confirmar que todos los locales renderizan:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```
