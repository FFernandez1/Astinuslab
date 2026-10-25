# Authentification

AstinusLab prend en charge une authentification d'API pour un administrateur unique dans les déploiements qui exposent l'API/UI au-delà de loopback.

Le mode de développement par défaut garde `auth.enabled = false` et écoute sur loopback. Dans ce mode, les endpoints d'API ne sont pas authentifiés pour un usage de lab local de confiance.

Quand `auth.enabled = true`, chaque requête doit s'authentifier avec un token bearer admin ou avec le cookie de session navigateur défini par `/login`, sauf les checks publics `/health` et `/ready`, `/login`, `/logout` et les assets `/static/*`. Cela couvre l'API JSON, les endpoints Playground, les téléchargements d'artefacts, `/openapi.json` et les pages de l'UI web elles-mêmes :

```bash
curl http://localhost:8000/api/v1/jobs   -H "Authorization: Bearer $ASTINUSLAB_ADMIN_API_TOKEN"
```

Le seul rôle applicatif est `admin`. AstinusLab n'implémente pas de comptes multi-utilisateurs, d'inscription self-service, de permissions par ressource ni de RBAC.

## Configurer l'authentification d'API admin

Le mode installé active l'authentification lors d'une installation propre. L'installateur génère un
token à haute entropie, ne conserve que son hash `sha256:<64-hex>` dans `astinuslab.toml` et affiche
le token une seule fois dans le terminal. Copiez-le immédiatement ; l'installateur ne place jamais
le token brut dans le code, la configuration, les arguments de service ou le journal.

Options d'authentification de l'installateur :

- Sans option : l'installation propre génère le token ; la mise à jour conserve le hash existant.
- `--admin-token-hash sha256:<64-hex>` : utilise un hash fourni par l'opérateur.
- `--generate-admin-token` : force la génération uniquement si aucun hash n'existe.
- `--allow-unauthenticated-local` : mode sans authentification, réservé au développement.
- `--generate-admin-token` est incompatible avec `--admin-token-hash` et
  `--allow-unauthenticated-local` ; `--admin-token-hash` est incompatible avec
  `--allow-unauthenticated-local`. Un hash existant plus une demande de rotation ou de mode sans
  authentification échoue au lieu de tourner silencieusement.

Une copie de travail de développement peut garder `auth.enabled = false` en loopback. Une écoute
non-loopback échoue toujours à la fermeture sauf si l'authentification est activée ou si
`allow_unsafe_network_bind_without_auth = true` est explicitement configuré.

Chemin dans l'app (disponible une fois `auth.enabled = true`) : ouvrez Security, utilisez **Generate API key**, copiez immédiatement le token brut et ne stockez que le hash SHA-256 généré dans la configuration AstinusLab. Le token brut est affiché une seule fois et n'est pas persisté par l'UI. Le générateur est masqué tant que l'authentification est désactivée ; amorcez donc la première clé avec le chemin headless ci-dessous.

Chemin headless : générez un token aléatoire et ne stockez que son hash SHA-256 :

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

Définissez soit `astinuslab.toml` :

```toml
[auth]
enabled = true
admin_api_token_hash = "sha256:<64-hex-sha256-of-random-token>"
```

soit des variables d'environnement :

```bash
ASTINUSLAB_AUTH_ENABLED=true
ASTINUSLAB_ADMIN_API_TOKEN_HASH=sha256:<64-hex-sha256-of-random-token>
```

Conservez le token bearer brut dans un gestionnaire de secrets opérateur ou dans l'environnement shell. Ne le commitez pas dans le dépôt.

## À quoi s'attendre quand l'authentification est activée

Activer l'authentification modifie toute l'app, pas seulement l'API JSON :

- **Toutes les routes non publiques sont protégées** sauf `/health`, `/ready`, `/login`, `/logout` et `/static/*` : les pages HTML de l'UI (`/`, `/jobs`, `/playground`, Security, Settings), l'API, les téléchargements d'artefacts et `/openapi.json` exigent une authentification. Les clients API/fetch sans bearer valide reçoivent toujours `401` avec le corps `{"detail": "admin bearer token required"}` et un en-tête `WWW-Authenticate: Bearer`.
- **L'UI navigateur reste utilisable via `/login`.** Une navigation de navigateur sans cookie de session valide redirige vers `/login?next=<path>`. Envoyer le même token admin définit un cookie HttpOnly, SameSite=Strict pendant 12 heures, afin que les pages rendues côté serveur et les formulaires ordinaires fonctionnent pendant que l'authentification reste activée.
- **Le générateur de clés intégré est lui-même derrière l'authentification.** Le contrôle **Generate API key** se trouve sur la page Security, qui nécessite une authentification pour s'ouvrir, et il est entièrement masqué tant que l'authentification est désactivée. Amorcez la première clé avec le chemin headless ci-dessus, activez l'authentification, connectez-vous sur `/login`, puis utilisez le générateur intégré pour les rotations ultérieures.
- **Il n'y a qu'un rôle, `admin`.** Un token valide donne un accès complet ; il n'existe pas de tokens scopés ou en lecture seule.

## Protection des mutations du navigateur

L'écoute en loopback n'est pas une frontière d'autorisation. N'importe quelle page visitée par
l'opérateur peut faire émettre au navigateur des requêtes vers `http://127.0.0.1:8000`, et avec
`auth.enabled = false` ces requêtes ne portent aucune information d'authentification : l'accès du
navigateur *est* l'autorisation. Deux gardes s'exécutent sur chaque méthode non sûre (`POST`, `PUT`,
`PATCH`, `DELETE`), que l'authentification soit activée ou non.

**Garde Origin.** La requête est refusée avec `403` et le code `cross_origin_forbidden` quand elle
porte `Sec-Fetch-Site: cross-site`, ou un en-tête `Origin` absent de l'ensemble autorisé. Les
requêtes sans en-tête `Origin` — `curl`, SDK, clients CI — ne sont pas des navigations et passent
cette vérification.

Par défaut, l'ensemble autorisé contient les origines loopback sur le port configuré :
`http://127.0.0.1:<api_port>`, `http://localhost:<api_port>` et `http://[::1]:<api_port>`.
Remplacez-le lorsque AstinusLab est servi derrière un proxy inverse ou sous un autre nom d'hôte :

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

```bash
ASTINUSLAB_BROWSER_ALLOWED_ORIGINS=https://astinuslab.example
```

Chaque entrée est un schéma, un hôte et un port exacts. Les chemins, identifiants et jokers sont
rejetés au démarrage : une liste mal formée arrête le service au lieu d'élargir la frontière.
Définir la clé **remplace** les valeurs loopback par défaut ; listez toutes les origines qui servent
l'UI.

**Token CSRF.** Les requêtes non sûres doivent renvoyer le cookie `astinuslab_csrf` dans l'en-tête
`X-CSRF-Token`, ou dans le champ de formulaire `csrf_token` pour les envois
`application/x-www-form-urlencoded` et `multipart/form-data`. Un token absent donne `403` avec le
code `csrf_required` ; un token non concordant donne `403` avec `csrf_invalid`. Le cookie est
`SameSite=Strict`, `Secure` en HTTPS, et volontairement lisible par JavaScript pour que la page
puisse le renvoyer : la garde est le renvoi, pas le secret du cookie. Le token tourne à la connexion
et est effacé à la déconnexion.

Les requêtes portant un token `Authorization: Bearer` valide sautent la vérification CSRF : un
bearer est une autorité explicite que le navigateur n'attache jamais de lui-même. La garde Origin
s'applique toujours.

L'UI fournie ne demande aucun travail supplémentaire :
`src/astinuslab/web/templates/partials/csrf_input.html` fournit le champ caché et `src/astinuslab/web/static/csrf.js`
lit la balise meta `astinuslab-csrf-token` et ajoute l'en-tête aux appels `fetch` et aux requêtes
HTMX. Les clients navigateur personnalisés doivent faire de même. Les clients non-navigateur
devraient s'authentifier avec un bearer ; un client à base de cookies seulement (y compris un
harnais de test sur un client HTTP brut) doit semer lui-même la paire cookie/en-tête.

## Sécurité de l'écoute réseau

AstinusLab refuse de démarrer avec un `api_host` non-loopback sauf si l'une de ces conditions est vraie :

- `auth.enabled = true` et `admin_api_token_hash` est configuré.
- `allow_unsafe_network_bind_without_auth = true` est explicitement défini pour une expérimentation locale contrôlée.

Utilisez loopback (`127.0.0.1`, `::1` ou `localhost`) pour le développement normal.

## Avant d'exposer l'app

L'authentification bearer de l'API protège les endpoints d'API contre les appelants réseau non authentifiés, mais ce n'est pas une frontière de sécurité complète face à internet. Pour les réseaux partagés ou l'usage distant :

- Placez AstinusLab derrière TLS, de préférence via un reverse proxy ou un VPN.
- Restreignez les IP sources avec des règles de firewall.
- Faites tourner le token admin s'il a pu fuiter.
- Préférez les tunnels SSH pour les opérations distantes ponctuelles.
- N'exposez pas les services guest vulnérables directement à des réseaux non fiables.

## Accès aux artefacts

Les endpoints de téléchargement d'artefacts nécessitent le token bearer admin quand l'authentification est activée :

- `GET /api/v1/jobs/{job_id}/artifact`
- `GET /api/v1/jobs/{job_id}/disk-artifact`
- `GET /api/v1/artifacts/{artifact_id}/download`
- `GET /api/v1/artifacts/{artifact_id}/manifest`
- `GET /api/v1/artifacts/{artifact_id}/sha256`

Traitez les tokens bearer et les artefacts capturés comme sensibles.
