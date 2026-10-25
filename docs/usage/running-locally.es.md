# Ejecución local

El host probado es Ubuntu 24.04 LTS x86_64 con KVM. Las recetas para otros hosts son experimentales. El espacio de descarga no equivale a la capacidad operativa: el smoke estricto avisa por debajo de 150 GB libres o 16 GB de RAM; cada laboratorio necesita su propio presupuesto.

Esta página es el runbook operativo para instalar y ejecutar AstinusLab desde este repositorio como servicio systemd.

Si planeas modificar código o ejecutar tests, consulta [Configuración de desarrollo](../development/setup.md).

## Prerrequisitos

### Paquetes del sistema

El host debe ejecutar Linux con soporte de virtualización KVM. Instala los paquetes requeridos según tu distribución:

**Debian / Ubuntu:**
```bash
sudo apt update
sudo apt install -y   qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients libvirt-dev   virtinst cloud-image-utils cloud-init pkg-config python3 python3-venv   python3-pip rsync openssh-client sudo

# Optional but recommended for image auto-detection:
sudo apt install -y libguestfs-tools
# Required for ISF symbol extraction from Fedora/RHEL targets:
sudo apt install -y rpm2cpio cpio
```

**Fedora / RHEL:**
```bash
sudo dnf install -y   qemu-kvm qemu-img libvirt libvirt-client libvirt-devel virt-install   cloud-utils pkgconf python3 python3-pip rsync openssh-clients sudo   policycoreutils-python-utils

sudo dnf install -y libguestfs-tools rpm-build cpio
```

**Arch Linux:**
```bash
sudo pacman -S --needed   qemu-desktop libvirt virt-install cloud-guest-utils pkgconf   python python-pip rsync openssh sudo cpio libguestfs
```

### Control de acceso obligatorio (AppArmor / SELinux)

Libvirt usa MAC para confinar VMs. AstinusLab necesita que libvirt pueda leer/escribir en el storage (`/opt/astinuslab/storage` por defecto). En AppArmor, `ops/deploy/install.sh` parchea la plantilla libvirt; en SELinux, etiqueta el storage con `svirt_image_t` y ejecuta `restorecon`.

### Pertenencia a grupos

```bash
# Add your user to the libvirt and kvm groups
sudo usermod -aG libvirt,kvm "$USER"
# Log out and back in for group membership to take effect
```

Verifica KVM:

```bash
virsh -c qemu:///system list
```

### Python

Se requiere Python 3.12 o 3.13 (`python3 --version`). AstinusLab 1.0.0 solo se
prueba en esos dos runtimes. La línea `requires-python = ">=3.12"` de
`pyproject.toml` es el mínimo de empaquetado, no una declaración de que 3.14 o
posterior esté soportado.

### Clave SSH

AstinusLab usa una clave ED25519 para conectar a VMs guest. El instalador puede generarla o puedes usar una existente; en dev debe estar disponible antes de iniciar jobs.

## Instalación

Ejecuta el instalador como root desde un checkout limpio. `SERVICE_USER` es el usuario que poseerá el servicio y recibirá el grant sudoers `NOPASSWD` para helpers libvirt:

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh   --install-dir /opt/astinuslab   --service-user "$USER"
```

Para usar almacenamiento fuera de la instalación predeterminada, registra la
raíz como root:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab --storage-root /srv/astinuslab
```

Configura `[storage] storage_dir` o `ASTINUSLAB_STORAGE_DIR` con la misma ruta antes de registrarla. Solo cambia el destino de futuras salidas de Jobs: no copia evidencias históricas ni actualiza referencias de la base de datos. Las capturas y discos de Playground, y `db_path`, tienen raíces independientes. Conserva las raíces anteriores hasta que una migración planificada verifique archivos, sidecars, referencias y acceso de los servicios. Registrar una raíz no migra los datos ni permite retirarlos; consulta [Política de almacenamiento](../reference/ADVANCED_CONFIG.md#politica-de-almacenamiento-privilegiado).

`--check` informa divergencias sin mutar nada; el reinicio solo ocurre con
`--restart`. Las unidades se renderizan con la raíz configurada. Bajo `/home`,
cada unidad de servicio usa `ProtectHome=tmpfs` y un montaje `BindPaths=` para
la raíz configurada.
Su última línea es `READY`, `READY WITH WARNINGS` o `NOT READY`; un requisito fallido devuelve un código distinto de cero.

Flags útiles:

```bash
# Preflight only; no writes, no root required for read-only checks
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"

# Update an existing release tree and restart units
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --restart

# Install/update without enabling or starting services
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --no-enable --no-start

# Noninteractive SSH-key behavior
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --yes
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --no-ssh-key
```

Después de instalar:

```bash
cd /opt/astinuslab/current
./astinuslab.sh smoke --require-running
./astinuslab.sh guide-volatility  # opcional; guía de solo lectura
```

Web UI: `http://localhost:8000`.

Comandos útiles:

```bash
# Logs
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f

# Lifecycle
sudo systemctl restart astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo systemctl stop    astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo systemctl disable astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # do not start at boot

# Update after a git pull (re-runs rsync, venv, helpers, sudoers, units)
sudo INSTALL_DIR=/opt/astinuslab SERVICE_USER=$USER bash ops/deploy/install.sh

# Remove
sudo bash ops/deploy/uninstall.sh
```

## Imágenes cloud

AstinusLab necesita imágenes cloud estándar para aprovisionar máquinas virtuales. En modo de
publicación, `/opt/astinuslab/current/ops/images/download_images.sh` las guarda en la raíz de datos
estable `/opt/astinuslab/images/definitions`; en desarrollo, el script usa `images/definitions/`
dentro del checkout. Un argumento opcional sustituye el directorio de destino.
Las descargas ocupan aproximadamente 3 GB comprimidos; mantén al menos 10 GB libres.

```bash
# Modo de publicación con una sola identidad
cd /opt/astinuslab/current && sudo -u "$USER" ./ops/images/download_images.sh

# Modo de publicación con --split-users
cd /opt/astinuslab/current && sudo -u astinuslab-runner ./ops/images/download_images.sh

# Checkout de desarrollo
./ops/images/download_images.sh
```

También puedes descargar imágenes manualmente en `images/definitions/`. Consulta [Generated and Runtime Files](../development/GENERATED_RUNTIME_FILES.md) para limpieza y procedencia.

## Docker o Compose

No hay Dockerfile ni Compose en este repositorio. Los workflows soportados son release con `ops/deploy/install.sh` + systemd y desarrollo con `./astinuslab.sh`.

## Errores comunes

| Síntoma | Causa probable | Arreglo |
| --- | --- | --- |
| `failed to connect to the hypervisor` | Falta acceso libvirt | Añade el usuario a `libvirt,kvm` y vuelve a iniciar sesión. |
| Error de sudo sin contraseña | Faltan helpers o sudoers | Ejecuta `sudo ./astinuslab.sh setup-host`. |
| La VM arranca pero SSH no conecta | Usuario o perfil cloud-init incorrecto | Revisa `default_username`, `cloud_init_profile` e `inject_network_config` en `targets.yaml`. |
| Imagen aparece como unknown | Falta `virt-inspector` o falló la heurística | Instala `libguestfs-tools` o añade metadata explícita. |
| `mixed/conflict` en `./astinuslab.sh status` | Modos dev y systemd activos | Detén un modo antes de lanzar jobs. |
| Falta dependencia de docs | `mkdocs` no instalado en `.venv` | Ejecuta `.venv/bin/python -m pip install -e ".[docs]"`. |
| `smoke: config missing/unreadable: config/astinuslab.env` | El archivo sembrado falta o no se puede leer | Vuelve a ejecutar `./astinuslab.sh setup`; no sobrescribe configuración existente. |
El instalador mantiene el código y `.venv` como propiedad del instalador y de solo lectura para los servicios; las escrituras de ejecución se limitan a storage, configuración mutable, definiciones de imágenes y símbolos de Volatility.

Una instalación limpia activa la autenticación admin y muestra el token al crearlo y en el bloque final;
guárdalo porque solo se conserva su hash. Una actualización
conserva el hash existente y se niega a rotarlo en silencio: pasa `--allow-unauthenticated-local`
solo para una instalación explícitamente de desarrollo. La actualización también conserva la raíz de
storage registrada con `configure-privileged-roots.sh`: el instalador la relee de la política en
lugar de volver a `<install-dir>/storage`.

Para dirigir futuras salidas de Jobs a otra raíz, configura `storage_dir` y registra esa raíz con `configure-privileged-roots.sh`. Esto no migra evidencias existentes. No desinstales para cambiar la raíz: la desinstalación elimina la política ACL, los helpers y la regla sudoers.

Volver deliberadamente a una versión anterior requiere un esquema compatible o restaurar una copia verificada anterior a la migración con todos los escritores detenidos. Reinstalar código no revierte el esquema. La recuperación automática de una actualización fallida restaura su propia copia y es una operación distinta. Conserva las evidencias y reconcilia los registros creados después de la copia. Sigue [Migración y recuperación](../reference/ADVANCED_CONFIG.md#migracion-de-esquema-y-reversion).