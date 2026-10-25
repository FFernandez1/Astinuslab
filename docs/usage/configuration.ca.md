# Configuració

Els paquets portàtils estan desactivats per defecte, són experimentals i no estan acceptats per a 1.0.0. Activa’ls només en proves controlades; consulta els [límits d’acceptació](../user/PORTABLE_LABS.md).

AstinusLab té tres capes de configuració:

| Capa | Fitxers | Propòsit |
| --- | --- | --- |
| Ajustos estàtics | `astinuslab.toml`, variables d'entorn `ASTINUSLAB_*`, `config/astinuslab.env` | Ajustos d'emmagatzematge del backend, libvirt, catàleg, Celery, Volatility 3, autenticació i bind de l'API. |
| Ajustos en temps d'execució | Taula `app_settings` de la base de dades de l'aplicació (`db_path`) | Retenció, límit d'espai lliure i ajustos d'imatges editats des de la UI. |
| Catàlegs | `config/catalog/*.yaml` | Imatges target, escenaris, workloads, imatges d'atacant, Network Nodes i perfils. |

## Precedència de configuració

Els settings estàtics fan servir aquesta cadena: variables d'entorn `ASTINUSLAB_*`, després `astinuslab.toml`, després defaults del codi. `config/astinuslab.env` també s'exporta a l'API server, worker i DB CLI mitjançant `astinuslab.sh` i systemd, així que els seus valors participen en la capa de més precedència.

Els ajustos de la UI es desen a la taula `app_settings` de la base de dades configurada. La retenció i el límit d'espai lliure fan servir la clau `app_settings`; les imatges tenen les seves pròpies claus. El carregador actual no llegeix ni importa `<storage_dir>/settings.json`: editar-lo o copiar-lo no modifica ni protegeix la política activa. Conserva una còpia verificada de la base de dades, la configuració i les evidències; canviar `storage_dir` no mou `db_path`. Consulta [Migració i recuperació](../reference/ADVANCED_CONFIG.md#migracio-desquema-i-reversio).

Les instal·lacions split fixen les bases SQLite gestionades a
`<install_dir>/storage/databases/` mitjançant `config/astinuslab.env`. Les taules
descriuen valors per defecte del codi; el manteniment ha de carregar les
sobreescriptures instal·lades. Consulta [les rutes instal·lades](../reference/ADVANCED_CONFIG.md#installed-split-database-paths).

## Ajustos de l'aplicació

`src/astinuslab/core/config.py` carrega `astinuslab.toml` des de l'arrel del repositori o de la instal·lació. Les rutes relatives es resolen des d'aquesta arrel. Els valors també es poden sobreescriure amb variables d'entorn `ASTINUSLAB_<SETTINGS_FIELD>`, on el nom del camp és el camp pla de `Settings`, per exemple `ASTINUSLAB_STORAGE_DIR` o `ASTINUSLAB_CELERY_BROKER_URL`. Les seccions o claus TOML desconegudes fan fallar l'arrencada; mantén els valors personalitzats en claus documentades o en overrides `ASTINUSLAB_<SETTINGS_FIELD>`.

El `astinuslab.toml` per defecte conté aquestes seccions:

| Secció | Ajustos clau |
| --- | --- |
| `[orchestration]` | `api_host`, `api_port`, `api_log_level`, `log_to_file`, `log_max_bytes`, `log_backup_count`, `log_format`, `libvirt_uri`, `libvirt_network`, `max_concurrent_vms` |
| `[storage]` | `storage_dir`, `db_path`, `images_dir`, `playground_disk_dir`, `playground_capture_dir`, `libvirt_image_base_dir` |
| `[catalogs]` | `targets`, `scenarios`, `workloads`, `attackers`, `network_nodes` |
| `[vol3]` | `enabled`, `symbols_path`, `dwarf2json_path`, `volatility_bin`, límits de proveïdors i URLs de mirror |
| `[vol3.provider_timeouts]` | timeouts específics per proveïdor per a `prebuilt_isf`, `debuginfod`, `host_fetch` i `live_guest` |
| `[celery]` | `broker_url`, `result_backend`, ajustos de reconciliació |
| `[image_inspection]` | `enabled`, `timeout_seconds`, `virt_inspector_bin` |
| `[auth]` | `enabled`, `admin_api_token_hash`, override de seguretat per a bind de xarxa |

`playground_disk_dir` i `playground_capture_dir` tenen per defecte `<repo>/storage/playground/disks` i `<repo>/storage/playground/captures`, encara que les claus TOML no hi siguin. Han de ser diferents i no han d'estar imbricats. El model d'ajustos rebutja les rutes imbricades perquè la recuperació a l'arrencada neteja els overlays de disc i mai no ha de netejar els artefactes de captura. `libvirt_image_base_dir` té per defecte `/var/lib/libvirt/images/astinuslab` perquè libvirt/qemu ha de travessar aquesta ruta del sistema.

## Entorn del llançador

Copia el fitxer d'exemple quan necessitis sobreescriure valors locals del llançador:

```bash
cp config/astinuslab.env.example config/astinuslab.env
```

`astinuslab.sh` llegeix:

| Variable | Valor per defecte a l'script | Propòsit |
| --- | --- | --- |
| `ASTINUSLAB_API_HOST` | `127.0.0.1` | Host de bind de l'API llegit per Settings i utilitzat per `astinuslab.serve`. |
| `ASTINUSLAB_API_PORT` | `8000` | Port de l'API i la UI llegit per Settings i utilitzat per `astinuslab.serve`. |
| `ASTINUSLAB_API_LOG_LEVEL` | `info` | Nivell de log d'Uvicorn llegit per Settings i utilitzat per `astinuslab.serve`. |
| `ASTINUSLAB_AUTH_ENABLED` | `false` | Activa auth d'un únic admin per a totes les rutes HTTP excepte `/health`, `/ready`, `/login`, `/logout` i `/static/*`. Requerit abans de binds no-loopback tret d'override insegur. |
| `ASTINUSLAB_ADMIN_API_TOKEN_HASH` | sense definir | Hash SHA-256 del token d'API admin, format `sha256:<64-hex>`. |
| `ASTINUSLAB_ALLOW_UNSAFE_NETWORK_BIND_WITHOUT_AUTH` | `false` | Escapatòria explícita només per a experiments locals controlats. |
| `ASTINUSLAB_DOCS_PORT` | `9000` | Port del servidor de desenvolupament de MkDocs en fer servir `start --docs`. |
| `ASTINUSLAB_LIBVIRT_URI` | `qemu:///system` | URI de libvirt que fan servir els helpers de neteja del llançador. |
| `ASTINUSLAB_LIBVIRT_NETWORK` | `default` | Xarxa de libvirt que fan servir els helpers de neteja del llançador. |
| `ASTINUSLAB_SERVICE_USER` | usuari actual | Usuari de la instància systemd comprovat per `status` i protegit per `start`. |

La unitat systemd de l'API també llegeix `/opt/astinuslab/config/astinuslab.env` després de la instal·lació i passa l'host, el port i el nivell de log de l'API a Uvicorn. Si `ASTINUSLAB_API_HOST` és una adreça LAN, configura primer `ASTINUSLAB_AUTH_ENABLED=true` i `ASTINUSLAB_ADMIN_API_TOKEN_HASH`.

### Autenticació admin bearer-token

Quan `[auth].enabled` o `ASTINUSLAB_AUTH_ENABLED` és `true`, AstinusLab requereix autenticació admin per a cada ruta HTTP excepte la llista pública explícita: `/health`, `/ready`, `/login`, `/logout` i assets estàtics sota `/static/*`. Els clients API han d'enviar `Authorization: Bearer <admin-token>` per a rutes JSON com `/api/v1/*` i `/volatility/api/*`.

L’inici de sessió emet una cookie de sessió opaca i independent (HttpOnly, SameSite=Strict), amb una durada fixa de 12 hores i només el hash desat al servidor. La cookie no conté el token admin de l’API; tancar sessió revoca la sessió del servidor. Consulta [Autenticació](../api/authentication.md).

## Consola d'administració

La consola d'administració renderitzada pel servidor està disponible a la barra lateral esquerra. En mode release, aquestes pàgines requereixen l'inici de sessió d'administrador; els checkouts de desenvolupament normalment les serveixen sense autenticació en loopback.

| Pàgina | Ruta | Propòsit |
| --- | --- | --- |
| Imatges | `/images` | Llistar imatges guest locals, pujar o obtenir imatges, inspeccionar-ne les metadades i eliminar-les. |
| Workloads | `/workloads` | Cercar, crear, editar i eliminar plantilles de workload. Els canvis s'escriuen al catàleg de workloads actiu. |
| Storage | `/storage` | Inspeccionar la capacitat d'emmagatzematge i els buckets d'evidència. Les pestanyes també enllacen a Dumps i Workload Cache. |
| Dumps | `/dumps` | Revisar artefactes de memòria i disc, descarregar evidència, eliminar artefactes seleccionats i netejar fitxers orfes. L'eliminació és irreversible. |
| Workload Cache | `/workload-cache` | Inspeccionar imatges snapshot de workloads en memòria cau i eliminar entrades individuals, seleccionades o totes. |
| Logs | `/logs` | Veure logs de l'aplicació i dels jobs, filtrar per nivell, cercar al fitxer actual, seguir actualitzacions, copiar el log visible o descarregar-lo. |
| Security | `/ssh-keys` | Generar, seleccionar i eliminar parells de claus SSH utilitzats per les VMs del lab. La pàgina també exposa la generació de claus API quan l'autenticació admin està activada. |
| Settings | `/settings` | Inspeccionar l'estat del servei, la xarxa i l'emmagatzematge, i aplicar ajustos operatius. |

Settings inclou l'estat i els controls del servei, informació de la xarxa libvirt gestionada i els seus leases DHCP, finestres de retenció, vista prèvia i neteja immediata de retenció, un llindar mínim d'espai lliure i la neteja de la memòria cau de símbols/ISF de Volatility. El guardarraïl de disc bloqueja jobs nous per sota del llindar configurat; fes servir `0` per desactivar-lo. Netejar la memòria cau de símbols reutilitzable no elimina les sortides d'anàlisi locals del job.

Tracta `storage/ssh_keys/` com un magatzem de credencials: les seves claus privades sense passphrase permeten l'accés automatitzat als guests del lab i no s'han de copiar amb l'evidència ni posar en emmagatzematge compartit. L'eliminació d'evidència des de Dumps, la neteja de retenció i la neteja de Workload Cache no es poden desfer. Segueix [Integritat forense](../reference/FORENSIC_INTEGRITY.md) en exportar evidència.

### Sondes operatives

Fes servir el `GET /health` públic per al liveness del procés sense dependències i `GET /ready` per al readiness de dependències. `/ready` retorna `503` només quan la base de dades o l'emmagatzematge amb escriptura no estan disponibles; els errors dels workers o de libvirt retornen `200` amb `status: degraded`. Totes dues respostes inclouen `X-Request-ID` per correlacionar-les amb els logs del backend. Vegeu [Endpoints de l'API](../api/endpoints.md#sondes-operatives).

## Catàlegs

L'app llegeix les rutes de catàleg de `[catalogs]` a `astinuslab.toml`.

| Fitxer | Defineix |
| --- | --- |
| `config/catalog/targets.yaml` | Imatges base de Linux, noms d'usuari, metadades de distro, recursos de VM, comportament de cloud-init i flags de cicle de vida. |
| `config/catalog/scenarios.yaml` | Modes d'execució de jobs com ara `immediate`, `post_script` i `manual_trigger`. |
| `config/catalog/workloads.yaml` | Scripts d'instal·lació de workloads, serveis, etiquetes CVE, health checks i metadades de compatibilitat. |
| `config/catalog/attackers.yaml` | Imatges de VM atacant utilitzables amb sessions de lab manual. |
| `config/catalog/profiles.yaml` | Metadades de perfil que fa servir la capa de catàleg. |

Es proporcionen catàlegs d'exemple amb sufixos `.example.yaml`. La configuració de desenvolupament normalment els copia a fitxers YAML actius abans de la primera arrencada.

## Inspecció d'imatges

Quan `virt-inspector` està disponible, AstinusLab inspecciona les imatges a `images/definitions/` i escriu sidecars `<image>.meta.json`. Si la inspecció està desactivada o no disponible, es fan servir heurístiques de nom de fitxer.

## Volatility 3

Els ISFs de Linux reutilitzables s'emmagatzemen a `vol3/symbols/linux/`. La sortida de plugins per job s'escriu a `storage/dumps/<job_id>/vol3/`. Els mirrors d'ISF prebuilt han de fer servir `https://` en desplegaments normals; `file://` està pensat per a tests locals i entorns offline controlats.

Volatility 3 Framework 2.28.0 i `dwarf2json` són eines instal·lades per l'operador
fora del checkout. Configura `volatility_bin`, `dwarf2json_path` i
`dwarf2json_sha256`, reinicia els serveis i comprova readiness; AstinusLab no
descarrega aquestes eines ni n'accepta les llicències. Mantén la taula de
variables generada amb `tools/dev/generate_env_reference.py`.

## Arrels d'emmagatzematge privilegiades

Els helpers ACL instal·lats només accepten arrels registrades a
`/etc/astinuslab/acl-policy.json`, controlat per root. Registra una arrel
absoluta, existent i sense enllaç simbòlic:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab --storage-root /srv/astinuslab
```

Configura `[storage] storage_dir` o `ASTINUSLAB_STORAGE_DIR` amb la mateixa ruta abans de registrar-la. Només canvia el destí de futures sortides de Jobs: no copia evidències històriques ni actualitza referències de la base de dades. Les captures i els discos de Playground, i `db_path`, tenen arrels independents. Conserva les arrels anteriors fins que una migració planificada verifiqui fitxers, sidecars, referències i accés dels serveis. Registrar una arrel no migra les dades ni permet retirar-les; consulta [Política d'emmagatzematge](../reference/ADVANCED_CONFIG.md#politica-demmagatzematge-privilegiat).

Usa la identitat de servei actual i el grup estable `astinuslab` per a l'arrel
(mode `2770`). Cada pare ha de ser propietat de root i no pot ser escrivible pel
grup ni per altres; usa `root:astinuslab` amb mode `0750` quan el grup necessiti
travessar-lo. Aquesta disposició continua sent vàlida després de separar les identitats.

`--check` informa de divergències sense modificar l'host; no hi ha reinici sense
`--restart`. Les unitats systemd es tornen a renderitzar amb l'arrel real. Sota
`/home`, cada unitat de servei usa `ProtectHome=tmpfs` i un muntatge
`BindPaths=` per a l'arrel configurada.

## Llista d'orígens de navegador permesos

Les mutacions de navegador (`POST`, `PUT`, `PATCH`, `DELETE`) han de portar un `Origin` que
AstinusLab reconegui i retornar la cookie `astinuslab_csrf` a la capçalera `X-CSRF-Token` o al camp
de formulari `csrf_token`. Sense configuració, els orígens acceptats són els loopback a `api_port`:
`http://127.0.0.1:<port>`, `http://localhost:<port>` i `http://[::1]:<port>`.

Servir la UI darrere d'un proxy invers o amb un altre nom de host exigeix llistar aquell origen, la
qual cosa **reemplaça** els valors loopback per defecte:

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

Les entrades són esquema, host i port exactes; rutes, credencials i comodins es rebutgen en
arrencar. Els clients d'API que no envien capçalera `Origin`, i les peticions amb bearer token
admin, no queden afectats. Vegeu
[Autenticació](../api/authentication.md#proteccio-de-mutacions-del-navegador).

## Referència de variables d'entorn

La referència generada de variables `ASTINUSLAB_*` viu a la font English d'aquesta pàgina. Mantén aquesta taula generada des de `tools/dev/generate_env_reference.py` i sincronitza les descripcions localitzades quan canviï la font. Els settings de llista accepten valors separats per coma o un array JSON; els settings tipus dict accepten un objecte JSON.
