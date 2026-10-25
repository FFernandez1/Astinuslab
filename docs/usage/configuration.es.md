# Configuración

Los paquetes portátiles están desactivados por defecto, son experimentales y no están aceptados para 1.0.0. Actívalos solo en pruebas controladas; consulta los [límites de aceptación](../user/PORTABLE_LABS.md).

AstinusLab tiene tres capas de configuración:

| Capa | Archivos | Propósito |
| --- | --- | --- |
| Ajustes estáticos | `astinuslab.toml`, variables de entorno `ASTINUSLAB_*`, `config/astinuslab.env` | Ajustes de almacenamiento del backend, libvirt, catálogo, Celery, Volatility 3, autenticación y bind de la API. |
| Ajustes en tiempo de ejecución | Tabla `app_settings` de la base de datos de la aplicación (`db_path`) | Retención, límite de espacio libre y ajustes de imágenes editados desde la UI. |
| Catálogos | `config/catalog/*.yaml` | Imágenes target, escenarios, workloads, imágenes de atacante, Network Nodes y perfiles. |

## Precedencia de configuración

Los settings estáticos usan esta cadena: variables de entorno `ASTINUSLAB_*`, luego `astinuslab.toml`, luego defaults del código. `config/astinuslab.env` también se exporta al API server, worker y DB CLI mediante `astinuslab.sh` y systemd, así que sus valores participan en la capa de mayor precedencia.

Los ajustes de la UI se guardan en la tabla `app_settings` de la base de datos configurada. La retención y el límite de espacio libre usan la clave `app_settings`; la administración de imágenes usa sus propias claves. El cargador actual no lee ni importa `<storage_dir>/settings.json`: editarlo o copiarlo no modifica ni respalda la política activa. Conserva una copia verificada de la base de datos, la configuración y las evidencias; cambiar `storage_dir` no mueve `db_path`. Consulta [Migración y recuperación](../reference/ADVANCED_CONFIG.md#migracion-de-esquema-y-reversion).

Las instalaciones split fijan las bases SQLite gestionadas en
`<install_dir>/storage/databases/` mediante `config/astinuslab.env`. Las tablas
describen valores por defecto del código; el mantenimiento debe cargar las
sobreescrituras instaladas. Consulta [las rutas instaladas](../reference/ADVANCED_CONFIG.md#installed-split-database-paths).

## Ajustes de la aplicación

`src/astinuslab/core/config.py` carga `astinuslab.toml` desde la raíz del repositorio o de la instalación. Las rutas relativas se resuelven desde esa raíz. También se pueden sobrescribir valores con variables de entorno `ASTINUSLAB_<SETTINGS_FIELD>`, donde el nombre del campo es el campo plano de `Settings`, por ejemplo `ASTINUSLAB_STORAGE_DIR` o `ASTINUSLAB_CELERY_BROKER_URL`. Las secciones o claves TOML desconocidas fallan al arrancar; mantén valores personalizados en claves documentadas o en overrides `ASTINUSLAB_<SETTINGS_FIELD>`.

El `astinuslab.toml` por defecto contiene estas secciones:

| Sección | Ajustes clave |
| --- | --- |
| `[orchestration]` | `api_host`, `api_port`, `api_log_level`, `log_to_file`, `log_max_bytes`, `log_backup_count`, `log_format`, `libvirt_uri`, `libvirt_network`, `max_concurrent_vms` |
| `[storage]` | `storage_dir`, `db_path`, `images_dir`, `playground_disk_dir`, `playground_capture_dir`, `libvirt_image_base_dir` |
| `[catalogs]` | `targets`, `scenarios`, `workloads`, `attackers`, `network_nodes` |
| `[vol3]` | `enabled`, `symbols_path`, `dwarf2json_path`, `volatility_bin`, límites de proveedores y URLs de mirror |
| `[vol3.provider_timeouts]` | timeouts específicos por proveedor para `prebuilt_isf`, `debuginfod`, `host_fetch` y `live_guest` |
| `[celery]` | `broker_url`, `result_backend`, ajustes de reconciliación |
| `[image_inspection]` | `enabled`, `timeout_seconds`, `virt_inspector_bin` |
| `[auth]` | `enabled`, `admin_api_token_hash`, override de seguridad para bind de red |

`playground_disk_dir` y `playground_capture_dir` tienen por defecto `<repo>/storage/playground/disks` y `<repo>/storage/playground/captures`, aunque no aparezcan las claves TOML. Deben ser diferentes y no deben estar anidados. El modelo de ajustes rechaza rutas anidadas porque la recuperación al arrancar limpia los overlays de disco y nunca debe limpiar los artefactos de captura. `libvirt_image_base_dir` tiene por defecto `/var/lib/libvirt/images/astinuslab` porque libvirt/qemu debe atravesar esa ruta del sistema.

## Entorno del lanzador

Copia el archivo de ejemplo cuando necesites sobreescribir valores locales del lanzador:

```bash
cp config/astinuslab.env.example config/astinuslab.env
```

`astinuslab.sh` lee:

| Variable | Valor por defecto en el script | Propósito |
| --- | --- | --- |
| `ASTINUSLAB_API_HOST` | `127.0.0.1` | Host de bind de la API leído por Settings y usado por `astinuslab.serve`. |
| `ASTINUSLAB_API_PORT` | `8000` | Puerto de la API y la UI leído por Settings y usado por `astinuslab.serve`. |
| `ASTINUSLAB_API_LOG_LEVEL` | `info` | Nivel de log de Uvicorn leído por Settings y usado por `astinuslab.serve`. |
| `ASTINUSLAB_AUTH_ENABLED` | `false` | Habilita auth de un único admin para todas las rutas HTTP excepto `/health`, `/ready`, `/login`, `/logout` y `/static/*`. Requerido antes de binds no-loopback salvo override inseguro. |
| `ASTINUSLAB_ADMIN_API_TOKEN_HASH` | sin definir | Hash SHA-256 del token de API admin, formato `sha256:<64-hex>`. |
| `ASTINUSLAB_ALLOW_UNSAFE_NETWORK_BIND_WITHOUT_AUTH` | `false` | Escape explícito solo para experimentos locales controlados. |
| `ASTINUSLAB_DOCS_PORT` | `9000` | Puerto del servidor de desarrollo de MkDocs al usar `start --docs`. |
| `ASTINUSLAB_LIBVIRT_URI` | `qemu:///system` | URI de libvirt usado por los helpers de limpieza del lanzador. |
| `ASTINUSLAB_LIBVIRT_NETWORK` | `default` | Red de libvirt usada por los helpers de limpieza del lanzador. |
| `ASTINUSLAB_SERVICE_USER` | usuario actual | Usuario de la instancia systemd comprobado por `status` y protegido por `start`. |

La unidad systemd de la API también lee `/opt/astinuslab/config/astinuslab.env` tras la instalación y pasa el host, el puerto y el nivel de log de la API a Uvicorn. Si `ASTINUSLAB_API_HOST` es una dirección LAN, configura primero `ASTINUSLAB_AUTH_ENABLED=true` y `ASTINUSLAB_ADMIN_API_TOKEN_HASH`.

### Autenticación admin bearer-token

Cuando `[auth].enabled` o `ASTINUSLAB_AUTH_ENABLED` es `true`, AstinusLab requiere autenticación admin para cada ruta HTTP excepto la lista pública explícita: `/health`, `/ready`, `/login`, `/logout` y assets estáticos bajo `/static/*`. Los clientes API deben enviar `Authorization: Bearer <admin-token>` para rutas JSON como `/api/v1/*` y `/volatility/api/*`.

El inicio de sesión emite una cookie de sesión opaca e independiente (HttpOnly, SameSite=Strict), con una duración fija de 12 horas y solo su hash almacenado en el servidor. La cookie no contiene el token admin de la API; cerrar sesión revoca la sesión del servidor. Consulta [Autenticación](../api/authentication.md).

## Consola de administración

La consola de administración renderizada por el servidor está disponible en la barra lateral izquierda. En modo release, estas páginas requieren el inicio de sesión de administrador; los checkouts de desarrollo normalmente las sirven sin autenticación en loopback.

| Página | Ruta | Propósito |
| --- | --- | --- |
| Imágenes | `/images` | Listar imágenes guest locales, subir o descargar imágenes, inspeccionar metadatos y eliminar imágenes. |
| Workloads | `/workloads` | Buscar, crear, editar y eliminar plantillas de workload. Los cambios se escriben en el catálogo de workloads activo. |
| Storage | `/storage` | Inspeccionar la capacidad de almacenamiento y los buckets de evidencia. Las pestañas también enlazan a Dumps y Workload Cache. |
| Dumps | `/dumps` | Revisar artefactos de memoria y disco, descargar evidencia, eliminar artefactos seleccionados y limpiar archivos huérfanos. La eliminación es irreversible. |
| Workload Cache | `/workload-cache` | Inspeccionar imágenes snapshot cacheadas de workloads y eliminar entradas individuales, seleccionadas o todas. |
| Logs | `/logs` | Ver logs de aplicación y jobs, filtrar por nivel, buscar en el archivo actual, seguir actualizaciones, copiar el log visible o descargarlo. |
| Security | `/ssh-keys` | Generar, seleccionar y eliminar pares de claves SSH usados por las VMs del lab. También expone la generación de claves API cuando la autenticación admin está activada. |
| Settings | `/settings` | Inspeccionar el estado del servicio, la red y el almacenamiento, y aplicar ajustes operativos. |

Settings incluye el estado y los controles del servicio, información de la red libvirt gestionada y sus leases DHCP, ventanas de retención, vista previa y limpieza inmediata de retención, un umbral mínimo de espacio libre y la limpieza de la caché de símbolos/ISF de Volatility. El guardarraíl de disco bloquea nuevos jobs por debajo del umbral configurado; usa `0` para desactivarlo. Limpiar la caché de símbolos reutilizable no elimina las salidas de análisis locales del job.

Trata `storage/ssh_keys/` como un almacén de credenciales: sus claves privadas sin passphrase permiten acceso automatizado a los guests del lab y no deben copiarse con la evidencia ni colocarse en almacenamiento compartido. La eliminación de evidencia desde Dumps, la limpieza de retención y la limpieza de Workload Cache no se pueden deshacer. Sigue [Integridad forense](../reference/FORENSIC_INTEGRITY.md) al exportar evidencia.

### Sondas operativas

Usa el `GET /health` público para el liveness del proceso sin dependencias y `GET /ready` para el readiness de dependencias. `/ready` devuelve `503` solo cuando la base de datos o el storage escribible no están disponibles; los fallos de workers o libvirt devuelven `200` con `status: degraded`. Ambas respuestas incluyen `X-Request-ID` para correlacionarlas con los logs del backend. Consulta [Endpoints de API](../api/endpoints.md#sondas-operativas).

## Catálogos

La app lee las rutas de catálogo de `[catalogs]` en `astinuslab.toml`.

| Archivo | Define |
| --- | --- |
| `config/catalog/targets.yaml` | Imágenes base de Linux, nombres de usuario, metadatos de distro, recursos de VM, comportamiento de cloud-init y flags de ciclo de vida. |
| `config/catalog/scenarios.yaml` | Modos de ejecución de jobs como `immediate`, `post_script` y `manual_trigger`. |
| `config/catalog/workloads.yaml` | Scripts de instalación de workloads, servicios, etiquetas CVE, health checks y metadatos de compatibilidad. |
| `config/catalog/attackers.yaml` | Imágenes de VM atacante usables con sesiones de lab manual. |
| `config/catalog/profiles.yaml` | Metadatos de perfil usados por la capa de catálogo. |

Se proporcionan catálogos de ejemplo con sufijos `.example.yaml`. La configuración de desarrollo suele copiarlos a archivos YAML activos antes del primer arranque.

## Inspección de imágenes

Cuando `virt-inspector` está disponible, AstinusLab inspecciona las imágenes en `images/definitions/` y escribe sidecars `<image>.meta.json`. Si la inspección está desactivada o no disponible, se usan heurísticas de nombre de archivo.

## Volatility 3

Los ISFs de Linux reutilizables se almacenan en `vol3/symbols/linux/`. La salida de plugins por job se escribe en `storage/dumps/<job_id>/vol3/`. Los mirrors de ISF prebuilt deben usar `https://` en despliegues normales; `file://` está pensado para tests locales y entornos offline controlados.

Volatility 3 Framework 2.28.0 y `dwarf2json` son herramientas instaladas por el operador
fuera del checkout. Configura `volatility_bin`, `dwarf2json_path` y
`dwarf2json_sha256`, reinicia los servicios y comprueba readiness; AstinusLab no
descarga estas herramientas ni acepta sus licencias. Mantén la tabla de
variables generada con `tools/dev/generate_env_reference.py`.

## Raíces de almacenamiento privilegiadas

Los helpers ACL instalados solo aceptan raíces registradas en el archivo
controlado por root `/etc/astinuslab/acl-policy.json`. Registra una raíz
personalizada absoluta, existente y sin symlink:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab --storage-root /srv/astinuslab
```

Configura `[storage] storage_dir` o `ASTINUSLAB_STORAGE_DIR` con la misma ruta antes de registrarla. Solo cambia el destino de futuras salidas de Jobs: no copia evidencias históricas ni actualiza referencias de la base de datos. Las capturas y discos de Playground, y `db_path`, tienen raíces independientes. Conserva las raíces anteriores hasta que una migración planificada verifique archivos, sidecars, referencias y acceso de los servicios. Registrar una raíz no migra los datos ni permite retirarlos; consulta [Política de almacenamiento](../reference/ADVANCED_CONFIG.md#politica-de-almacenamiento-privilegiado).

Usa la identidad de servicio actual y el grupo estable `astinuslab` para la raíz
(modo `2770`). Cada padre debe pertenecer a root y no ser escribible por grupo ni
por otros; usa `root:astinuslab` con modo `0750` cuando el grupo necesite
atravesarlo. Este diseño sigue siendo válido tras migrar a identidades separadas.

Usa `--check` para informar divergencias sin mutar nada; no hay reinicio salvo
que se indique `--restart`. Las unidades systemd se vuelven a renderizar con la
raíz real. Bajo `/home`, cada unidad de servicio usa `ProtectHome=tmpfs` y un
montaje `BindPaths=` para la raíz configurada.

## Lista de orígenes de navegador permitidos

Las mutaciones de navegador (`POST`, `PUT`, `PATCH`, `DELETE`) deben llevar un `Origin` que
AstinusLab reconozca y devolver la cookie `astinuslab_csrf` en la cabecera `X-CSRF-Token` o en el
campo de formulario `csrf_token`. Sin configuración, los orígenes aceptados son los loopback en
`api_port`: `http://127.0.0.1:<puerto>`, `http://localhost:<puerto>` y `http://[::1]:<puerto>`.

Servir la UI tras un proxy inverso u otro nombre de host exige listar ese origen, lo que
**reemplaza** los valores loopback por defecto:

```toml
[auth]
browser_allowed_origins = ["https://astinuslab.example"]
```

Las entradas son esquema, host y puerto exactos; rutas, credenciales y comodines se rechazan al
arrancar. Los clientes de API que no envían cabecera `Origin`, y las peticiones con bearer token
admin, no se ven afectados. Ver
[Autenticación](../api/authentication.md#proteccion-de-mutaciones-del-navegador).

## Referencia de variables de entorno

La referencia generada de variables `ASTINUSLAB_*` vive en la fuente English de esta página. Mantén esa tabla generada desde `tools/dev/generate_env_reference.py` y sincroniza las descripciones localizadas cuando cambie la fuente. Los settings de lista aceptan valores separados por coma o un array JSON; los settings tipo dict aceptan un objeto JSON.
