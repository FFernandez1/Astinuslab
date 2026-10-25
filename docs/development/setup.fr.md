# Configuration de développement

Utilisez le checkout du dépôt pour le développement. L'installation en mode release est documentée dans [Exécution locale](../usage/running-locally.md).

## Cloner et préparer les catalogues

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

Exécutez `./astinuslab.sh setup` ; il initialise `astinuslab.toml`, `config/astinuslab.env` et les catalogues actifs depuis leurs exemples lorsqu'ils manquent, sans remplacer la configuration existante.

## Environnement Python

```bash
./astinuslab.sh setup
npm ci
npx playwright install chromium
```

Crée `.venv`, installe les dépendances Python et propose de générer une clé SSH ED25519. `npm ci` installe les outils frontend verrouillés et la commande Playwright installe Chromium pour les tests navigateur.

## Démarrer les services

```bash
./astinuslab.sh start
./astinuslab.sh start --docs
./astinuslab.sh status
```

## Organisation du code source

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

## Fichiers générés

No hagas commit de salida runtime de `storage/`, salida MkDocs de `site/`, cachés, imágenes cloud descargadas ni símbolos generados. Consulta [Generated Runtime Files](GENERATED_RUNTIME_FILES.md).


## Variables d'environnement

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

## Installation

Los catálogos activos se inicializan automáticamente: `./astinuslab.sh setup` copia cada `config/catalog/*.example.yaml` a su `*.yaml` cuando falta el archivo vivo (consulta [Configuration](../usage/configuration.md)).

```bash
./astinuslab.sh setup
```

Les helpers privilégiés, sudoers et AppArmor optionnel pour le checkout de développement sont couverts dans la section de développement local.

### Images cloud

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

## Modes d'exécution

AstinusLab soporta modo release (systemd en `/opt/astinuslab`) y modo desarrollo (`./astinuslab.sh` en tu checkout). Comparten código y contrato de helpers privilegiados, pero mantienen árboles, venvs, storage y gestores de procesos separados. No ejecutes ambos runners al mismo tiempo; `./astinuslab.sh status` advierte sobre `mixed/conflict`.

## Développement local

El modo desarrollo ejecuta API y host runner directamente desde el checkout. Detén los servicios systemd de release antes de arrancarlo.

### 1. Clone

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

### 2. Configuration

Préparez les catalogues comme dans Installation. Modifiez `astinuslab.toml` pour la configuration applicative. `config/astinuslab.env` est aussi chargé et exporté par le lanceur : tout nom `ASTINUSLAB_*` correspondant à un champ de `Settings` remplace la valeur TOML pour l'API, le worker et la CLI DB. `ASTINUSLAB_DOCS_PORT` et `ASTINUSLAB_SERVICE_USER` sont des paramètres réservés au lanceur.

### 3. Helpers + règle sudoers

El runner comprueba `sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop`. Si falla, instala helpers y sudoers para este checkout:

```bash
sudo ./astinuslab.sh setup-host
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop && echo OK
```

### 4. Appartenance aux groupes


L'utilisateur doit appartenir aux **deux** groupes `libvirt` et `kvm`. Ajoutez `astinuslab` uniquement si une installation complète a créé ce groupe. Fermez puis rouvrez la session après modification.
```bash
sudo usermod -aG libvirt,kvm "$USER"
# astinuslab: only if the full installer created the group
# sudo usermod -aG astinuslab "$USER"
# log out and back in (or `newgrp <group>`) for new memberships to apply
```

### 5. AppArmor + ACLs pour le `storage/` dev (seulement avec de vraies VMs)

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

`./astinuslab.sh start` ya aplica ACLs de traversal en `./storage`; `setup-host --with-apparmor` añade la regla de AppArmor y cadena de traversal.

### 6. Configurer le venv, smoke et démarrer

```bash
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # manual mode only
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host
./astinuslab.sh smoke
./astinuslab.sh start           # API + runner + analysis worker en arrière-plan
./astinuslab.sh start --docs
```

`setup` initialise `astinuslab.toml`, `config/astinuslab.env` et
`config/catalog/*.yaml` depuis les exemples uniquement lorsqu'ils manquent ;
la configuration existante n'est pas remplacée.

C'est le fichier d'environnement du lanceur : ses valeurs sont exportées avant le
démarrage de l'API et des workers, donc tout nom `ASTINUSLAB_*` correspondant à un
champ de settings écrase aussi la configuration.

Web UI: `http://localhost:8000`; docs con `--docs`: `http://localhost:9000`.

### Commandes de contrôle

```bash
./astinuslab.sh smoke
./astinuslab.sh status
./astinuslab.sh logs            # suivre les logs de l'API, du runner et du worker d'analyse
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

## Linting et formatage

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src tests/python ops/deploy
shellcheck ops/host/*.sh
```

## Build de documentation

```bash
.venv/bin/python -m pip install -e ".[docs]"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml
.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000
```

## Changer de mode

Les deux modes partagent `/usr/local/libexec/astinuslab/` : les helpers incorporent le chemin et l'identité de l'arbre qui les a installés. Réinstallez-les à chaque changement de mode. Les commandes ci-dessous concernent une installation à identité unique. Pour les identités séparées, préservez leur configuration et contrôlez les unités explicites `astinuslab-api`, `astinuslab-runner` et `astinuslab-analysis` ; ne les ramenez pas à votre utilisateur.

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
