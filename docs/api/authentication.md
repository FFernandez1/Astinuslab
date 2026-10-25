# Authentication

AstinusLab supports single-admin API authentication for deployments that bind the API/UI beyond loopback.

Default development mode keeps `auth.enabled = false` and binds to loopback. In that mode API endpoints are unauthenticated for trusted local lab use.

When `auth.enabled = true`, every request must authenticate with either an admin bearer token or the browser session cookie set by `/login` — except the public `/health` and `/ready` checks, `/login`, `/logout`, and `/static/*` assets. This covers the JSON API, the Playground endpoints, artifact downloads, `/openapi.json`, and the web UI pages themselves:

```bash
curl http://localhost:8000/api/v1/jobs \
  -H "Authorization: Bearer $ASTINUSLAB_ADMIN_API_TOKEN"
```

The only application role is `admin`. AstinusLab does not implement multi-user accounts, self-service registration, per-resource permissions, or RBAC.

## Configure Admin API Auth

Installed mode enables authentication during clean install. Installer generates one high-entropy token, stores only its `sha256:<64-hex>` hash in `astinuslab.toml`, and prints raw token once to installer terminal. Copy it immediately; installer never puts raw token in source, config, service arguments, or journal messages.

Installer auth flags:

- No auth flags: clean install generates token; update preserves existing valid hash.
- `--admin-token-hash sha256:<64-hex>`: use operator-provided hash without exposing raw token to installer arguments.
- `--generate-admin-token`: force generation only when no hash exists.
- `--allow-unauthenticated-local`: explicit development-only no-auth mode.
- `--generate-admin-token` conflicts with `--admin-token-hash` and `--allow-unauthenticated-local`; `--admin-token-hash` conflicts with `--allow-unauthenticated-local`. Existing hash plus any rotation/no-auth request fails rather than rotating silently.

Development checkout may keep `auth.enabled = false` on loopback. Non-loopback binding still fails closed unless auth is enabled or `allow_unsafe_network_bind_without_auth = true` is explicitly configured.

In-app path (available once `auth.enabled = true`): open Security, use **Generate API key**, copy the raw token immediately, and store only the generated SHA-256 hash in AstinusLab config. The raw token is shown once and is not persisted by the UI. The generator is hidden while auth is disabled, so bootstrap the first key with the headless path below.

Headless path: generate a random token and store only its SHA-256 hash:

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

Set either `astinuslab.toml`:

```toml
[auth]
enabled = true
admin_api_token_hash = "sha256:<64-hex-sha256-of-random-token>"
```

or environment variables:

```bash
ASTINUSLAB_AUTH_ENABLED=true
ASTINUSLAB_ADMIN_API_TOKEN_HASH=sha256:<64-hex-sha256-of-random-token>
```

Keep the raw bearer token in an operator secret manager or shell environment. Do not commit it to the repository.

## What to Expect When Auth Is Enabled

Turning auth on changes the whole app, not just the JSON API:

- **Every non-public route is gated** except `/health`, `/ready`, `/login`, `/logout`, and `/static/*` — the HTML UI pages (`/`, `/jobs`, `/playground`, Security, Settings), the API, artifact downloads, and `/openapi.json` all require authentication. API/fetch clients without a valid bearer token still receive `401` with body `{"detail": "admin bearer token required"}` and a `WWW-Authenticate: Bearer` header.
- **Browser UI remains usable through `/login`.** A browser navigation without a valid session cookie redirects to `/login?next=<path>`. Submitting the admin token issues an independent random browser-session cookie with HttpOnly and SameSite=Strict flags. The server enforces a fixed 12-hour expiration, and logout revokes that session immediately. The database stores only a hash of the session credential. Rotating the configured admin token invalidates all existing browser sessions; API bearer credentials keep their existing behavior. Upgrading from the old raw-token cookie requires signing in again.
- **The in-app key generator is itself behind auth.** The **Generate API key** control lives on the Security page, which needs authentication to open, and it is hidden entirely while auth is disabled. Bootstrap the first key with the headless path above, enable auth, log in at `/login`, then use the in-app generator for later rotations.
- **There is one role, `admin`.** A valid token grants full access; there are no scoped or read-only tokens.

## Browser Mutation Protection

Loopback binding is not an authorization boundary. Any page the operator visits can make the
browser issue requests to `http://127.0.0.1:8000`, and with `auth.enabled = false` those requests
carry no credential at all — the browser's reachability *is* the credential. Two guards run on every
unsafe method (`POST`, `PUT`, `PATCH`, `DELETE`) regardless of whether auth is enabled.

**Origin guard.** A request is refused with `403` and code `cross_origin_forbidden` when it carries
`Sec-Fetch-Site: cross-site`, or an `Origin` header that is not in the allowed set. Requests with no
`Origin` header — `curl`, SDKs, CI clients — are not browser navigations and pass this check.

The allowed set defaults to the loopback origins on the configured API port:
`http://127.0.0.1:<api_port>`, `http://localhost:<api_port>`, and `http://[::1]:<api_port>`. Override
it when AstinusLab is served through a reverse proxy or a different hostname:

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

```bash
ASTINUSLAB_BROWSER_ALLOWED_ORIGINS=https://astinuslab.example
```

Each entry is an exact scheme, host, and port; the default port is implied for `http` and `https`.
Paths, query strings, credentials, and wildcards are rejected at startup, so a malformed allowlist
fails the service rather than widening the boundary. Setting the key replaces the loopback defaults
— list every origin the UI is served from.

**CSRF token.** Unsafe requests must echo the `astinuslab_csrf` cookie back in the
`X-CSRF-Token` header, or in a `csrf_token` form field for `application/x-www-form-urlencoded` and
`multipart/form-data` posts. A missing token is `403` with code `csrf_required`; a mismatched one is
`403` with code `csrf_invalid`. The cookie is `SameSite=Strict`, `Secure` on HTTPS, and deliberately
readable by JavaScript so the page can echo it — the guard is the echo, not the secrecy of the
cookie. The token rotates on login and is cleared on logout.

Requests carrying a valid `Authorization: Bearer` admin token skip the CSRF check: a bearer token is
explicit authority the browser never attaches on its own. The Origin guard still applies to them.

The bundled UI needs no extra work — `src/astinuslab/web/templates/partials/csrf_input.html` supplies the
hidden form field, and `src/astinuslab/web/static/csrf.js` reads the `astinuslab-csrf-token` meta tag and adds
the header to `fetch` calls and HTMX requests. Custom browser clients must do the same. Non-browser
clients should authenticate with a bearer token instead; a cookie-only client (including a test
harness built on a bare HTTP client) has to seed the cookie/header pair itself.

## Network Bind Safety

AstinusLab refuses to start with a non-loopback `api_host` unless one of these is true:

- `auth.enabled = true` and `admin_api_token_hash` is configured.
- `allow_unsafe_network_bind_without_auth = true` is explicitly set for a controlled local experiment.

Use loopback (`127.0.0.1`, `::1`, or `localhost`) for normal development.

## Before Exposing the App

API bearer auth protects API endpoints from unauthenticated network callers, but it is not a complete internet-facing security boundary. For shared networks or remote use:

- Put AstinusLab behind TLS, preferably through a reverse proxy or VPN.
- Restrict source IPs with firewall rules.
- Rotate the admin token if it may have leaked.
- Prefer SSH tunnels for ad-hoc remote operation.
- Do not expose vulnerable guest services directly to untrusted networks.

## Artifact Access

Artifact download endpoints require the admin bearer token when auth is enabled:

- `GET /api/v1/jobs/{job_id}/artifact`
- `GET /api/v1/jobs/{job_id}/disk-artifact`
- `GET /api/v1/artifacts/{artifact_id}/download`
- `GET /api/v1/artifacts/{artifact_id}/manifest`
- `GET /api/v1/artifacts/{artifact_id}/sha256`

Treat bearer tokens and captured artifacts as sensitive.
