# Configuración avanzada

Esta es la referencia detallada de los ajustes del backend. Para la guía de configuración de cara al operador, consulta [Configuración](../usage/configuration.md).

AstinusLab lee `astinuslab.toml` desde el checkout del código fuente o el directorio
de instalación. `src/astinuslab/core/config.py` aplica entonces las sobreescrituras de
entorno `ASTINUSLAB_<SETTINGS_FIELD>` para los campos del modelo aplanado
`Settings`. Por ejemplo, `ASTINUSLAB_STORAGE_DIR` sobreescribe `storage_dir` y
`ASTINUSLAB_CELERY_BROKER_URL` sobreescribe `celery_broker_url`.

---

## 1. Ajustes globales (`astinuslab.toml`)

El archivo TOML es la fuente de verdad para los ajustes de infraestructura de la plataforma.

### [orchestration]
| Clave | Por defecto | Descripción |
| :--- | :--- | :--- |
| `api_host` | `127.0.0.1` | La dirección a la que se vincula el servidor de la API. |
| `api_port` | `8000` | El puerto para la API y la UI. |
| `log_to_file` | `false` | Escribe también los registros del servicio bajo `storage/logs/`, que lee la página de Logs. Las instalaciones con identidades separadas usan [directorios por rol](#installed-split-service-logs). Las unidades systemd lo activan porque stdout va al journal; el lanzador de desarrollo lo desactiva porque ya redirige stdout a los archivos planos. |
| `log_max_bytes` | `10485760` | Límite de tamaño por archivo de registro antes de rotar. `0` desactiva la rotación. |
| `log_backup_count` | `5` | Cuántos archivos rotados se conservan por servicio. |
| `log_format` | `text` | Formato de los registros: `text` (formato legible que muestra la página de Logs) o `json` (un objeto por registro, con los campos de correlación nombrados). Un valor no reconocido vuelve a `text`. |
| `libvirt_uri` | `qemu:///system` | Cadena de conexión para libvirt. |
| `libvirt_network` | `default` | La red de libvirt usada para las VMs guest. |

### [auth]
| Clave | Por defecto | Descripción |
| :--- | :--- | :--- |
| `enabled` | `false` | Requiere un bearer token de admin para `/api/v1/*`, `/api/v1/playground/*` y la documentación OpenAPI. |
| `admin_api_token_hash` | `null` | Hash SHA-256 del token de API admin. Usa formato `sha256:<64-hex>`. Requerido cuando `enabled=true`. |
| `allow_unsafe_network_bind_without_auth` | `false` | Escape explícito solo para experimentos locales controlados. Cuando es `false`, los `api_host` no-loopback requieren auth. |
| `browser_allowed_origins` | `[]` | Orígenes exactos aceptados en mutaciones de navegador. Vacío significa los orígenes loopback en `api_port`. Separados por comas o lista JSON en el entorno; una entrada con ruta, credenciales o comodín falla la validación al arrancar. |

Las releases instaladas activan la autenticación durante la instalación limpia:
`ops/deploy/install.sh` genera un token, escribe solo su hash en `astinuslab.toml` e imprime el token
una vez. Reejecutar el instalador conserva un hash válido existente y se niega a rotarlo en
silencio. Un checkout de desarrollo mantiene `enabled = false` y enlace loopback. Ver
[Autenticación](../api/authentication.md).

Genera un hash sin imprimir el token raw:

```bash
python - <<'PY'
import getpass, hashlib

token = getpass.getpass('Admin API token: ')
print('sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest())
PY
```

### [storage]
| Clave | Por defecto | Descripción |
| :--- | :--- | :--- |
| `storage_dir` | `storage` | `storage_dir` selecciona el destino de futuras evidencias de Jobs; cambiarlo o registrar raíces privilegiadas no copia bytes históricos ni reescribe sus referencias de base de datos. `playground_capture_dir`, `playground_disk_dir` y `db_path` son independientes. Planifica y valida una migración separada antes de retirar una raíz antigua. |
| `db_path` | `storage/astinuslab.db` | Ruta a la base de datos SQLite. |
| `images_dir` | `images/definitions` | Ruta donde se almacenan las imágenes cloud base. |

Las peticiones de job que proporcionan un `image_path` explícito solo se aceptan
cuando la ruta resuelta está dentro de `images_dir`. El helper de almacenamiento de
libvirt aplica la misma comprobación de prefijo de origen permitido antes de copiar
una imagen base al almacenamiento de libvirt.

### [celery]
| Clave | Descripción |
| :--- | :--- |
| `broker_url` | URL de conexión para el broker de Celery (p. ej., `sqla+sqlite:///...`). |
| `result_backend` | Dónde se almacenan los resultados de las tareas. |

### [vol3]
| Clave | Por defecto | Descripción |
| :--- | :--- | :--- |
| `enabled` | `true` | Habilita los flujos de ISF y análisis de Volatility 3. |
| `symbols_path` | `vol3/symbols` | Raíz de la caché global reutilizable de ISF. Los símbolos de Linux se almacenan en `linux/`. |
| `dwarf2json_path` | unset | Ejecutable `dwarf2json` instalado por el operador fuera del checkout; requiere el pin `dwarf2json_sha256`. |
| `dwarf2json_sha256` | unset | Pin SHA-256 exacto comprobado antes de cada ejecución y transferencia al guest. |
| `volatility_bin` | unset | Ejecutable de Volatility 3 Framework 2.28.0 instalado por el operador fuera del checkout. |
| `default_plugins` | Plugins de proceso/módulo de Linux | Lista de plugins separada por comas para el análisis local al job. |
| `fetch_timeout_seconds` | `1800` | Timeout de obtención de paquetes de depuración del lado del host. Los paquetes de depuración grandes de las distros pueden superar 1 GB. |
| `dwarf2json_timeout_seconds` | `600` | Timeout de build de `dwarf2json` del lado del host antes de intentar el fallback de guest en vivo. |
| `debuginfod_urls` | `[]` | Endpoints debuginfod adicionales. Fedora, Arch y openSUSE obtienen valores por defecto de la distro cuando no hay un endpoint explícito configurado. |
| `allow_invasive_guest_symbol_install` | `false` | Cuando es `true`, los builds de símbolos en el guest en vivo pueden instalar paquetes de depuración dentro del guest. El valor por defecto `false` solo permite un `vmlinux` coincidente con DWARF o debuginfod y pasa `VOL3_ALLOW_GUEST_PACKAGE_INSTALL=0`; `dwarf2json` no admite una entrada que solo contenga BTF. |

AstinusLab comprueba `<volatility_bin> -q frameworkinfo.FrameworkInfo` antes de la ejecución de plugins y registra la primera línea de salida como `vol3_version` en `analysis-summary.json`. Mantén este binario fijado a nivel de despliegue; los despliegues probados deberían registrar el paquete o commit de Volatility 3 instalado en sus notas de versión.

### [image_inspection]
Controla la identificación automática de distro de las imágenes de disco subidas o
descargadas en `images_dir`. Cuando está habilitado, AstinusLab ejecuta
`virt-inspector` (de `libguestfs-tools`) contra cada nueva imagen, analiza
`/etc/os-release` y similares, y persiste el resultado como un sidecar
`<image>.meta.json` junto a la imagen. El sidecar es la fuente de verdad para el
autodescubrimiento de catálogo y la UI de administración de imágenes; las
heurísticas de nombre de archivo se usan solo como fallback cuando la inspección no
está disponible.

| Clave | Por defecto | Descripción |
| :--- | :--- | :--- |
| `enabled` | `true` | Pon `false` para omitir la inspección por completo y usar siempre heurísticas de nombre de archivo. |
| `timeout_seconds` | `60` | Tiempo máximo de reloj para una única invocación de `virt-inspector`. |
| `virt_inspector_bin` | `virt-inspector` | Sobreescribe si el binario está fuera de `$PATH`. |

Si `virt-inspector` falta o falla, el sidecar igualmente se escribe con
`source: "filename"` y un campo `inspect_error` para que la UI lo muestre; nada se
bloquea. Los usuarios pueden editar campos a través de la página de administración
de imágenes o disparar una reinspección — los campos registrados en
`user_edited_fields` sobreviven a reinspecciones posteriores.

---

## 2. Rutas de catálogo

Las rutas de catálogo se configuran en la sección `[catalogs]` de `astinuslab.toml`.
Usa rutas absolutas o rutas relativas al checkout del código fuente / directorio de instalación.

---

## 3. Ajustes del lanzador y del servicio

`config/astinuslab.env` lo carga `astinuslab.sh` y la línea de comando de la unidad
systemd de la API tras la instalación de release. Es principalmente un archivo de
lanzador para valores como el host y el puerto de bind de la API. Como esos valores
se exportan al entorno del proceso, los nombres que también coinciden con campos de
`Settings` pueden afectar también a los ajustes del backend.

### Caché de recursos estáticos (`AL_ASSET_VERSION`)

Las plantillas invalidan la caché de sus propios enlaces con `?v=<versión de
recurso>`, resuelta desde `AL_ASSET_VERSION`, si no desde el SHA corto de git y,
en último caso, `dev`. El montaje `/static` envía `Cache-Control: no-cache` por
defecto: los navegadores siguen cacheando, pero revalidan antes de usar, de modo
que un despliegue se recoge en la siguiente carga de página. Esto importa porque
los especificadores `import` de ESM dentro de los módulos de la página están
deliberadamente sin versionar: sus URL no cambian entre despliegues y, sin una
política explícita, el navegador aplicaría frescura heurística y seguiría
ejecutando el módulo antiguo.

Fijar `AL_ASSET_VERSION` a un valor elegido por el despliegue (una etiqueta de
release, un id de build) hace además que las peticiones que llevan ese token
exacto se puedan cachear un año como `immutable`. Un SHA de git **no** activa
esto: no se mueve mientras se edita un árbol de trabajo, así que una copia de
desarrollo fijaría bytes obsoletos bajo un token que luego reutiliza. Defínelo
solo donde los recursos de un token dado ya no vuelvan a cambiar.

## 4. Ajuste para concurrencia

Usa el lanzador o las unidades systemd instaladas. Mantén el worker de ciclo de vida con `--pool=threads --concurrency=1` y Beat activado; conserva el worker separado `analysis` con concurrencia 1. No aumentes la concurrencia del worker para admitir más VMs. Usa `[orchestration] max_concurrent_vms` como control de admisión; arranques y capturas siguen serializados. Varios workers requieren rediseñar broker, bloqueos y reconciliación.

## 5. Límite de seguridad

### Postura de despliegue

V1 es software de localhost/local-confiable por defecto. Mantén `api_host` vinculado a `127.0.0.1` para uso local normal. Si vinculas a una dirección LAN, habilita `[auth] enabled = true` con `admin_api_token_hash`; AstinusLab rechaza binds no-loopback sin autenticación salvo que `allow_unsafe_network_bind_without_auth = true` esté establecido explícitamente. Incluso con autenticación de API admin, usa TLS, VPN/firewall y reverse proxy para redes compartidas. Los workloads guest son intencionadamente vulnerables; prefiere redes de Playground aisladas para labs hostiles.

Las recetas actuales de Playground son entrada JSON local confiable para configuración de lab repetible. Trata los archivos de receta como confiables porque pueden describir workloads, servicios y comportamiento del lado del guest. El soporte de recetas YAML sigue siendo una dirección de roadmap.

### Límite de mutaciones del navegador

La alcanzabilidad por loopback no es autorización: cualquier página del navegador del operador puede
enviar un POST a `http://127.0.0.1:8000`, y con la autenticación desactivada esa petición no lleva
credencial. Por eso cada `POST`, `PUT`, `PATCH` y `DELETE` pasa dos guardas, esté o no activada la
autenticación.

- **Origin.** `Sec-Fetch-Site: cross-site`, o un `Origin` fuera de `browser_allowed_origins`, se
  rechaza con `403` / `cross_origin_forbidden`. Una petición sin cabecera `Origin` no es una
  navegación de navegador y no se ve afectada, así que `curl` y los SDK siguen funcionando.
- **CSRF.** La cookie `astinuslab_csrf` debe devolverse en la cabecera `X-CSRF-Token` o en el campo
  `csrf_token`: `403` / `csrf_required` si falta, `403` / `csrf_invalid` si no coincide. Las
  peticiones con bearer token admin están exentas, porque un bearer no es autoridad ambiental del
  navegador.

Detalle completo del lado cliente en
[Autenticación](../api/authentication.md#proteccion-de-mutaciones-del-navegador).

### Política de almacenamiento privilegiado

Los helpers ACL instalados se ejecutan como root vía `sudo -n` y ya no aceptan una raíz de confianza
del llamador. Las raíces provienen de `/etc/astinuslab/acl-policy.json`, que debe ser propiedad de
root, no ser un symlink y no ser escribible por grupo ni por otros, con esquema
`astinuslab-acl-policy/v1` y las claves `allowed_roots`, `allowed_callers` y `storage_groups`
(opcional). Una invocación cuyo objetivo cae fuera de toda raíz permitida, o cuyo llamador no está
listado, falla en vez de aplicar una ACL.

Registra una raíz con el script instalado, nunca editando la política a mano:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab \
  --storage-root /srv/astinuslab
```

`storage_dir` selecciona el destino de futuras evidencias de Jobs; cambiarlo o registrar raíces privilegiadas no copia bytes históricos ni reescribe sus referencias de base de datos. `playground_capture_dir`, `playground_disk_dir` y `db_path` son independientes. Planifica y valida una migración separada antes de retirar una raíz antigua.
El asistente de ACL rechaza discos y capturas de Playground fuera de toda raíz registrada, así que configura también `playground_disk_dir` y `playground_capture_dir` dentro de una raíz registrada. Hasta que ambos estén cubiertos, la comprobación profunda de disponibilidad informa `acl_policy_playground_dir_mismatch` en `root_policy`.
Haz que la identidad de servicio actual y el grupo estable `astinuslab` sean
propietarios de la raíz (modo `2770`). Cada padre debe pertenecer a root y no ser
escribible por grupo ni por otros; usa `root:astinuslab` con modo `0750` cuando
el grupo necesite atravesarlo. Así la ruta sigue disponible tras migrar a identidades separadas.

Una raíz registrable es absoluta, existente y un directorio sin symlink. Se rechazan `/` y los
prefijos de sistema (`/etc`, `/usr`, `/bin`, `/sbin`, `/lib*`, `/boot`, `/root`, `/tmp`, `/dev`,
`/proc`, `/sys`, `/run`); `/home` y `/var` se rechazan como raíces en sí mismas, mientras que los
subdirectorios dedicados bajo ellos se aceptan. `--check` informa de divergencias de política y de
unidades sin mutar el host, y ningún servicio se reinicia salvo con `--restart`.

Dentro de una raíz aprobada, el helper recorre la ruta por descriptor de directorio
(`O_NOFOLLOW | O_DIRECTORY`), comprueba por ese descriptor el propietario y los bits de escritura de
cada componente, concede solo las entradas de recorrido y lectura que libvirt necesita, confirma que
cada entrada dentro del objetivo sigue resolviendo al mismo inodo, y después relee la ACL y falla si
la máscara recortó alguna entrada: una entrada presente pero no *efectiva* se trata como fallo. La escritura de
grupo en un componente solo se tolera para un grupo nombrado en `storage_groups` (el layout
instalado usa modo `2770` en el directorio de bases de datos para crear WAL y
SHM). En modo split, la raíz exterior conserva el bit sticky (`3770`) para
proteger los almacenes de paquetes; no lo elimines para reparar permisos SQLite.

`ops/deploy/uninstall.sh` elimina el archivo de política y todo el conjunto conocido de helpers, y falla
ruidosamente si queda un archivo desconocido en `/usr/local/libexec/astinuslab`.

Llegar a una captura exige `x` en cada directorio desde `/` hasta la raíz de storage, y ese
recorrido atraviesa directorios que el producto no posee. El instalador registra cada entrada que
añade en `/etc/astinuslab/traversal-acl.manifest` y deja fuera las que ya estaban; la
desinstalación retira solo lo que ese manifiesto nombra.

### Disposición de la versión instalada

Una versión instalada es inmutable y se direcciona por su contenido. `ops/deploy/install.sh` prepara el
código y construye su entorno virtual bajo `releases/<versión>-<resumen>/` y después mueve el enlace
`current` con un renombrado, de modo que un servicio en ejecución nunca resuelve un momento sin
`current`. Reinstalar el mismo código resuelve a la versión que ya está allí en lugar de preparar una
segunda copia.

```text
/opt/astinuslab/
├── releases/1.0.0-<resumen>/  código y entorno virtual inmutables, propiedad de root
├── current -> releases/1.0.0-<resumen>
├── storage/                   evidencia, bases de datos, espacio del playground
├── config/                    catálogos y fichero de entorno del lanzador
├── images/definitions/        imágenes base suministradas por el operador
├── vol3/symbols/              caché de símbolos de Volatility
└── astinuslab.toml            configuración del operador
```

Las rutas mutables permanecen en la raíz de instalación y sobreviven a cada cambio. Esa raíz es
también lo que vincula la política privilegiada: `install_sudoers_astinuslab.sh` incrusta su
dispositivo e inodo en el ayudante de almacenamiento y, como la raíz nunca se mueve, el ayudante
sigue funcionando tras una actualización y tras una reversión.

Las unidades systemd se ejecutan desde `current` y saben dónde vive el estado mutable mediante
`Environment=ASTINUSLAB_DATA_DIR=/opt/astinuslab`. La aplicación mantiene separadas ambas raíces:
`base_dir` es la raíz de código y `data_dir` la raíz de instalación.

Actualizar una instalación anterior a esta disposición traslada su código a una versión y elimina
las copias sustituidas de la raíz, sin tocar las rutas mutables. Como `systemctl start` no hace nada
en una unidad ya activa, un cambio de versión siempre reinicia las unidades.

### Bases de datos instaladas con identidades separadas {#installed-split-database-paths}

El instalador split mueve las tres bases SQLite gestionadas a
`<install_dir>/storage/databases/` y fija sus rutas mediante `ASTINUSLAB_DB_PATH`,
`ASTINUSLAB_CELERY_BROKER_URL` y `ASTINUSLAB_CELERY_RESULT_BACKEND` en
`config/astinuslab.env`. Los valores por defecto del código y las instalaciones
con una identidad siguen bajo `storage/`. La raíz de evidencias es independiente.
Usa el evaluador del entorno instalado para mantenimiento; una ruta fija puede
seleccionar otra base. La acción de borrar el estado rechaza una base trasladada
antes de borrar la base o las claves SSH. Detén los servicios para mantenerla.
El instalador conserva las rutas explícitas compatibles y rechaza disposiciones
ambiguas o no admitidas. Conserva los registros de recuperación y las familias
SQLite bajo `backups/`; no muevas archivos ni crees enlaces simbólicos manualmente.
Resuelve cualquier transacción de disposición pendiente antes de repetir la instalación.

### Logs instalados con identidades separadas {#installed-split-service-logs}

Los servicios escriben en `storage/logs/api/api.log`,
`storage/logs/runner/runner.log` y `storage/logs/analysis/analysis-worker.log`.
Cada worker rota sus propios logs; la API puede leerlos y truncarlos sin sustituir
sus archivos. La página de Logs y `./astinuslab.sh logs` resuelven estas rutas.
Los checkouts y las instalaciones con una sola identidad mantienen archivos planos.

Los logs planos anteriores siguen disponibles en modo de solo lectura como
`legacy-api.log`, `legacy-runner.log` y `legacy-analysis-worker.log`.
Limpiar el almacenamiento de ejecución conserva los directorios y su historial.
No borres el marcador ni cambies la propiedad manualmente: si la instalación
falla, conserva los nuevos logs para recuperación antes de eliminar las cuentas.

### Migración de esquema y reversión

Cambiar `current` cambia el código, no el esquema de la base de datos. Una reversión
requiere un esquema compatible o restaurar su copia verificada previa a la migración
con todos los escritores detenidos. Un enlace a una versión antigua arbitraria no
es una reversión segura. La actualización guarda primero esa copia y restaura ambas
partes si falla un paso posterior:

La copia, migración, restauración y readiness leen `config/astinuslab.env` mediante
el mismo evaluador sin shell: entorno del servicio, luego TOML, luego defaults.
Se eliminan los `ASTINUSLAB_*` ambientales del instalador y se registra
`effective_database=...`. El fichero no se ejecuta como root. No cambies la
configuración durante la operación. Una migración fallida aborta antes de publicar
la versión, también con `--no-start`. Para la CLI manual, usa el evaluador y el
usuario de servicio reales según el ejemplo de [la guía canónica](ADVANCED_CONFIG.md).


1. **Detención.** Todas las unidades se paran, de modo que el esquema nunca se migra con un escritor
   conectado y nada se ejecuta mientras `current` se mueve.
2. **Copia de seguridad.** La base de datos se copia con la API de copia del propio SQLite tras un
   `wal_checkpoint(TRUNCATE)`; una copia de fichero normal puede perder filas confirmadas porque la
   base de datos funciona en modo WAL. La copia se verifica con `integrity_check`, se crea con modo
   `0600`, y su ruta, SHA-256 y revisión quedan registradas. El instalador se niega a migrar si este
   paso no tiene éxito.
3. **Migración**, después arranque de los servicios y comprobación de preparación profunda.
4. **Reversión ante fallo.** La base de datos se restaura *antes* de reiniciar los servicios, y
   `current` vuelve a la versión anterior. Una restauración fallida nombra el fichero de copia. Después la reversión comprueba la versión restaurada igual que una actualización comprueba
   la nueva: `systemctl` vuelve en cuanto ha arrancado las unidades, antes de que la versión
   restaurada sirva, y un host que no vuelva se informaría como recuperado.

El cambio es una secuencia ordenada, igual para cada instalación y cada actualización: parar todas
las unidades, mover `current` con un renombrado, `daemon-reload` y después **reiniciar** cada
unidad. `restart` es el único verbo: `systemctl start` no hace nada en una unidad ya activa, así que
una actualización que lo usara dejaría los procesos antiguos atados a una versión ya sustituida.

`--no-start` para los servicios, mueve `current` y los deja parados y habilitados. La comprobación
de preparación profunda se omite con un aviso que dice que el cambio no está verificado.

Las copias viven en `/opt/astinuslab/backups/`, modo `0700` — junto al árbol de storage, nunca
dentro: storage es de donde se cortan los paquetes de evidencia y contiene `storage/ssh_keys/`.

`downgrade` exige una revisión explícita; los objetivos relativos como `-1` y los alias `head`,
`heads` y `base` se rechazan, porque `-1` se resuelve contra el head del momento.

Revertir el esquema **no** deshace la evidencia ya escrita: las capturas, los manifiestos y los
ficheros SHA-256 publicados permanecen donde están.

### Propiedad del árbol de instalación

El código y `.venv` siguen siendo propiedad del instalador y no son escribibles por la identidad de
ejecución. `ops/deploy/install.sh` reconcilia esto también en actualizaciones: quita ACLs, limpia los
bits de escritura de grupo/otros y setgid, y asigna `src`, `ops`, `tools`, `deploy`,
`guest`, `.venv`, `pyproject.toml` y los lockfiles a `root:root` **dentro de la versión
activa**. Solo `storage`, `config`,
`images/definitions` y `vol3/symbols` son propiedad de la identidad de ejecución.

Las unidades systemd imponen la misma forma en ejecución: `ReadOnlyPaths` cubre el directorio de
instalación y `UMask=0007` evita modos legibles por todos en la evidencia nueva conservando la
escritura de grupo que necesitan los ficheros SQLite compartidos. Cada servicio recibe solo las
rutas mutables que escribe su propio código.

| Unidad | `ReadWritePaths` | Motivo |
|---|---|---|
| `astinuslab-api` | storage, `config`, `images/definitions` | prepara imágenes subidas y descargadas, reescribe el catálogo YAML |
| `astinuslab-runner` | storage, `images/definitions`, `vol3/symbols`, `/var/lib/libvirt/images` | crea overlays de VM y construye símbolos ISF; el ayudante de almacenamiento sudo-root se ejecuta dentro del espacio de nombres de montaje de esta unidad |
| `astinuslab-analysis` | storage, `vol3/symbols` | consume solo la cola de análisis; nunca crea una VM, así que no alcanza ninguna ruta de virtualización |

Las tres unidades también establecen `LockPersonality`, `ProtectKernelTunables`,
`ProtectKernelModules`, `ProtectKernelLogs` y `ProtectControlGroups`. `RestrictSUIDSGID`
deliberadamente **no** se establece: su filtro rechaza cualquier `chmod` cuyo modo lleve setgid, y
el árbol de storage es setgid para que se herede el grupo de servicio — activarlo convierte cada
captura de memoria en `Operation not permitted`. `NoNewPrivileges` permanece solo en la API, porque
hace fallar toda llamada `sudo -n`; la identidad de análisis separada sí lo recibe porque no tiene
ningún sudo.

Una raíz de storage bajo `/home` renderiza todas las unidades con
`ProtectHome=tmpfs` y un montaje `BindPaths=` para esa raíz. Esto mantiene
ocultos los demás directorios personales y hace que el storage configurado sea
accesible y escribible.

### Política de red en descargas de imágenes

Las descargas por URL se validan en el socket, no solo en la URL. AstinusLab resuelve el nombre él
mismo y, con la política por defecto `network_policy = "safe"`, rechaza toda respuesta que no sea
una dirección global — loopback, RFC1918, link-local, unique-local y la dirección de metadatos
`169.254.169.254`, incluidas las formas IPv4-mapped. Después conecta a esa IP fijada, comprueba
`getpeername()` antes de enviar la petición y valida el certificado TLS contra el hostname original.
Un rebind de DNS entre validación y conexión no puede cambiar el par.

Los redirects se siguen manualmente: cada salto se revalida con la misma política, con un máximo de
cinco saltos, y una URL repetida se trata como bucle y falla. `source_policy` decide qué dominios se
aceptan — `official_only` (los mirrors de distribución integrados), `allowlist` (`allowed_domains`,
que no puede estar vacío) o una política sin restricción — y un dominio rechazado falla con `403`
antes de abrir conexión. Se rechazan las URL con credenciales, esquema distinto de `http(s)` o
puerto `0`, y `max_size_gb` acota la transferencia.

Cada descarga se prepara en un directorio privado por intento (`images/.staging/<uuid>`, modo
`0700`) y se publica enlazando el archivo terminado en `images_dir`: el enlace falla si el nombre ya
existe y el inodo publicado se compara con el preparado. Dos descargas concurrentes de la misma
imagen no pueden compartir ni destruir el archivo parcial de la otra, y la limpieza se fija al inodo
del directorio de la propia operación.
