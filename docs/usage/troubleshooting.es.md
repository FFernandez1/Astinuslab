# Resolución de problemas

## Errores de permisos y libvirt

Verifica la pertenencia a grupos:

```bash
id -nG | tr ' ' '\n' | grep -E 'libvirt|kvm|astinuslab'
```

Si falta `libvirt` o `kvm`:

```bash
sudo usermod -aG libvirt,kvm "$USER"
```

Cierra sesión y vuelve a entrar antes de reintentar.

Verifica libvirt:

```bash
virsh -c qemu:///system list
```

## Falla el preflight de sudo sin contraseña

El host runner espera scripts auxiliares instalados en `/usr/local/libexec/astinuslab/`.

Instálalos o refréscalos:

```bash
sudo ./astinuslab.sh setup-host
```

Verifica:

```bash
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop
```

No configures sudoers para ejecutar los scripts auxiliares directamente desde el checkout del código.

## El modo de desarrollo se niega a arrancar

`./astinuslab.sh start` se niega a arrancar cuando los servicios systemd de AstinusLab están activos.

```bash
./astinuslab.sh status
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo ./astinuslab.sh setup-host
./astinuslab.sh start
```

## Actualizaciones de bases de datos legacy pre-Alembic

Si una base de datos antigua no puede migrar, haz backup de `storage/astinuslab.db`, revisa los logs de arranque y aplica la ruta de migración documentada antes de volver a iniciar servicios.

## Los Jobs se quedan en cola durante una captura larga

El runner de captura/ciclo de vida es serial por diseño. Una captura larga puede retrasar reconciliación, expiración y otros jobs hasta que termine. Las ejecuciones interactivas del workbench usan la cola `analysis` separada.

## Capacidad de VM alcanzada (HTTP 409, código `vm_capacity`)

`orchestration.max_concurrent_vms` limita cuántas VMs pueden estar activas a la vez. Detén o destruye VMs de Playground, espera a que la reconciliación libere filas en teardown, o aumenta el límite si el host tiene RAM suficiente.

## Los Jobs se quedan en `waiting_for_guest_access`

Causas probables:

- `default_username` en `config/catalog/targets.yaml` no coincide con la imagen cloud.
- El guest no procesó los datos de cloud-init.
- Falló la red o DHCP no asignó una dirección.
- Falta la clave SSH o no está activa en la UI.

Comprueba:

```bash
./astinuslab.sh logs
```

Para imágenes con red específica del proveedor, prueba una sobreescritura en el catálogo de targets como `inject_network_config: false`.

## Las nuevas imágenes no se reconocen

Ejecuta:

```bash
./astinuslab.sh scan-images --dry-run
```

Si la imagen sigue apareciendo como desconocida:

- Instala `libguestfs-tools` para disponer de `virt-inspector`.
- Usa un nombre de archivo que identifique distro y versión.
- Añade una entrada explícita a `config/catalog/targets.yaml`.
- Revisa el sidecar `<image>.meta.json` generado en `images/definitions/`.

## Errores de AppArmor o de acceso al almacenamiento

El modo release configura AppArmor y las ACLs de traversía para `/opt/astinuslab/storage`.

El modo de desarrollo guarda los artefactos en `<repo>/storage`. Si los jobs con VMs reales fallan por acceso al almacenamiento, ejecuta:

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

Luego reintenta `./astinuslab.sh smoke` y el job.

## Falla la generación de ISF de Volatility 3

Comprueba primero `/api/v1/volatility/readiness` o la página Volatility. Los
códigos `*_not_configured`, `*_not_found`, `*_not_executable`,
`volatility_version_unsupported`, `volatility_probe_failed`,
`dwarf2json_hash_not_configured` y `dwarf2json_hash_mismatch` indican la
corrección necesaria. Un `guest transfer mismatch` exige verificar el mismo hash
en host y guest; Upload ISF es la alternativa manual. No copies herramientas
en el checkout de AstinusLab.
La cadena de producción solo-ISF es:

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

Si esa cadena falla, AstinusLab prueba el fallback del guest en vivo, que puede usar un `vmlinux` coincidente con DWARF, debuginfod o una instalación de paquetes de depuración permitida explícitamente. `dwarf2json` no admite una entrada que solo contenga BTF y este fallback nunca la utiliza.

Causas comunes:

- Faltan `rpm2cpio`, `cpio`, `libguestfs-tools`, `qemu-utils` o `libvirt-clients`.
- No hay símbolos de depuración que coincidan con el kernel exacto del guest.
- Las URLs del mirror prebuilt faltan o no son `https://`.
- El target no es una distribución Linux x86_64 soportada.

Consulta [Volatility 3](../user/VOLATILITY3.md) y [Soporte de distros](../reference/DISTRO_SUPPORT.md).

### Falta output del plugin

Comprueba que existe un dump de memoria, que hay un ISF compatible y que los archivos bajo `storage/dumps/<job_id>/vol3/` no contienen errores de Volatility 3. Regenera el ISF si fue construido para un build ID distinto.

## Problemas de espacio en disco

Los volcados de memoria pueden ser tan grandes como la RAM de la VM. Los artefactos de disco pueden ocupar varios GB.

Comprueba:

- `storage/dumps/`
- `storage/playground/captures/`
- `/var/lib/libvirt/images/astinuslab/`
- `vol3/symbols/`
- `site/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`

Consulta [Archivos de runtime generados](../development/GENERATED_RUNTIME_FILES.md) antes de eliminar rutas generadas.

## Logs

El modo de desarrollo escribe logs en `storage/logs/`:

```bash
./astinuslab.sh logs
```

El modo release usa los journals de systemd:

```bash
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f
```

Una instalación también guarda su propia copia rotativa en `storage/logs/`, que es lo que lee la página **Logs**. Usa `[orchestration] log_to_file = false` para desactivar esa copia y usar solo el journal.
