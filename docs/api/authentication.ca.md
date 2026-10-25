# Autenticació

AstinusLab admet autenticació d'API per a un únic administrador en desplegaments que enllacen l'API/UI més enllà de loopback.

El mode de desenvolupament predeterminat manté `auth.enabled = false` i enllaça a loopback. En aquest mode, els endpoints d'API no tenen autenticació per a ús de lab local de confiança.

Quan `auth.enabled = true`, cada petició s'ha d'autenticar amb un token bearer d'admin o amb la cookie de sessió del navegador definida per `/login`, excepte els checks públics `/health` i `/ready`, `/login`, `/logout` i els assets `/static/*`. Això cobreix l'API JSON, els endpoints de Playground, les descàrregues d'artefactes, `/openapi.json` i les pròpies pàgines de la UI web:

```bash
curl http://localhost:8000/api/v1/jobs   -H "Authorization: Bearer $ASTINUSLAB_ADMIN_API_TOKEN"
```

L'únic rol de l'aplicació és `admin`. AstinusLab no implementa comptes multiusuari, registre self-service, permisos per recurs ni RBAC.

## Configurar autenticació d'API admin

El mode instal·lat activa l'autenticació durant la instal·lació neta. L'instal·lador genera un token
d'alta entropia, desa només el seu hash `sha256:<64-hex>` a `astinuslab.toml` i imprimeix el token
una sola vegada al terminal. Copia'l immediatament; l'instal·lador mai escriu el token en brut al
codi, la configuració, els arguments del servei ni el journal.

Opcions d'autenticació de l'instal·lador:

- Sense opcions: la instal·lació neta genera el token; l'actualització conserva el hash existent.
- `--admin-token-hash sha256:<64-hex>`: usa un hash proporcionat per l'operador.
- `--generate-admin-token`: força la generació només quan no hi ha hash.
- `--allow-unauthenticated-local`: mode sense autenticació, només per a desenvolupament explícit.
- `--generate-admin-token` és incompatible amb `--admin-token-hash` i
  `--allow-unauthenticated-local`; `--admin-token-hash` és incompatible amb
  `--allow-unauthenticated-local`. Un hash existent més qualsevol petició de rotació o de mode sense
  autenticació falla en lloc de rotar en silenci.

Un checkout de desenvolupament pot mantenir `auth.enabled = false` en loopback. L'enllaç no-loopback
continua fallant tancat si l'autenticació no està activada o no es configura explícitament
`allow_unsafe_network_bind_without_auth = true`.

Ruta dins de l'app (disponible un cop `auth.enabled = true`): obre Security, fes servir **Generate API key**, copia el token brut immediatament i desa només el hash SHA-256 generat a la configuració d'AstinusLab. El token brut es mostra una sola vegada i la UI no el persisteix. El generador queda ocult mentre l'autenticació està desactivada, així que inicialitza la primera clau amb la ruta headless de sota.

Ruta headless: genera un token aleatori i desa només el seu hash SHA-256:

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

Defineix `astinuslab.toml`:

```toml
[auth]
enabled = true
admin_api_token_hash = "sha256:<64-hex-sha256-of-random-token>"
```

o variables d'entorn:

```bash
ASTINUSLAB_AUTH_ENABLED=true
ASTINUSLAB_ADMIN_API_TOKEN_HASH=sha256:<64-hex-sha256-of-random-token>
```

Mantén el token bearer brut en un gestor de secrets d'operador o a l'entorn de shell. No el facis commit al repositori.

## Què esperar quan l'autenticació està activada

Activar l'autenticació canvia tota l'app, no només l'API JSON:

- **Totes les rutes no públiques queden protegides** excepte `/health`, `/ready`, `/login`, `/logout` i `/static/*`: les pàgines HTML de la UI (`/`, `/jobs`, `/playground`, Security, Settings), l'API, les descàrregues d'artefactes i `/openapi.json` requereixen autenticació. Els clients API/fetch sense bearer vàlid continuen rebent `401` amb cos `{"detail": "admin bearer token required"}` i capçalera `WWW-Authenticate: Bearer`.
- **La UI del navegador continua sent usable mitjançant `/login`.** Una navegació de navegador sense cookie de sessió vàlida redirigeix a `/login?next=<path>`. Enviar el mateix token admin defineix una cookie HttpOnly, SameSite=Strict durant 12 hores, de manera que les pàgines renderitzades pel servidor i els formularis ordinaris funcionen mentre l'autenticació continua activada.
- **El generador de claus dins de l'app també està darrere de l'autenticació.** El control **Generate API key** viu a la pàgina Security, que necessita autenticació per obrir-se, i està completament ocult mentre l'autenticació està desactivada. Inicialitza la primera clau amb la ruta headless anterior, activa l'autenticació, inicia sessió a `/login` i després usa el generador intern per a rotacions posteriors.
- **Hi ha un únic rol, `admin`.** Un token vàlid concedeix accés complet; no hi ha tokens amb scope ni de només lectura.

## Protecció de mutacions del navegador

L'enllaç a loopback no és un límit d'autorització. Qualsevol pàgina que visiti l'operador pot fer
que el navegador enviï peticions a `http://127.0.0.1:8000`, i amb `auth.enabled = false` aquestes
peticions no porten cap credencial: l'abast del navegador *és* la credencial. Dues guardes
s'executen a cada mètode no segur (`POST`, `PUT`, `PATCH`, `DELETE`), tant si l'autenticació està
activada com si no.

**Guarda d'Origin.** La petició es rebutja amb `403` i codi `cross_origin_forbidden` quan porta
`Sec-Fetch-Site: cross-site`, o una capçalera `Origin` que no és al conjunt permès. Les peticions
sense capçalera `Origin` — `curl`, SDK, clients de CI — no són navegacions i passen aquesta
comprovació.

Per defecte el conjunt permès són els orígens loopback al port configurat:
`http://127.0.0.1:<api_port>`, `http://localhost:<api_port>` i `http://[::1]:<api_port>`.
Substitueix-lo quan AstinusLab se serveixi darrere d'un proxy invers o amb un altre nom de host:

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

```bash
ASTINUSLAB_BROWSER_ALLOWED_ORIGINS=https://astinuslab.example
```

Cada entrada és un esquema, host i port exactes. Rutes, credencials i comodins es rebutgen en
arrencar, de manera que una llista mal formada atura el servei en lloc d'ampliar el límit. Definir
la clau **reemplaça** els valors loopback per defecte: enumera tots els orígens des dels quals se
serveix la UI.

**Token CSRF.** Les peticions no segures han de retornar la cookie `astinuslab_csrf` a la capçalera
`X-CSRF-Token`, o al camp de formulari `csrf_token` per a enviaments
`application/x-www-form-urlencoded` i `multipart/form-data`. Un token absent és `403` amb codi
`csrf_required`; un que no coincideix és `403` amb codi `csrf_invalid`. La cookie és
`SameSite=Strict`, `Secure` en HTTPS i llegible per JavaScript a propòsit, perquè la pàgina la pugui
retornar: la guarda és la devolució, no el secret de la cookie. El token rota en iniciar sessió i
s'esborra en tancar-la.

Les peticions amb un token `Authorization: Bearer` vàlid ometen la comprovació CSRF: un bearer és
autoritat explícita que el navegador mai adjunta pel seu compte. La guarda d'Origin continua
aplicant-se.

La UI inclosa no necessita feina extra: `src/astinuslab/web/templates/partials/csrf_input.html` aporta el camp
ocult i `src/astinuslab/web/static/csrf.js` llegeix la meta `astinuslab-csrf-token` i afegeix la capçalera a
les crides `fetch` i a les peticions HTMX. Els clients de navegador propis han de fer el mateix. Els
clients que no són navegador s'haurien d'autenticar amb un bearer; un client només amb cookies
(inclòs un arnès de proves sobre un client HTTP simple) ha de sembrar ell mateix el parell
cookie/capçalera.

## Seguretat de l'enllaç de xarxa

AstinusLab es nega a arrencar amb un `api_host` que no sigui loopback llevat que una d'aquestes condicions sigui certa:

- `auth.enabled = true` i `admin_api_token_hash` està configurat.
- `allow_unsafe_network_bind_without_auth = true` està definit explícitament per a un experiment local controlat.

Fes servir loopback (`127.0.0.1`, `::1` o `localhost`) per al desenvolupament normal.

## Abans d'exposar l'app

L'autenticació bearer d'API protegeix els endpoints d'API davant cridadors de xarxa no autenticats, però no és un límit de seguretat complet de cara a internet. Per a xarxes compartides o ús remot:

- Col·loca AstinusLab darrere de TLS, preferiblement mitjançant un reverse proxy o una VPN.
- Restringeix les IP d'origen amb regles de firewall.
- Rota el token d'admin si pot haver-se filtrat.
- Prefereix túnels SSH per a operacions remotes puntuals.
- No exposis serveis guest vulnerables directament a xarxes no fiables.

## Accés a artefactes

Els endpoints de descàrrega d'artefactes requereixen el token bearer d'admin quan l'autenticació està activada:

- `GET /api/v1/jobs/{job_id}/artifact`
- `GET /api/v1/jobs/{job_id}/disk-artifact`
- `GET /api/v1/artifacts/{artifact_id}/download`
- `GET /api/v1/artifacts/{artifact_id}/manifest`
- `GET /api/v1/artifacts/{artifact_id}/sha256`

Tracta els tokens bearer i els artefactes capturats com a sensibles.
