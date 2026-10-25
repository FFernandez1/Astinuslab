# Architecture du frontend et développement

L'UI d'AstinusLab est rendue côté serveur et améliorée progressivement. Il n'y a
pas de single-page app ni de router côté client : les pages sont des templates
Jinja2, HTMX gère les mises à jour sur place, et le comportement propre à chaque
page vit dans de petits modules ES JavaScript vanilla.

## Stack

*   **Templates** : Jinja2, servis par FastAPI.
*   **Interactivité** : [HTMX](https://htmx.org/) pour les mises à jour partielles et les envois de formulaires.
*   **Scripting** : Modules ES vanilla (`<script type="module">`). Pas de framework, pas de bundler, pas d'étape de build : les fichiers sous `src/astinuslab/web/static/` sont servis tels quels.
*   **Styles** : CSS simple sous `src/astinuslab/web/static/css/`.

### Pourquoi pas de framework

La Content-Security-Policy définie dans `src/astinuslab/main.py` utilise :

```
script-src 'self' 'nonce-{nonce}'
```

Il n'y a pas de `'unsafe-eval'` ni de `'unsafe-inline'` pour les scripts. Les
frameworks qui évaluent des chaînes d'expressions au runtime (Alpine.js et
similaires) ont besoin de `'unsafe-eval'`, ils ne peuvent donc pas fonctionner
sous cette politique. C'est une contrainte de sécurité délibérée, pas un oubli :
gardez la logique cliente dans de vrais modules ES et donnez à tout `<script>`
inline le nonce de la requête.

## Conventions HTMX

*   **La gestion globale des erreurs** vit dans `src/astinuslab/web/static/toast.js`. Elle
    écoute sur `document.body` les événements `htmx:responseError` (non-2xx) et
    `htmx:sendError` (échec réseau), puis affiche un toast via
    `window.showToast`. Les handlers individuels n'ont pas besoin de leurs
    propres toasts d'erreur.
*   **Les confirmations stylées** vivent dans
    `src/astinuslab/web/static/confirm-dialog.js`. Ce module intercepte l'événement
    `htmx:confirm` de HTMX et gère aussi `data-confirm="…"` sur les formulaires
    et boutons simples, en remplaçant le `confirm()` natif du navigateur par une
    modale accessible. Ajoutez `data-confirm` plutôt que de câbler votre propre
    dialogue.
*   **Les états de chargement** associent `hx-indicator` (ce qu'il faut afficher
    pendant qu'une requête est en vol) à `hx-disabled-elt` (ce qu'il faut
    désactiver pour empêcher un double envoi).
*   **Les fragments en polling** (par exemple un statut rafraîchi à intervalle
    régulier) omettent intentionnellement `hx-indicator` : un spinner qui
    clignote toutes les quelques secondes est du bruit, pas du feedback.

## Pattern store / modules

Les pages plus grandes divisent leur JavaScript en un petit ensemble de modules
plutôt qu'un seul fichier. `src/astinuslab/web/static/playground/` est l'implémentation de
référence :

*   `state.js` contient un **unique objet d'état** plus `getState()` et
    `setState(updater)`. `setState` accepte soit un objet partiel, soit une
    fonction updater.
*   Les modules de rendu **s'abonnent** avec un sélecteur :
    `subscribe(selector, fn)`. Après chaque `setState`, le `fn` d'un abonné ne
    s'exécute que lorsque sa slice sélectionnée change (une vérification
    d'égalité superficielle bloque les re-renders), donc les mises à jour sans
    rapport ne repeignent pas toute la page.
*   `api.js` encapsule tous les appels `fetch` afin que les erreurs remontent de
    manière uniforme et que les modules consomment une forme de DTO stable.

`src/astinuslab/web/static/volatility/` suit le même layout de modules : `state.js`,
`api.js` et modules de fonctionnalité (`catalog`, `run`, `results`, `queue`,
`isf`) câblés par `index.js`. Son `state.js` est un objet mutable partagé plus
simple (le rendu est appelé impérativement plutôt que via `subscribe`) ; la
division en modules state / api / fonctionnalité est le pattern à copier.

## Règle pratique

Le nouveau JavaScript au niveau d'une page commence comme **un module**.
Divisez-le en modules `state` / `api` / `render-*` (ou de fonctionnalité) une
fois qu'il dépasse environ **300 lignes** : c'est à ce moment-là qu'un fichier
unique commence à masquer sa propre structure.
