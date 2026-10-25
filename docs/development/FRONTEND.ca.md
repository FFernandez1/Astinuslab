# Arquitectura del frontend i desenvolupament

La UI d'AstinusLab es renderitza al servidor i es millora progressivament. No hi
ha single-page app ni router del costat client: les pàgines són plantilles
Jinja2, HTMX gestiona les actualitzacions in-place i el comportament específic de
cada pàgina viu en petits mòduls ES de JavaScript vanilla.

## Stack

*   **Plantilles**: Jinja2, servides per FastAPI.
*   **Interactivitat**: [HTMX](https://htmx.org/) per a actualitzacions parcials i enviaments de formularis.
*   **Scripting**: Mòduls ES vanilla (`<script type="module">`). Sense framework, sense bundler, sense pas de build: els fitxers sota `src/astinuslab/web/static/` se serveixen tal com són.
*   **Estils**: CSS pla sota `src/astinuslab/web/static/css/`.

### Per què no hi ha framework

La Content-Security-Policy definida a `src/astinuslab/main.py` fa servir:

```
script-src 'self' 'nonce-{nonce}'
```

No hi ha `'unsafe-eval'` ni `'unsafe-inline'` per a scripts. Els frameworks que
avaluen strings d'expressions en runtime (Alpine.js i similars) necessiten
`'unsafe-eval'`, de manera que no poden executar-se sota aquesta política. És una
restricció de seguretat deliberada, no un descuit: mantén la lògica client en
mòduls ES reals i dona a qualsevol `<script>` inline el nonce de la petició.

## Convencions d'HTMX

*   **La gestió global d'errors** viu a `src/astinuslab/web/static/toast.js`. Escolta a
    `document.body` els esdeveniments `htmx:responseError` (no 2xx) i
    `htmx:sendError` (fallada de xarxa) i mostra un toast mitjançant
    `window.showToast`. Els handlers individuals no necessiten els seus propis
    toasts d'error.
*   **Les confirmacions amb estil** viuen a
    `src/astinuslab/web/static/confirm-dialog.js`. Intercepta l'esdeveniment
    `htmx:confirm` d'HTMX i també gestiona `data-confirm="…"` en formularis i
    botons normals, substituint el `confirm()` natiu del navegador per una modal
    accessible. Afegeix `data-confirm` en lloc de cablejar el teu propi diàleg.
*   **Els estats de càrrega** combinen `hx-indicator` (què mostrar mentre una
    petició està en vol) amb `hx-disabled-elt` (què desactivar perquè no es pugui
    enviar dues vegades).
*   **Els fragments amb polling** (per exemple, estats que es refresquen en un
    interval) ometen `hx-indicator` de manera intencional: un spinner que
    parpelleja cada pocs segons és soroll, no feedback.

## Patró de store / mòduls

Les pàgines més grans divideixen el seu JavaScript en un petit conjunt de mòduls
en lloc d'un únic fitxer. `src/astinuslab/web/static/playground/` és la implementació de
referència:

*   `state.js` conté un **únic objecte d'estat** més `getState()` i
    `setState(updater)`. `setState` accepta un objecte parcial o una funció
    updater.
*   Els mòduls de render **se subscriuen** amb un selector:
    `subscribe(selector, fn)`. Després de cada `setState`, el `fn` d'un
    subscriptor s'executa només quan canvia la seva slice seleccionada (una
    comprovació d'igualtat superficial bloqueja re-renders), de manera que les
    actualitzacions no relacionades no repinten tota la pàgina.
*   `api.js` embolcalla totes les crides `fetch` perquè els errors emergeixin de
    manera uniforme i els mòduls consumeixin una forma de DTO estable.

`src/astinuslab/web/static/volatility/` segueix el mateix layout de mòduls: `state.js`,
`api.js` i mòduls de funcionalitat (`catalog`, `run`, `results`, `queue`, `isf`)
cablejats per `index.js`. El seu `state.js` és un objecte mutable compartit més
simple (el render es crida imperativament en lloc de mitjançant `subscribe`); la
divisió en mòduls de state / api / funcionalitat és el patró a copiar.

## Regla pràctica

El JavaScript nou a nivell de pàgina comença com **un mòdul**. Divideix-lo en
mòduls `state` / `api` / `render-*` (o de funcionalitat) quan superi
aproximadament les **300 línies**: és llavors quan un únic fitxer comença a
amagar la seva pròpia estructura.
