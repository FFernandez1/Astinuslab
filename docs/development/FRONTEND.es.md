# Arquitectura del frontend y desarrollo

La UI de AstinusLab se renderiza en el servidor y se mejora progresivamente. No
hay single-page app ni router del lado cliente: las páginas son plantillas
Jinja2, HTMX gestiona las actualizaciones in-place y el comportamiento
específico de cada página vive en pequeños módulos ES de JavaScript vanilla.

## Stack

*   **Plantillas**: Jinja2, servidas por FastAPI.
*   **Interactividad**: [HTMX](https://htmx.org/) para actualizaciones parciales y envíos de formularios.
*   **Scripting**: Módulos ES vanilla (`<script type="module">`). Sin framework, sin bundler, sin paso de build: los archivos bajo `src/astinuslab/web/static/` se sirven tal cual.
*   **Estilos**: CSS plano bajo `src/astinuslab/web/static/css/`.

### Por qué no hay framework

La Content-Security-Policy definida en `src/astinuslab/main.py` usa:

```
script-src 'self' 'nonce-{nonce}'
```

No hay `'unsafe-eval'` ni `'unsafe-inline'` para scripts. Los frameworks que
evalúan strings de expresiones en runtime (Alpine.js y similares) necesitan
`'unsafe-eval'`, por lo que no pueden ejecutarse bajo esta política. Es una
restricción de seguridad deliberada, no un descuido: mantén la lógica cliente en
módulos ES reales y da a cualquier `<script>` inline el nonce de la petición.

## Convenciones de HTMX

*   **Manejo global de errores** vive en `src/astinuslab/web/static/toast.js`. Escucha en
    `document.body` los eventos `htmx:responseError` (no 2xx) y `htmx:sendError`
    (fallo de red) y muestra un toast mediante `window.showToast`. Los handlers
    individuales no necesitan sus propios toasts de error.
*   **Confirmaciones con estilo** viven en `src/astinuslab/web/static/confirm-dialog.js`.
    Intercepta el evento `htmx:confirm` de HTMX y también maneja
    `data-confirm="…"` en formularios y botones normales, reemplazando el
    `confirm()` nativo del navegador por un modal accesible. Añade
    `data-confirm` en lugar de cablear tu propio diálogo.
*   **Estados de carga** combinan `hx-indicator` (qué mostrar mientras una
    petición está en vuelo) con `hx-disabled-elt` (qué desactivar para que no se
    pueda enviar dos veces).
*   **Fragmentos con polling** (por ejemplo, estados que se refrescan en un
    intervalo) omiten `hx-indicator` de forma intencional: un spinner que
    parpadea cada pocos segundos es ruido, no feedback.

## Patrón de store / módulos

Las páginas más grandes dividen su JavaScript en un pequeño conjunto de módulos
en lugar de un único archivo. `src/astinuslab/web/static/playground/` es la implementación
de referencia:

*   `state.js` contiene un **único objeto de estado** más `getState()` y
    `setState(updater)`. `setState` acepta un objeto parcial o una función
    updater.
*   Los módulos de render **se suscriben** con un selector:
    `subscribe(selector, fn)`. Tras cada `setState`, el `fn` de un suscriptor se
    ejecuta solo cuando cambia su slice seleccionado (una comprobación de
    igualdad superficial bloquea re-renders), por lo que las actualizaciones no
    relacionadas no repintan toda la página.
*   `api.js` envuelve todas las llamadas `fetch` para que los errores se
    expongan de forma uniforme y los módulos consuman una forma de DTO estable.

`src/astinuslab/web/static/volatility/` sigue el mismo layout de módulos: `state.js`,
`api.js` y módulos de funcionalidad (`catalog`, `run`, `results`, `queue`,
`isf`) cableados por `index.js`. Su `state.js` es un objeto mutable compartido
más simple (el render se llama imperativamente en lugar de mediante
`subscribe`); la división en módulos de state / api / funcionalidad es el patrón
a copiar.

## Regla práctica

El JavaScript nuevo a nivel de página empieza como **un módulo**. Divídelo en
módulos `state` / `api` / `render-*` (o de funcionalidad) cuando supere
aproximadamente las **300 líneas**: es entonces cuando un archivo único empieza a
ocultar su propia estructura.
