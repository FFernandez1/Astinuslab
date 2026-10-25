# Configuración de desarrollo

Usa el checkout del repositorio para desarrollo. La instalación en modo release está documentada en [Ejecución local](../usage/running-locally.md).

## Clonar y preparar catálogos

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

Ejecuta `./astinuslab.sh setup`; inicializa `astinuslab.toml`, `config/astinuslab.env` y los catálogos activos desde sus ejemplos cuando faltan, sin sobrescribir la configuración existente.

## Entorno de Python

```bash
./astinuslab.sh setup
npm ci
npx playwright install chromium
```

Crea `.venv`, instala las dependencias Python y ofrece generar una clave SSH ED25519. `npm ci` instala las herramientas frontend fijadas y el comando Playwright instala Chromium para las pruebas de navegador.

## Iniciar servicios

```bash
./astinuslab.sh start
./astinuslab.sh start --docs
./astinuslab.sh status
```

## Estructura del código

| Path | Purpose |
| --- | --- |
| `src/astinuslab/` | FastAPI app, servicios, runner, repositorios, schemas y soporte. |
| `tests/python/` | Suite de tests backend. |
| `src/astinuslab/web/templates/` | Plantillas Jinja2. |
| `src/astinuslab/web/static/` | CSS y JavaScript. |
| `ops/host/` | Helpers de host usados por el runner. |
| `guest/scripts/` | Scripts copiados a VMs guest. |
| `docs/` | Documentación MkDocs. |
| `config/catalog/` | Catálogos declarativos. |

## Archivos generados

No hagas commit de salida runtime de `storage/`, salida MkDocs de `site/`, cachés, imágenes cloud descargadas ni símbolos generados. Consulta [Generated Runtime Files](GENERATED_RUNTIME_FILES.md).


## Variables de entorno

Crea `config/astinuslab.env` solo cuando los defaults deban cambiar:

```bash
cp config/astinuslab.env.example config/astinuslab.env
```

Valores comunes:

```bash
ASTINUSLAB_API_HOST=127.0.0.1
ASTINUSLAB_API_PORT=8000
ASTINUSLAB_API_LOG_LEVEL=info
ASTINUSLAB_LIBVIRT_URI=qemu:///system
ASTINUSLAB_LIBVIRT_NETWORK=default
```

`ASTINUSLAB_DOCS_PORT` y `ASTINUSLAB_SERVICE_USER` también son soportados por `astinuslab.sh`. Los settings backend se cargan desde `astinuslab.toml` y luego se sobreescriben con valores `ASTINUSLAB_*` exportados.

## Instalación

Los catálogos activos se inicializan automáticamente: `./astinuslab.sh setup` copia cada `config/catalog/*.example.yaml` a su `*.yaml` cuando falta el archivo vivo (consulta [Configuration](../usage/configuration.md)).

```bash
./astinuslab.sh setup
```

Los helpers privilegiados, sudoers y AppArmor opcional para el checkout de desarrollo se cubren en la sección de desarrollo local.

### Imágenes cloud

Descarga las imágenes cloud que quieras usar y colócalas en `images/definitions/`:

```bash
mkdir -p images/definitions

# Ubuntu 22.04
wget -P images/definitions   https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img

# Ubuntu 24.04
wget -P images/definitions   https://cloud-images.ubuntu.com/releases/24.04/release/noble-server-cloudimg-amd64.img

# Debian 12
wget -P images/definitions   https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2

# Fedora 40
wget -P images/definitions   https://download.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2
```

Las imágenes VM grandes son assets locales, no archivos fuente normales. Consulta [Generated and Runtime Files](../development/GENERATED_RUNTIME_FILES.md).

## Modos de ejecución

AstinusLab soporta modo release (systemd en `/opt/astinuslab`) y modo desarrollo (`./astinuslab.sh` en tu checkout). Comparten código y contrato de helpers privilegiados, pero mantienen árboles, venvs, storage y gestores de procesos separados. No ejecutes ambos runners al mismo tiempo; `./astinuslab.sh status` advierte sobre `mixed/conflict`.

## Desarrollo local

El modo desarrollo ejecuta API y host runner directamente desde el checkout. Detén los servicios systemd de release antes de arrancarlo.

### 1. Clone

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

### 2. Configuración

Prepara los catálogos como en Instalación. Edita `astinuslab.toml` para configuración de aplicación. `config/astinuslab.env` también se obtiene y exporta mediante el launcher: cualquier nombre `ASTINUSLAB_*` que coincida con un campo de `Settings` sobrescribe el valor de TOML para la API, el worker y la CLI de DB. `ASTINUSLAB_DOCS_PORT` y `ASTINUSLAB_SERVICE_USER` son ajustes exclusivos del launcher.

### 3. Helpers + regla sudoers

El runner comprueba `sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop`. Si falla, instala helpers y sudoers para este checkout:

```bash
sudo ./astinuslab.sh setup-host
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop && echo OK
```

### 4. Pertenencia a grupos


El usuario necesita **ambos** grupos `libvirt` y `kvm`. Añade `astinuslab` solo si existe por una instalación completa. Cierra sesión y vuelve a entrar después de cambiar los grupos.
```bash
sudo usermod -aG libvirt,kvm "$USER"
# astinuslab: only if the full installer created the group
# sudo usermod -aG astinuslab "$USER"
# log out and back in (or `newgrp <group>`) for new memberships to apply
```

### 5. AppArmor + ACLs para `storage/` de dev (solo al ejecutar VMs reales)

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

`./astinuslab.sh start` ya aplica ACLs de traversal en `./storage`; `setup-host --with-apparmor` añade la regla de AppArmor y cadena de traversal.

### 6. Configurar venv, smoke y arranque

```bash
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # manual mode only
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host
./astinuslab.sh smoke
./astinuslab.sh start           # API + runner + analysis worker en segundo plano
./astinuslab.sh start --docs
```

`setup` siembra `astinuslab.toml`, `config/astinuslab.env` y
`config/catalog/*.yaml` desde los ejemplos solo cuando faltan; no sobrescribe
la configuración existente.

Es el archivo de entorno del lanzador: sus valores se exportan antes de arrancar
la API y los workers, de modo que cualquier nombre `ASTINUSLAB_*` que coincida
con un campo de settings también sobrescribe la configuración.

Web UI: `http://localhost:8000`; docs con `--docs`: `http://localhost:9000`.

### Comandos de control

```bash
./astinuslab.sh smoke
./astinuslab.sh status
./astinuslab.sh logs            # seguir logs de API, runner y analysis worker
./astinuslab.sh stop
./astinuslab.sh restart
./astinuslab.sh cleanup
./astinuslab.sh scan-images
./astinuslab.sh scan-images --dry-run
```

## Tests

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
.venv/bin/python -m pytest tests/python/api/test_api.py -q
```

## Linting y formato

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests/python ops/deploy
shellcheck ops/host/*.sh
```

## Build de documentación

```bash
.venv/bin/python -m pip install -e ".[docs]"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml
.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000
```

## Cambiar entre modos

Los dos modos comparten `/usr/local/libexec/astinuslab/`: los helpers incluyen la ruta e identidad del árbol que los instaló. Hay que reinstalarlos al cambiar de modo. Las órdenes siguientes son para una instalación de identidad única. En instalaciones con identidades separadas, conserva las identidades configuradas y controla las unidades explícitas `astinuslab-api`, `astinuslab-runner` y `astinuslab-analysis`; no las conviertas en servicios de tu usuario.

Ambos modos compiten por la misma cola Celery, nombres de dominio libvirt y estado del host runner. Elige uno. Para pasar a dev:

```bash
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo ./astinuslab.sh setup-host
./astinuslab.sh start
./astinuslab.sh status   # expected: Active runner mode: manual
```

Para volver a release:

```bash
./astinuslab.sh stop
sudo /opt/astinuslab/current/ops/host/install_sudoers_astinuslab.sh \
  --install-dir /opt/astinuslab --run-user "$USER" \
  --helper-source-dir /opt/astinuslab/current/ops/host
sudo systemctl start astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
```

Si necesitas ambos instalados, ejecuta solo un runner a la vez y usa puertos, storage y URLs de broker/resultados separados.
