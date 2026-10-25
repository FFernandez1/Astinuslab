# Configuració de desenvolupament

Fes servir el checkout del repositori per a desenvolupament. La instal·lació en mode release està documentada a [Execució local](../usage/running-locally.md).

## Clonar i preparar catàlegs

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

Executa `./astinuslab.sh setup`; inicialitza `astinuslab.toml`, `config/astinuslab.env` i els catàlegs actius des dels exemples quan falten, sense sobreescriure la configuració existent.

## Entorn de Python

```bash
./astinuslab.sh setup
npm ci
npx playwright install chromium
```

Crea `.venv`, instal·la les dependències Python i ofereix generar una clau SSH ED25519. `npm ci` instal·la les eines frontend fixades i l'ordre Playwright instal·la Chromium per a les proves de navegador.

## Iniciar serveis

```bash
./astinuslab.sh start
./astinuslab.sh start --docs
./astinuslab.sh status
```

## Estructura del codi

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

## Fitxers generats

No hagas commit de salida runtime de `storage/`, salida MkDocs de `site/`, cachés, imágenes cloud descargadas ni símbolos generados. Consulta [Generated Runtime Files](GENERATED_RUNTIME_FILES.md).


## Variables d'entorn

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

## Instal·lació

Los catálogos activos se inicializan automáticamente: `./astinuslab.sh setup` copia cada `config/catalog/*.example.yaml` a su `*.yaml` cuando falta el archivo vivo (consulta [Configuration](../usage/configuration.md)).

```bash
./astinuslab.sh setup
```

Els helpers privilegiats, sudoers i AppArmor opcional per al checkout de desenvolupament es cobreixen a la secció de desenvolupament local.

### Imatges cloud

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

## Modes d'execució

AstinusLab soporta modo release (systemd en `/opt/astinuslab`) y modo desarrollo (`./astinuslab.sh` en tu checkout). Comparten código y contrato de helpers privilegiados, pero mantienen árboles, venvs, storage y gestores de procesos separados. No ejecutes ambos runners al mismo tiempo; `./astinuslab.sh status` advierte sobre `mixed/conflict`.

## Desenvolupament local

El modo desarrollo ejecuta API y host runner directamente desde el checkout. Detén los servicios systemd de release antes de arrancarlo.

### 1. Clone

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

### 2. Configuració

Prepara els catàlegs com a Installation. Edita `astinuslab.toml` per a configuració d'aplicació. `config/astinuslab.env` també es carrega i s'exporta mitjançant el launcher: qualsevol nom `ASTINUSLAB_*` que coincideixi amb un camp de `Settings` sobreescriu el valor de TOML per a l'API, el worker i la CLI de DB. `ASTINUSLAB_DOCS_PORT` i `ASTINUSLAB_SERVICE_USER` són ajustos exclusius del launcher.

### 3. Helpers + regla sudoers

El runner comprueba `sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop`. Si falla, instala helpers y sudoers para este checkout:

```bash
sudo ./astinuslab.sh setup-host
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop && echo OK
```

### 4. Pertinença a grups


L'usuari necessita **tots dos** grups `libvirt` i `kvm`. Afegeix `astinuslab` només si existeix per una instal·lació completa. Tanca sessió i torna a entrar després de canviar els grups.
```bash
sudo usermod -aG libvirt,kvm "$USER"
# astinuslab: only if the full installer created the group
# sudo usermod -aG astinuslab "$USER"
# log out and back in (or `newgrp <group>`) for new memberships to apply
```

### 5. AppArmor + ACLs per al `storage/` de dev (només en executar VMs reals)

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

`./astinuslab.sh start` ya aplica ACLs de traversal en `./storage`; `setup-host --with-apparmor` añade la regla de AppArmor y cadena de traversal.

### 6. Configurar venv, smoke i arrencada

```bash
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # manual mode only
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host
./astinuslab.sh smoke
./astinuslab.sh start           # API + runner + analysis worker en segon pla
./astinuslab.sh start --docs
```

`setup` inicialitza `astinuslab.toml`, `config/astinuslab.env` i
`config/catalog/*.yaml` des dels exemples només quan falten; no sobreescriu la
configuració existent.

És el fitxer d'entorn del llançador: els seus valors s'exporten abans d'arrencar
l'API i els workers, de manera que qualsevol nom `ASTINUSLAB_*` que coincideixi
amb un camp de settings també sobreescriu la configuració.

Web UI: `http://localhost:8000`; docs con `--docs`: `http://localhost:9000`.

### Ordres de control

```bash
./astinuslab.sh smoke
./astinuslab.sh status
./astinuslab.sh logs            # seguir els logs de l'API, el runner i l'analysis worker
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

## Linting i format

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests/python ops/deploy
shellcheck ops/host/*.sh
```

## Build de documentació

```bash
.venv/bin/python -m pip install -e ".[docs]"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml
.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000
```

## Canviar entre modes

Tots dos modes comparteixen `/usr/local/libexec/astinuslab/`: els helpers incorporen la ruta i la identitat de l'arbre que els ha instal·lat. Cal reinstal·lar-los en canviar de mode. Les ordres següents són per a una instal·lació d'identitat única. Amb identitats separades, conserva la configuració i controla les unitats explícites `astinuslab-api`, `astinuslab-runner` i `astinuslab-analysis`; no les converteixis en serveis del teu usuari.

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
