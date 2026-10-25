# Autenticación

AstinusLab admite autenticación de API para un único administrador en despliegues que enlazan la API/UI más allá de loopback.

El modo de desarrollo predeterminado mantiene `auth.enabled = false` y enlaza a loopback. En ese modo, los endpoints de API no tienen autenticación para uso de lab local de confianza.

Cuando `auth.enabled = true`, cada petición debe autenticarse con un token bearer de admin o con la cookie de sesión del navegador definida por `/login`, excepto los checks públicos `/health` y `/ready`, `/login`, `/logout` y los assets `/static/*`. Esto cubre la API JSON, los endpoints de Playground, las descargas de artefactos, `/openapi.json` y las propias páginas de la UI web:

```bash
curl http://localhost:8000/api/v1/jobs   -H "Authorization: Bearer $ASTINUSLAB_ADMIN_API_TOKEN"
```

El único rol de la aplicación es `admin`. AstinusLab no implementa cuentas multiusuario, registro self-service, permisos por recurso ni RBAC.

## Configurar autenticación de API admin

El modo instalado activa la autenticación durante la instalación limpia. El instalador genera un
token de alta entropía, guarda solo su hash `sha256:<64-hex>` en `astinuslab.toml` e imprime el
token una sola vez en la terminal. Cópialo de inmediato; el instalador nunca escribe el token en
el código, la configuración, los argumentos del servicio ni el journal.

Flags de autenticación del instalador:

- Sin flags: la instalación limpia genera el token; la actualización conserva el hash existente.
- `--admin-token-hash sha256:<64-hex>`: usa un hash proporcionado por el operador.
- `--generate-admin-token`: fuerza la generación solo cuando no existe hash.
- `--allow-unauthenticated-local`: modo sin autenticación, solo para desarrollo explícito.
- `--generate-admin-token` entra en conflicto con `--admin-token-hash` y
  `--allow-unauthenticated-local`; `--admin-token-hash` entra en conflicto con
  `--allow-unauthenticated-local`. Un hash existente más cualquier petición de rotación o de modo
  sin autenticación falla en lugar de rotar en silencio.

Un checkout de desarrollo puede mantener `auth.enabled = false` en loopback. El enlace no-loopback
sigue fallando cerrado salvo que la autenticación esté activada o se configure explícitamente
`allow_unsafe_network_bind_without_auth = true`.

Ruta dentro de la app (disponible cuando `auth.enabled = true`): abre Security, usa **Generate API key**, copia el token sin procesar inmediatamente y guarda solo el hash SHA-256 generado en la configuración de AstinusLab. El token sin procesar se muestra una vez y la UI no lo persiste. El generador queda oculto mientras la autenticación está desactivada, así que inicializa la primera clave con la ruta headless de abajo.

Ruta headless: genera un token aleatorio y guarda solo su hash SHA-256:

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

Define `astinuslab.toml`:

```toml
[auth]
enabled = true
admin_api_token_hash = "sha256:<64-hex-sha256-of-random-token>"
```

o variables de entorno:

```bash
ASTINUSLAB_AUTH_ENABLED=true
ASTINUSLAB_ADMIN_API_TOKEN_HASH=sha256:<64-hex-sha256-of-random-token>
```

Mantén el token bearer sin procesar en un gestor de secretos de operador o en el entorno de shell. No lo hagas commit al repositorio.

## Qué esperar cuando la autenticación está activada

Activar la autenticación cambia toda la app, no solo la API JSON:

- **Todas las rutas no públicas quedan protegidas** excepto `/health`, `/ready`, `/login`, `/logout` y `/static/*`: las páginas HTML de la UI (`/`, `/jobs`, `/playground`, Security, Settings), la API, las descargas de artefactos y `/openapi.json` requieren autenticación. Los clientes API/fetch sin bearer válido siguen recibiendo `401` con cuerpo `{"detail": "admin bearer token required"}` y cabecera `WWW-Authenticate: Bearer`.
- **La UI del navegador sigue siendo usable mediante `/login`.** Una navegación de navegador sin cookie de sesión válida redirige a `/login?next=<path>`. Enviar el mismo token admin define una cookie HttpOnly, SameSite=Strict durante 12 horas, por lo que las páginas renderizadas por servidor y los formularios ordinarios funcionan mientras la autenticación sigue activada.
- **El generador de claves dentro de la app también está detrás de la autenticación.** El control **Generate API key** vive en la página Security, que necesita autenticación para abrirse, y está oculto por completo mientras la autenticación está desactivada. Inicializa la primera clave con la ruta headless anterior, activa la autenticación, inicia sesión en `/login` y usa después el generador interno para rotaciones posteriores.
- **Hay un único rol, `admin`.** Un token válido concede acceso completo; no hay tokens con scope ni de solo lectura.

## Protección de mutaciones del navegador

El enlace a loopback no es un límite de autorización. Cualquier página que visite el operador puede
hacer que el navegador envíe peticiones a `http://127.0.0.1:8000`, y con `auth.enabled = false` esas
peticiones no llevan ninguna credencial: la alcanzabilidad del navegador *es* la credencial. Dos
guardas se ejecutan en cada método no seguro (`POST`, `PUT`, `PATCH`, `DELETE`), esté o no activada
la autenticación.

**Guarda de Origin.** La petición se rechaza con `403` y código `cross_origin_forbidden` cuando
lleva `Sec-Fetch-Site: cross-site`, o una cabecera `Origin` que no está en el conjunto permitido.
Las peticiones sin cabecera `Origin` — `curl`, SDKs, clientes de CI — no son navegaciones de
navegador y pasan esta comprobación.

El conjunto permitido por defecto son los orígenes loopback en el puerto configurado:
`http://127.0.0.1:<api_port>`, `http://localhost:<api_port>` y `http://[::1]:<api_port>`.
Sustitúyelo cuando AstinusLab se sirva tras un proxy inverso u otro nombre de host:

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

```bash
ASTINUSLAB_BROWSER_ALLOWED_ORIGINS=https://astinuslab.example
```

Cada entrada es un esquema, host y puerto exactos. Rutas, credenciales y comodines se rechazan al
arrancar, así que una lista mal formada detiene el servicio en lugar de ampliar el límite. Definir
la clave **reemplaza** los valores loopback por defecto: enumera todos los orígenes desde los que se
sirve la UI.

**Token CSRF.** Las peticiones no seguras deben devolver la cookie `astinuslab_csrf` en la cabecera
`X-CSRF-Token`, o en el campo de formulario `csrf_token` para envíos
`application/x-www-form-urlencoded` y `multipart/form-data`. Un token ausente es `403` con código
`csrf_required`; uno que no coincide es `403` con código `csrf_invalid`. La cookie es
`SameSite=Strict`, `Secure` en HTTPS y legible por JavaScript a propósito, para que la página pueda
devolverla: la guarda es la devolución, no el secreto de la cookie. El token rota al iniciar sesión
y se borra al cerrarla.

Las peticiones con un token `Authorization: Bearer` válido omiten la comprobación CSRF: un bearer es
autoridad explícita que el navegador nunca adjunta por su cuenta. La guarda de Origin sigue
aplicándose.

La UI incluida no necesita trabajo extra: `src/astinuslab/web/templates/partials/csrf_input.html` aporta el
campo oculto y `src/astinuslab/web/static/csrf.js` lee la meta `astinuslab-csrf-token` y añade la cabecera a
las llamadas `fetch` y a las peticiones HTMX. Los clientes de navegador propios deben hacer lo
mismo. Los clientes que no son navegador deberían autenticarse con un bearer; un cliente basado solo
en cookies (incluido un arnés de pruebas sobre un cliente HTTP simple) tiene que sembrar él mismo el
par cookie/cabecera.

## Seguridad del enlace de red

AstinusLab se niega a arrancar con un `api_host` que no sea loopback salvo que una de estas condiciones sea cierta:

- `auth.enabled = true` y `admin_api_token_hash` está configurado.
- `allow_unsafe_network_bind_without_auth = true` está definido explícitamente para un experimento local controlado.

Usa loopback (`127.0.0.1`, `::1` o `localhost`) para el desarrollo normal.

## Antes de exponer la app

La autenticación bearer de API protege los endpoints de API frente a llamadores de red no autenticados, pero no es un límite de seguridad completo para internet. Para redes compartidas o uso remoto:

- Coloca AstinusLab detrás de TLS, preferiblemente mediante reverse proxy o VPN.
- Restringe las IP de origen con reglas de firewall.
- Rota el token de admin si puede haberse filtrado.
- Prefiere túneles SSH para operaciones remotas puntuales.
- No expongas servicios guest vulnerables directamente a redes no confiables.

## Acceso a artefactos

Los endpoints de descarga de artefactos requieren el token bearer de admin cuando la autenticación está activada:

- `GET /api/v1/jobs/{job_id}/artifact`
- `GET /api/v1/jobs/{job_id}/disk-artifact`
- `GET /api/v1/artifacts/{artifact_id}/download`
- `GET /api/v1/artifacts/{artifact_id}/manifest`
- `GET /api/v1/artifacts/{artifact_id}/sha256`

Trata los tokens bearer y los artefactos capturados como sensibles.
