# Execució local

El host provat és Ubuntu 24.04 LTS x86_64 amb KVM. Les receptes per a altres hosts són experimentals. L’espai de descàrrega no equival a la capacitat operativa: el smoke estricte avisa per sota de 150 GB lliures o 16 GB de RAM; cada laboratori necessita el seu propi pressupost.

Aquesta pàgina és la guia operativa per instal·lar i executar AstinusLab des d'aquest repositori com a servei systemd.

Si planeges modificar codi o executar tests, consulta [Configuració de desenvolupament](../development/setup.md).

## Prerequisits

### Paquets del sistema

La màquina amfitriona ha d'executar Linux amb compatibilitat per a la virtualització de maquinari KVM. Instal·leu els paquets necessaris segons la distribució:

**Per a Debian / Ubuntu:**
```bash
sudo apt update
sudo apt install -y   qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients libvirt-dev   virtinst cloud-image-utils cloud-init pkg-config python3 python3-venv   python3-pip rsync openssh-client sudo

# Optional but recommended for image auto-detection:
sudo apt install -y libguestfs-tools
# Required for ISF symbol extraction from Fedora/RHEL targets:
sudo apt install -y rpm2cpio cpio
```

**Per a Fedora / RHEL:**
```bash
sudo dnf install -y   qemu-kvm qemu-img libvirt libvirt-client libvirt-devel virt-install   cloud-utils pkgconf python3 python3-pip rsync openssh-clients sudo   policycoreutils-python-utils

sudo dnf install -y libguestfs-tools rpm-build cpio
```

**Per a Arch Linux:**
```bash
sudo pacman -S --needed   qemu-desktop libvirt virt-install cloud-guest-utils pkgconf   python python-pip rsync openssh sudo cpio libguestfs
```

### Control d'accés obligatori (AppArmor / SELinux)

Libvirt utilitza mecanismes MAC per confinar les VM. AstinusLab requereix que les VM de libvirt puguin llegir i escriure al directori d'emmagatzematge (`/opt/astinuslab/storage` per defecte). Amb AppArmor, `ops/deploy/install.sh` modifica la plantilla de libvirt; amb SELinux, etiqueta l'emmagatzematge amb `svirt_image_t` i executa `restorecon`.

### Pertinença a grups

```bash
# Add your user to the libvirt and kvm groups
sudo usermod -aG libvirt,kvm "$USER"
# Log out and back in for group membership to take effect
```

Comproveu que KVM es pot utilitzar:

```bash
virsh -c qemu:///system list
```

### Python

Cal Python 3.12 o 3.13 (`python3 --version`). AstinusLab 1.0.0 només es prova en
aquests dos runtimes. La línia `requires-python = ">=3.12"` de `pyproject.toml`
és el mínim d'empaquetatge, no una declaració que 3.14 o posterior estigui
suportat.

### Clau SSH

AstinusLab utilitza un parell de claus ED25519 per connectar-se a les VM convidades. L'script d'instal·lació en pot generar un automàticament, o podeu utilitzar una clau existent. En mode de desenvolupament, la clau ha d'estar disponible abans d'iniciar Jobs (es pot configurar a la secció Seguretat de la interfície).

## Instal·lació

Executeu l'instal·lador com a root des d'una còpia de treball neta. `SERVICE_USER` és l'usuari local que serà propietari del servei i rebrà l'autorització sudoers `NOPASSWD` per a les utilitats de libvirt:

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh   --install-dir /opt/astinuslab   --service-user "$USER"
```

Per utilitzar emmagatzematge fora de la instal·lació predeterminada, registra
l'arrel com a root:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab --storage-root /srv/astinuslab
```

Configura `[storage] storage_dir` o `ASTINUSLAB_STORAGE_DIR` amb la mateixa ruta abans de registrar-la. Només canvia el destí de futures sortides de Jobs: no copia evidències històriques ni actualitza referències de la base de dades. Les captures i els discos de Playground, i `db_path`, tenen arrels independents. Conserva les arrels anteriors fins que una migració planificada verifiqui fitxers, sidecars, referències i accés dels serveis. Registrar una arrel no migra les dades ni permet retirar-les; consulta [Política d'emmagatzematge](../reference/ADVANCED_CONFIG.md#politica-demmagatzematge-privilegiat).

`--check` informa de divergències sense modificar l'amfitrió; el reinici només
es produeix amb `--restart`. Les unitats es renderitzen amb l'arrel configurada.
Sota `/home`, cada unitat de servei usa `ProtectHome=tmpfs` i un muntatge
`BindPaths=` per a l'arrel configurada.
La darrera línia és `READY`, `READY WITH WARNINGS` o `NOT READY`; si falla un requisit retorna un codi diferent de zero.

Opcions útils de l'instal·lador:

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

Després d'instal·lar o actualitzar, podeu comprovar l'estat:

```bash
cd /opt/astinuslab/current
./astinuslab.sh smoke --require-running
./astinuslab.sh guide-volatility  # opcional; guia de només lectura
```

Interfície web: `http://localhost:8000`.

Ordres útils:

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

## Imatges cloud

AstinusLab necessita imatges cloud estàndard per aprovisionar màquines virtuals. En mode de
publicació, `/opt/astinuslab/current/ops/images/download_images.sh` les desa a l'arrel de dades estable
`/opt/astinuslab/images/definitions`; en desenvolupament, l'script usa `images/definitions/` dins
del checkout. Un argument opcional substitueix el directori de destinació.
Les baixades ocupen aproximadament 3 GB comprimits; mantén almenys 10 GB lliures.

```bash
# Mode de publicació amb una sola identitat
cd /opt/astinuslab/current && sudo -u "$USER" ./ops/images/download_images.sh

# Mode de publicació amb --split-users
cd /opt/astinuslab/current && sudo -u astinuslab-runner ./ops/images/download_images.sh

# Checkout de desenvolupament
./ops/images/download_images.sh
```

També podeu baixar manualment imatges a `images/definitions/`. Consulteu [Fitxers generats i d'execució](../development/GENERATED_RUNTIME_FILES.md) per conèixer les regles de neteja i procedència.

## Docker o Compose

No hi ha cap Dockerfile ni cap fitxer Compose en aquest repositori. Els fluxos de treball admesos són el mode de publicació mitjançant `ops/deploy/install.sh` amb systemd i el mode de desenvolupament mitjançant `./astinuslab.sh`.

## Errors comuns

| Símptoma | Causa probable | Solució |
| --- | --- | --- |
| `failed to connect to the hypervisor` | L'usuari no té accés a libvirt | Afegiu l'usuari a `libvirt,kvm` i, després, tanqueu la sessió i torneu a entrar. |
| El Runner finalitza amb un error de sudo sense contrasenya | Falten els scripts auxiliars o la regla sudoers | Executeu `sudo ./astinuslab.sh setup-host`. |
| La VM arrenca però SSH no es connecta mai | Nom d'usuari del catàleg o perfil cloud-init incorrectes | Comproveu `default_username`, `cloud_init_profile` i `inject_network_config` a `targets.yaml`. |
| La imatge apareix com a unknown | Falta `virt-inspector` o ha fallat la detecció pel nom de fitxer | Instal·leu `libguestfs-tools` o afegiu metadades explícites al catàleg. |
| `mixed/conflict` a `./astinuslab.sh status` | Els modes de desenvolupament i systemd estan actius alhora | Atureu un mode abans d'iniciar Jobs. |
| Falta una dependència de la documentació | `mkdocs` no està instal·lat a `.venv` | Executeu `.venv/bin/python -m pip install -e ".[docs]"`. |
| `smoke: config missing/unreadable: config/astinuslab.env` | Falta el fitxer inicialitzat o no es pot llegir | Torna a executar `./astinuslab.sh setup`; no sobreescriu la configuració existent. |
L’instal·lador manté el codi i `.venv` en només lectura per als serveis; les escriptures d’execució es limiten a storage, configuració mutable, definicions d’imatges i símbols de Volatility.

Una instal·lació neta activa l'autenticació admin i mostra el token en crear-lo i al bloc final;
desa'l perquè només se'n conserva el hash. Una actualització
conserva el hash existent i es nega a rotar-lo en silenci: passa `--allow-unauthenticated-local`
només per a una instal·lació explícitament de desenvolupament. L'actualització també conserva
l'arrel de storage registrada amb `configure-privileged-roots.sh`: l'instal·lador la rellegeix de la
política en lloc de tornar a `<install-dir>/storage`.

Per dirigir futures sortides de Jobs a una altra arrel, configura `storage_dir` i registra-la amb `configure-privileged-roots.sh`. Això no migra les evidències existents. No desinstal·lis per canviar l'arrel: la desinstal·lació elimina la política ACL, els helpers i la regla sudoers.

Tornar deliberadament a una versió anterior requereix un esquema compatible o restaurar una còpia verificada anterior a la migració amb tots els escriptors aturats. Reinstal·lar codi no reverteix l'esquema. La recuperació automàtica d'una actualització fallida restaura la seva pròpia còpia i és una operació diferent. Conserva les evidències i reconcilia els registres creats després de la còpia. Segueix [Migració i recuperació](../reference/ADVANCED_CONFIG.md#migracio-desquema-i-reversio).