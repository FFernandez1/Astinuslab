# Configuració avançada

Aquesta és la referència detallada dels ajustos del backend. Per a la guia de configuració de cara a l'operador, vegeu [Configuració](../usage/configuration.md).

AstinusLab llegeix `astinuslab.toml` des del checkout del codi font o el directori
d'instal·lació. `src/astinuslab/core/config.py` aplica llavors les sobreescriptures
d'entorn `ASTINUSLAB_<SETTINGS_FIELD>` per als camps del model aplanat `Settings`.
Per exemple, `ASTINUSLAB_STORAGE_DIR` sobreescriu `storage_dir` i
`ASTINUSLAB_CELERY_BROKER_URL` sobreescriu `celery_broker_url`.

---

## 1. Ajustos globals (`astinuslab.toml`)

El fitxer TOML és la font de veritat per als ajustos d'infraestructura de la plataforma.

### [orchestration]
| Clau | Per defecte | Descripció |
| :--- | :--- | :--- |
| `api_host` | `127.0.0.1` | L'adreça a la qual es vincula el servidor de l'API. |
| `api_port` | `8000` | El port per a l'API i la UI. |
| `log_to_file` | `false` | Escriu també els registres del servei sota `storage/logs/`, que llegeix la pàgina de Logs. Les instal·lacions amb identitats separades usen [directoris per rol](#installed-split-service-logs). Les unitats systemd ho activen perquè stdout va al journal; el llançador de desenvolupament ho desactiva perquè ja redirigeix stdout als fitxers plans. |
| `log_max_bytes` | `10485760` | Límit de mida per fitxer de registre abans de rotar. `0` desactiva la rotació. |
| `log_backup_count` | `5` | Quants fitxers rotats es conserven per servei. |
| `log_format` | `text` | Format dels registres: `text` (format llegible que mostra la pàgina de Logs) o `json` (un objecte per registre, amb els camps de correlació anomenats). Un valor no reconegut torna a `text`. |
| `libvirt_uri` | `qemu:///system` | Cadena de connexió per a libvirt. |
| `libvirt_network` | `default` | La xarxa de libvirt que es fa servir per a les VMs guest. |

### [auth]
| Clau | Per defecte | Descripció |
| :--- | :--- | :--- |
| `enabled` | `false` | Requereix un bearer token admin per a `/api/v1/*`, `/api/v1/playground/*` i la documentació OpenAPI. |
| `admin_api_token_hash` | `null` | Hash SHA-256 del token d'API admin. Usa format `sha256:<64-hex>`. Requerit quan `enabled=true`. |
| `allow_unsafe_network_bind_without_auth` | `false` | Escapatòria explícita només per a experiments locals controlats. Quan és `false`, els `api_host` no-loopback requereixen auth. |
| `browser_allowed_origins` | `[]` | Orígens exactes acceptats en mutacions de navegador. Buit significa els orígens loopback a `api_port`. Separats per comes o llista JSON a l'entorn; una entrada amb ruta, credencials o comodí falla la validació en arrencar. |

Les releases instal·lades activen l'autenticació durant la instal·lació neta:
`ops/deploy/install.sh` genera un token, escriu només el seu hash a `astinuslab.toml` i imprimeix el
token una vegada. Tornar a executar l'instal·lador conserva un hash vàlid existent i es nega a
rotar-lo en silenci. Un checkout de desenvolupament manté `enabled = false` i enllaç loopback. Vegeu
[Autenticació](../api/authentication.md).

Genera un hash sense imprimir el token cru:

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

### [storage]
| Clau | Per defecte | Descripció |
| :--- | :--- | :--- |
| `storage_dir` | `storage` | `storage_dir` selecciona el destí de futures evidències de Jobs; canviar-lo o registrar arrels privilegiades no copia bytes històrics ni reescriu referències a la base de dades. `playground_capture_dir`, `playground_disk_dir` i `db_path` són independents. Planifica i valida una migració separada abans de retirar una arrel antiga. |
| `db_path` | `storage/astinuslab.db` | Ruta a la base de dades SQLite. |
| `images_dir` | `images/definitions` | Ruta on s'emmagatzemen les imatges cloud base. |

Les peticions de job que proporcionen un `image_path` explícit només s'accepten quan
la ruta resolta és dins de `images_dir`. El helper d'emmagatzematge de libvirt
aplica la mateixa comprovació de prefix d'origen permès abans de copiar una imatge
base a l'emmagatzematge de libvirt.

### [celery]
| Clau | Descripció |
| :--- | :--- |
| `broker_url` | URL de connexió per al broker de Celery (p. ex., `sqla+sqlite:///...`). |
| `result_backend` | On s'emmagatzemen els resultats de les tasques. |

### [vol3]
| Clau | Per defecte | Descripció |
| :--- | :--- | :--- |
| `enabled` | `true` | Habilita els fluxos d'ISF i anàlisi de Volatility 3. |
| `symbols_path` | `vol3/symbols` | Arrel de la memòria cau global reutilitzable d'ISF. Els símbols de Linux s'emmagatzemen a `linux/`. |
| `dwarf2json_path` | unset | Executable `dwarf2json` instal·lat per l'operador fora del checkout; requereix el pin `dwarf2json_sha256`. |
| `dwarf2json_sha256` | unset | Pin SHA-256 exacte comprovat abans de cada execució i transferència al guest. |
| `volatility_bin` | unset | Executable de Volatility 3 Framework 2.28.0 instal·lat per l'operador fora del checkout. |
| `default_plugins` | Plugins de procés/mòdul de Linux | Llista de plugins separada per comes per a l'anàlisi local al job. |
| `fetch_timeout_seconds` | `1800` | Timeout d'obtenció de paquets de depuració del costat de l'host. Els paquets de depuració grans de les distros poden superar 1 GB. |
| `dwarf2json_timeout_seconds` | `600` | Timeout de build de `dwarf2json` del costat de l'host abans d'intentar el fallback de guest en viu. |
| `debuginfod_urls` | `[]` | Endpoints debuginfod addicionals. Fedora, Arch i openSUSE obtenen valors per defecte de la distro quan no hi ha cap endpoint explícit configurat. |
| `allow_invasive_guest_symbol_install` | `false` | Quan és `true`, els builds de símbols al guest en viu poden instal·lar paquets de depuració dins del guest. El valor per defecte `false` només permet un `vmlinux` coincident amb DWARF o debuginfod i passa `VOL3_ALLOW_GUEST_PACKAGE_INSTALL=0`; `dwarf2json` no admet una entrada que només contingui BTF. |

AstinusLab comprova `<volatility_bin> -q frameworkinfo.FrameworkInfo` abans de l'execució de plugins i registra la primera línia de sortida com a `vol3_version` a `analysis-summary.json`. Mantén aquest binari fixat a nivell de desplegament; els desplegaments provats haurien de registrar el paquet o commit de Volatility 3 instal·lat a les seves notes de versió.

### [image_inspection]
Controla la identificació automàtica de distro de les imatges de disc pujades o
descarregades a `images_dir`. Quan està habilitat, AstinusLab executa
`virt-inspector` (de `libguestfs-tools`) contra cada nova imatge, analitza
`/etc/os-release` i companys, i persisteix el resultat com un sidecar
`<image>.meta.json` al costat de la imatge. El sidecar és la font de veritat per a
l'autodescobriment de catàleg i la UI d'administració d'imatges; les heurístiques de
nom de fitxer es fan servir només com a fallback quan la inspecció no està
disponible.

| Clau | Per defecte | Descripció |
| :--- | :--- | :--- |
| `enabled` | `true` | Posa `false` per ometre la inspecció completament i fer servir sempre heurístiques de nom de fitxer. |
| `timeout_seconds` | `60` | Temps de rellotge màxim per a una única invocació de `virt-inspector`. |
| `virt_inspector_bin` | `virt-inspector` | Sobreescriu si el binari és fora de `$PATH`. |

Si `virt-inspector` falta o falla, el sidecar igualment s'escriu amb
`source: "filename"` i un camp `inspect_error` perquè la UI el mostri; res no es
bloqueja. Els usuaris poden editar camps a través de la pàgina d'administració
d'imatges o disparar una reinspecció — els camps registrats a `user_edited_fields`
sobreviuen a reinspeccions posteriors.

---

## 2. Rutes de catàleg

Les rutes de catàleg es configuren a la secció `[catalogs]` de `astinuslab.toml`.
Fes servir rutes absolutes o rutes relatives al checkout del codi font / directori d'instal·lació.

---

## 3. Ajustos del llançador i del servei

`config/astinuslab.env` el carrega `astinuslab.sh` i la línia d'ordres de la unitat
systemd de l'API després de la instal·lació de release. És principalment un fitxer de
llançador per a valors com l'host i el port de bind de l'API. Com que aquests valors
s'exporten a l'entorn del procés, els noms que també coincideixen amb camps de
`Settings` poden afectar també els ajustos del backend.

### Memòria cau de recursos estàtics (`AL_ASSET_VERSION`)

Les plantilles invaliden la memòria cau dels seus propis enllaços amb
`?v=<versió de recurs>`, resolta des d'`AL_ASSET_VERSION`, si no des del SHA
curt de git i, finalment, `dev`. El muntatge `/static` envia
`Cache-Control: no-cache` per defecte: els navegadors continuen desant a la
memòria cau, però revaliden abans d'usar, de manera que un desplegament es
recull a la càrrega de pàgina següent. Això importa perquè els especificadors
`import` d'ESM dins dels mòduls de la pàgina són deliberadament sense versionar:
les seves URL no canvien entre desplegaments i, sense una política explícita, el
navegador aplicaria frescor heurística i continuaria executant el mòdul antic.

Fixar `AL_ASSET_VERSION` a un valor triat pel desplegament (una etiqueta de
release, un id de build) fa a més que les peticions que porten aquest token
exacte es puguin desar a la memòria cau durant un any com a `immutable`. Un SHA
de git **no** activa això: no es mou mentre s'edita un arbre de treball, de
manera que una còpia de desenvolupament fixaria bytes obsolets sota un token que
després reutilitza. Defineix-lo només on els recursos d'un token donat ja no
tornin a canviar.

## 4. Ajust per a concurrència

Utilitza el llançador o les unitats systemd instal·lades. Mantén el worker de cicle de vida amb `--pool=threads --concurrency=1` i Beat activat; conserva el worker separat `analysis` amb concurrència 1. No augmentis la concurrència del worker per admetre més VMs. Utilitza `[orchestration] max_concurrent_vms` com a control d’admissió; arrencades i captures continuen serialitzades. Diversos workers requereixen redissenyar broker, bloquejos i reconciliació.

## 5. Límit de seguretat

### Postura de desplegament

V1 és localhost/local-de-confiança per defecte. Mantén `api_host` vinculat a `127.0.0.1` per a ús local normal. Si vincules a una adreça LAN, habilita `[auth] enabled = true` amb `admin_api_token_hash`; AstinusLab rebutja binds no-loopback sense autenticació tret que `allow_unsafe_network_bind_without_auth = true` estigui establert explícitament. Fins i tot amb autenticació d'API admin, usa TLS, VPN/firewall i reverse proxy per a xarxes compartides. Els workloads guest són intencionadament vulnerables; prefereix xarxes de Playground aïllades per a labs hostils.

Les receptes actuals de Playground són entrada JSON local de confiança per a configuració de lab repetible. Tracta els fitxers de recepta com a de confiança perquè poden descriure workloads, serveis i comportament del costat del guest. El suport de receptes YAML continua sent una direcció de full de ruta.

### Límit de mutacions del navegador

L'abast per loopback no és autorització: qualsevol pàgina del navegador de l'operador pot enviar un
POST a `http://127.0.0.1:8000`, i amb l'autenticació desactivada aquella petició no porta cap
credencial. Per això cada `POST`, `PUT`, `PATCH` i `DELETE` passa dues guardes, tant si
l'autenticació està activada com si no.

- **Origin.** `Sec-Fetch-Site: cross-site`, o un `Origin` fora de `browser_allowed_origins`, es
  rebutja amb `403` / `cross_origin_forbidden`. Una petició sense capçalera `Origin` no és una
  navegació de navegador i no queda afectada, així que `curl` i els SDK continuen funcionant.
- **CSRF.** La cookie `astinuslab_csrf` s'ha de retornar a la capçalera `X-CSRF-Token` o al camp
  `csrf_token`: `403` / `csrf_required` si falta, `403` / `csrf_invalid` si no coincideix. Les
  peticions amb bearer token admin estan exemptes, perquè un bearer no és autoritat ambiental del
  navegador.

Detall complet del costat client a
[Autenticació](../api/authentication.md#proteccio-de-mutacions-del-navegador).

### Política d'emmagatzematge privilegiat

Els helpers ACL instal·lats s'executen com a root via `sudo -n` i ja no accepten una arrel de
confiança del cridador. Les arrels vénen de `/etc/astinuslab/acl-policy.json`, que ha de ser
propietat de root, no ser un enllaç simbòlic i no ser escrivible pel grup ni per altres, amb esquema
`astinuslab-acl-policy/v1` i les claus `allowed_roots`, `allowed_callers` i `storage_groups`
(opcional). Una invocació amb un objectiu fora de totes les arrels permeses, o amb un cridador no
llistat, falla en lloc d'aplicar una ACL.

Registra una arrel amb l'script instal·lat, mai editant la política a mà:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab \
  --storage-root /srv/astinuslab
```

Configura primer `storage_dir` amb aquesta mateixa ruta. Es mou l'arbre
d'evidència, però no `db_path` ni els altres valors operatius predeterminats sota
`<install-dir>/storage`; per això les unitats conserven escriptura en ambdues arrels.
L'auxiliar d'ACL rebutja discos i captures de Playground fora de qualsevol arrel registrada; configura també `playground_disk_dir` i `playground_capture_dir` dins d'una arrel registrada. Fins que tots dos hi siguin coberts, la comprovació profunda de disponibilitat informa `acl_policy_playground_dir_mismatch` a `root_policy`.
L'arrel ha de pertànyer a la identitat de servei actual i al grup estable
`astinuslab` (mode `2770`). Cada pare ha de ser propietat de root i no pot ser
escrivible pel grup ni per altres; usa `root:astinuslab` amb mode `0750` quan el
grup necessiti travessar-lo. Així la ruta continua disponible després de separar les identitats.

Una arrel registrable és absoluta, existent i un directori sense enllaç simbòlic. Es rebutgen `/` i
els prefixos de sistema (`/etc`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/boot`, `/root`, `/tmp`, `/dev`,
`/proc`, `/sys`, `/run`); `/home` i `/var` es rebutgen com a arrels en si mateixes, mentre que els
subdirectoris dedicats per sota s'accepten. `--check` informa de divergències de política i
d'unitats sense mutar l'amfitrió, i cap servei es reinicia sense `--restart`.

Dins d'una arrel aprovada, el helper recorre la ruta per descriptor de directori
(`O_NOFOLLOW | O_DIRECTORY`), comprova per aquest descriptor el propietari i els bits d'escriptura de
cada component, concedeix només les entrades de traversa i lectura que libvirt necessita, confirma
que cada entrada dins de l'objectiu continua resolent al mateix inode, i després rellegeix l'ACL i
falla si la màscara ha retallat cap entrada: una entrada present però no *efectiva* és un error. L'escriptura de
grup en un component només es tolera per a un grup anomenat a `storage_groups` (la instal·lació posa
el directori de bases de dades en mode `2770` per crear WAL i SHM de SQLite).
En mode split, l'arrel exterior conserva el bit sticky (`3770`) per protegir els
magatzems de paquets; no l'eliminis per reparar permisos SQLite.

`ops/deploy/uninstall.sh` elimina el fitxer de política i tot el conjunt conegut de helpers, i falla
sorollosament si queda un fitxer desconegut a `/usr/local/libexec/astinuslab`.

Arribar a una captura requereix `x` a cada directori des de `/` fins a l'arrel de storage, i
aquest recorregut travessa directoris que el producte no posseeix. L'instal·lador registra cada
entrada que afegeix a `/etc/astinuslab/traversal-acl.manifest` i deixa fora les que ja hi eren;
la desinstal·lació retira només el que aquest manifest indica.

### Disposició de la versió instal·lada

Una versió instal·lada és immutable i s'adreça pel seu contingut. `ops/deploy/install.sh` prepara el
codi font i construeix el seu entorn virtual sota `releases/<versió>-<resum>/` i després mou l'enllaç
`current` amb un canvi de nom, de manera que un servei en execució mai no resol un moment sense
`current`. Reinstal·lar el mateix codi resol a la versió que ja hi és en lloc de preparar-ne una
segona còpia.

```text
/opt/astinuslab/
├── releases/1.0.0-<resum>/    codi i entorn virtual immutables, propietat de root
├── current -> releases/1.0.0-<resum>
├── storage/                   evidència, bases de dades, espai del playground
├── config/                    catàlegs i fitxer d'entorn del llançador
├── images/definitions/        imatges base proporcionades per l'operador
├── vol3/symbols/              memòria cau de símbols de Volatility
└── astinuslab.toml            configuració de l'operador
```

Les rutes mutables es queden a l'arrel d'instal·lació i sobreviuen a cada canvi. Aquesta arrel és
també el que vincula la política privilegiada: `install_sudoers_astinuslab.sh` incrusta el seu
dispositiu i inode a l'ajudant d'emmagatzematge i, com que l'arrel no es mou mai, l'ajudant continua
funcionant després d'una actualització i d'una reversió.

Les unitats systemd s'executen des de `current` i saben on viu l'estat mutable amb
`Environment=ASTINUSLAB_DATA_DIR=/opt/astinuslab`. L'aplicació manté les dues arrels separades:
`base_dir` és l'arrel de codi i `data_dir` l'arrel d'instal·lació.

Actualitzar una instal·lació anterior a aquesta disposició trasllada el seu codi a una versió i
elimina les còpies substituïdes de l'arrel, sense tocar les rutes mutables. Com que `systemctl
start` no fa res en una unitat ja activa, un canvi de versió sempre reinicia les unitats.

### Bases de dades instal·lades amb identitats separades {#installed-split-database-paths}

L'instal·lador split mou les tres bases SQLite gestionades a
`<install_dir>/storage/databases/` i fixa les rutes amb `ASTINUSLAB_DB_PATH`,
`ASTINUSLAB_CELERY_BROKER_URL` i `ASTINUSLAB_CELERY_RESULT_BACKEND` a
`config/astinuslab.env`. Els valors per defecte del codi i les instal·lacions amb
una identitat continuen sota `storage/`. L'arrel d'evidències és independent.
Fes servir l'avaluador de l'entorn instal·lat per al manteniment; una ruta fixa
pot seleccionar una altra base. L'acció d'esborrar l'estat rebutja una base moguda
abans d'esborrar la base o les claus SSH. Atura els serveis per mantenir-la.
L'instal·lador conserva les rutes explícites compatibles i rebutja disposicions
ambigües o no admeses. Conserva els registres de recuperació i les famílies SQLite
sota `backups/`; no moguis fitxers ni creïs enllaços simbòlics manualment.
Resol qualsevol transacció de disposició pendent abans de repetir la instal·lació.

### Logs instal·lats amb identitats separades {#installed-split-service-logs}

Els serveis escriuen a `storage/logs/api/api.log`,
`storage/logs/runner/runner.log` i `storage/logs/analysis/analysis-worker.log`.
Cada worker rota els seus logs; l'API pot llegir-los i truncar-los sense substituir
els fitxers. La pàgina de Logs i `./astinuslab.sh logs` resolen aquestes rutes.
Els checkouts i les instal·lacions amb una sola identitat mantenen fitxers plans.

Els logs plans anteriors continuen disponibles en mode de només lectura com
`legacy-api.log`, `legacy-runner.log` i `legacy-analysis-worker.log`.
Netejar l'emmagatzematge d'execució conserva els directoris i el seu historial.
No esborris el marcador ni canviïs la propietat manualment: si la instal·lació
falla, conserva els nous logs per recuperar-los abans d'eliminar els comptes.

### Migració d'esquema i reversió

Canviar `current` canvia el codi, no l'esquema de la base de dades. Una reversió
requereix un esquema compatible o restaurar la còpia verificada anterior a la migració
amb tots els escriptors aturats. Un enllaç a una versió antiga arbitrària no és una
reversió segura. L'actualització desa primer la còpia i restaura totes dues parts
si falla un pas posterior:

La còpia, migració, restauració i readiness llegeixen `config/astinuslab.env` amb
el mateix avaluador sense shell: entorn del servei, després TOML, després defaults.
S'eliminen els `ASTINUSLAB_*` ambientals de l'instal·lador i es registra
`effective_database=...`. El fitxer no s'executa com a root. No canviïs la configuració
durant l'operació. Una migració fallida avorta abans de publicar la versió, també
amb `--no-start`. Per a la CLI manual, utilitza l'avaluador i l'usuari de servei reals
segons l'exemple de [la guia canònica](ADVANCED_CONFIG.md).


1. **Aturada.** Totes les unitats s'aturen, de manera que l'esquema mai no es migra amb un escriptor
   connectat i res no s'executa mentre `current` es mou.
2. **Còpia de seguretat.** La base de dades es copia amb l'API de còpia del mateix SQLite després
   d'un `wal_checkpoint(TRUNCATE)`; una còpia de fitxer normal pot perdre files confirmades perquè
   la base de dades funciona en mode WAL. La còpia es verifica amb `integrity_check`, es crea amb
   mode `0600`, i la seva ruta, SHA-256 i revisió es registren. L'instal·lador es nega a migrar si
   aquest pas no té èxit.
3. **Migració**, després engegada dels serveis i comprovació de preparació profunda.
4. **Reversió en cas d'error.** La base de dades es restaura *abans* de reiniciar els serveis, i
   `current` torna a la versió que hi havia. Una restauració fallida indica el fitxer de còpia. Després la reversió comprova la versió restaurada igual que una actualització comprova
   la nova: `systemctl` torna quan ha engegat les unitats, abans que la versió restaurada
   serveixi, i un amfitrió que no torni s'informaria com a recuperat.

El canvi és una seqüència ordenada, igual per a cada instal·lació i cada actualització: aturar totes
les unitats, moure `current` amb un canvi de nom, `daemon-reload` i després **reiniciar** cada
unitat. `restart` és l'únic verb: `systemctl start` no fa res en una unitat ja activa, de manera que
una actualització que l'usés deixaria els processos antics lligats a una versió ja substituïda.

`--no-start` atura els serveis, mou `current` i els deixa aturats i habilitats. La comprovació de
preparació profunda s'omet amb un avís que diu que el canvi no està verificat.

Les còpies viuen a `/opt/astinuslab/backups/`, mode `0700` — al costat de l'arbre de storage, mai a
dins: storage és d'on es tallen els paquets d'evidència i conté `storage/ssh_keys/`.

`downgrade` requereix una revisió explícita; els objectius relatius com `-1` i els àlies `head`,
`heads` i `base` es rebutgen, perquè `-1` es resol contra el head del moment.

Revertir l'esquema **no** desfà l'evidència ja escrita: les captures, els manifestos i els fitxers
SHA-256 publicats es queden on són.

### Propietat de l'arbre d'instal·lació

El codi i `.venv` continuen sent propietat de l'instal·lador i no són escrivibles per la identitat
d'execució. `ops/deploy/install.sh` ho reconcilia també en actualitzacions: treu ACLs, neteja els bits
d'escriptura de grup/altres i setgid, i assigna `src`, `ops`, `tools`, `deploy`, `guest`,
`.venv`, `pyproject.toml` i els lockfiles a `root:root` **dins de la versió activa**. Només
`storage`, `config`,
`images/definitions` i `vol3/symbols` són propietat de l'execució.

Les unitats systemd imposen la mateixa forma en execució: `ReadOnlyPaths` cobreix el directori
d'instal·lació i `UMask=0007` evita modes llegibles per tothom en l'evidència nova tot preservant
l'escriptura de grup que necessiten els fitxers SQLite compartits. Cada servei rep només les rutes
mutables que el seu propi codi escriu.

| Unitat | `ReadWritePaths` | Motiu |
|---|---|---|
| `astinuslab-api` | storage, `config`, `images/definitions` | prepara imatges pujades i descarregades, reescriu el catàleg YAML |
| `astinuslab-runner` | storage, `images/definitions`, `vol3/symbols`, `/var/lib/libvirt/images` | crea overlays de VM i construeix símbols ISF; l'ajudant d'emmagatzematge sudo-root s'executa dins l'espai de noms de muntatge d'aquesta unitat |
| `astinuslab-analysis` | storage, `vol3/symbols` | consumeix només la cua d'anàlisi; mai no crea una VM, així que no arriba a cap ruta de virtualització |

Les tres unitats també estableixen `LockPersonality`, `ProtectKernelTunables`,
`ProtectKernelModules`, `ProtectKernelLogs` i `ProtectControlGroups`. `RestrictSUIDSGID`
deliberadament **no** s'estableix: el seu filtre rebutja qualsevol `chmod` amb el bit setgid, i
l'arbre de storage és setgid perquè s'hereti el grup de servei — activar-lo converteix cada captura
de memòria en `Operation not permitted`. `NoNewPrivileges` es queda només a l'API, perquè fa fallar
tota crida `sudo -n`; la identitat d'anàlisi separada sí que el rep perquè no té cap sudo.

Una arrel de storage sota `/home` renderitza totes les unitats amb
`ProtectHome=tmpfs` i un muntatge `BindPaths=` per a aquesta arrel. Això manté
ocults els altres directoris personals i fa que l'storage configurat sigui
accessible i es pugui escriure.

### Política de xarxa en descàrregues d'imatges

Les descàrregues per URL es validen al socket, no només a la URL. AstinusLab resol el nom ell mateix
i, amb la política per defecte `network_policy = "safe"`, rebutja tota resposta que no sigui una
adreça global — loopback, RFC1918, link-local, unique-local i l'adreça de metadades
`169.254.169.254`, incloses les formes IPv4-mapped. Després connecta a aquella IP fixada, comprova
`getpeername()` abans d'enviar la petició i valida el certificat TLS contra el hostname original. Un
rebind de DNS entre validació i connexió no pot canviar el parell.

Els redirects se segueixen manualment: cada salt es revalida amb la mateixa política, amb un màxim
de cinc salts, i una URL repetida es tracta com un bucle i falla. `source_policy` decideix quins
dominis s'accepten — `official_only` (els mirrors de distribució integrats), `allowlist`
(`allowed_domains`, que no pot ser buit) o una política sense restricció — i un domini rebutjat
falla amb `403` abans d'obrir connexió. Es rebutgen les URL amb credencials, esquema diferent de
`http(s)` o port `0`, i `max_size_gb` limita la transferència.

Cada descàrrega es prepara en un directori privat per intent (`images/.staging/<uuid>`, mode `0700`)
i es publica enllaçant el fitxer acabat a `images_dir`: l'enllaç falla si el nom ja existeix i
l'inode publicat es compara amb el preparat. Dues descàrregues concurrents de la mateixa imatge no
poden compartir ni destruir el fitxer parcial de l'altra, i la neteja es fixa a l'inode del directori
de la mateixa operació.
