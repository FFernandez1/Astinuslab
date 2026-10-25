# Resolució de problemes

## Errors de permisos i de libvirt

Verifica la pertinença als grups:

```bash
id -nG | tr ' ' '\n' | grep -E 'libvirt|kvm|astinuslab'
```

Si falta `libvirt` o `kvm`:

```bash
sudo usermod -aG libvirt,kvm "$USER"
```

Tanca la sessió i torna a entrar abans de reintentar.

Verifica libvirt:

```bash
virsh -c qemu:///system list
```

## El preflight de sudo sense contrasenya falla

El host runner espera scripts auxiliars instal·lats a `/usr/local/libexec/astinuslab/`.

Instal·la'ls o refresca'ls:

```bash
sudo ./astinuslab.sh setup-host
```

Verifica:

```bash
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop
```

No configuris sudoers per executar els scripts auxiliars directament des del checkout del codi font.

## El mode de desenvolupament es nega a arrencar

`./astinuslab.sh start` es nega a arrencar quan els serveis systemd d'AstinusLab estan actius.

```bash
./astinuslab.sh status
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo ./astinuslab.sh setup-host
./astinuslab.sh start
```

## Actualitzacions de bases de dades legacy pre-Alembic

Si una base de dades antiga no pot migrar, fes backup de `storage/astinuslab.db`, revisa els logs d'arrencada i aplica la ruta de migració documentada abans de tornar a iniciar serveis.

## Els Jobs es queden en cua durant una captura llarga

El runner de captura/cicle de vida és serial per disseny. Una captura llarga pot retardar reconciliació, expiració i altres jobs fins que acabi. Les execucions interactives del workbench fan servir la cua `analysis` separada.

## Capacitat de VM assolida (HTTP 409, codi `vm_capacity`)

`orchestration.max_concurrent_vms` limita quantes VMs poden estar actives alhora. Atura o destrueix VMs de Playground, espera que la reconciliació alliberi files en teardown, o augmenta el límit si l'host té prou RAM.

## Els Jobs es queden a `waiting_for_guest_access`

Causes probables:

- `default_username` a `config/catalog/targets.yaml` no coincideix amb la imatge cloud.
- El guest no ha processat les dades de cloud-init.
- La xarxa ha fallat o DHCP no ha assignat cap adreça.
- Falta la clau SSH o no està activa a la UI.

Comprova:

```bash
./astinuslab.sh logs
```

Per a imatges amb xarxa específica del proveïdor, prova una sobreescriptura al catàleg de targets com ara `inject_network_config: false`.

## Les imatges noves no es reconeixen

Executa:

```bash
./astinuslab.sh scan-images --dry-run
```

Si la imatge encara apareix com a desconeguda:

- Instal·la `libguestfs-tools` perquè `virt-inspector` estigui disponible.
- Fes servir un nom de fitxer que identifiqui distro i versió.
- Afegeix una entrada explícita a `config/catalog/targets.yaml`.
- Revisa el sidecar `<image>.meta.json` generat a `images/definitions/`.

## Errors d'AppArmor o d'accés a l'emmagatzematge

El mode release configura AppArmor i les ACL de traversia per a `/opt/astinuslab/storage`.

El mode de desenvolupament desa els artefactes a `<repo>/storage`. Si els jobs amb VMs reals fallen per accés a l'emmagatzematge, executa:

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

Després reintenta `./astinuslab.sh smoke` i el job.

## La generació d'ISF de Volatility 3 falla

Comprova primer `/api/v1/volatility/readiness` o la pàgina Volatility. Els
codis `*_not_configured`, `*_not_found`, `*_not_executable`,
`volatility_version_unsupported`, `volatility_probe_failed`,
`dwarf2json_hash_not_configured` i `dwarf2json_hash_mismatch` indiquen la
correcció necessària. Un `guest transfer mismatch` exigeix verificar el mateix
hash a host i guest; Upload ISF és l'alternativa manual. No copiïs eines dins
del checkout d'AstinusLab.
La cadena de producció només-ISF és:

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

Si aquesta cadena falla, AstinusLab prova el fallback del guest en viu, que pot fer servir un `vmlinux` coincident amb DWARF, debuginfod o una instal·lació de paquets de depuració permesa explícitament. `dwarf2json` no admet una entrada que només contingui BTF i aquest fallback no la fa servir mai.

Causes habituals:

- Falten `rpm2cpio`, `cpio`, `libguestfs-tools`, `qemu-utils` o `libvirt-clients`.
- No hi ha símbols de depuració que coincideixin amb el kernel exacte del guest.
- Les URLs del mirror prebuilt falten o no són `https://`.
- El target no és una distribució Linux x86_64 suportada.

Consulta [Volatility 3](../user/VOLATILITY3.md) i [Suport de distros](../reference/DISTRO_SUPPORT.md).

### Falta output del plugin

Comprova que existeix un dump de memòria, que hi ha un ISF compatible i que els fitxers sota `storage/dumps/<job_id>/vol3/` no contenen errors de Volatility 3. Regenera l'ISF si va ser construït per a un build ID diferent.

## Problemes d'espai de disc

Els bolcats de memòria poden ser tan grans com la RAM de la VM. Els artefactes de disc poden ocupar diversos GB.

Comprova:

- `storage/dumps/`
- `storage/playground/captures/`
- `/var/lib/libvirt/images/astinuslab/`
- `vol3/symbols/`
- `site/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`

Consulta [Fitxers de runtime generats](../development/GENERATED_RUNTIME_FILES.md) abans d'eliminar rutes generades.

## Logs

El mode de desenvolupament escriu logs a `storage/logs/`:

```bash
./astinuslab.sh logs
```

El mode release fa servir els journals de systemd:

```bash
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f
```

Una instal·lació també desa la seva pròpia còpia rotativa a `storage/logs/`, que és el que llegeix la pàgina **Logs**. Usa `[orchestration] log_to_file = false` per desactivar aquesta còpia i usar només el journal.
