# Imágenes pre-horneadas y rendimiento

Para Windows, consulta [Imágenes de Windows](WINDOWS_IMAGES.md), la guía autoritativa en inglés. Las recetas Linux de esta página no preparan Windows. Conserva las copias de referencia y recuperación.

Instalar workloads complejos (como un stack LAMP completo) puede tardar varios minutos por job. Para conseguir lanzamientos de lab casi instantáneos, AstinusLab te permite "hornear" un workload directamente en una nueva imagen base.

## 0b. Cargas de Playground en redes aisladas

La red predeterminada de Playground no reenvía tráfico guest a Internet. Los instaladores sin caché que usan repositorios o Git necesitan preparación antes de lanzar en aislamiento. El asistente muestra este requisito sin comprobar la caché ni activar salida.

1. Cierra una sesión Playground ordinaria y confirma su limpieza antes de pasar a Jobs. Activa la clave SSH en **Security** y prepara la imagen base seleccionada.
2. En un host con salida explícita para Jobs (normalmente la red NAT `default` de libvirt), ejecuta un `lab-session` con la imagen/carga deseada. Espera al aprovisionamiento correcto y a `waiting_for_capture`; confirma una entrada de caché coincidente en `<storage_dir>/workload-cache/` antes de cancelar/cerrar el Job y esperar su limpieza.
3. Vuelve a Playground y elige la misma imagen/carga en la red aislada. La clave de caché vincula ID del target, carga, identidad de imagen base y huella del instalador. Una entrada coincidente omite la instalación. Si falta, se ejecuta el instalador en vivo: prepara antes o elige explícitamente una red con salida.

Define `api_curl` desde [Ejemplos API](../usage/examples.md) antes de enviar el Job de preparación:

```bash
api_curl -sS -X POST "$BASE_URL/api/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"ubuntu-24.04","workload_id":"dvwa",
       "scenario_id":"lab-session","execution_mode":"manual_trigger"}'
```


Antes de ejecutar el helper, configura `ASTINUSLAB_DATA_DIR` para la raíz de datos de la instalación y `ASTINUSLAB_TOKEN` para su token bearer de administrador. `BASE_URL` permite indicar la URL de la API local. Sin token, el helper usa cookie y cabecera CSRF para desarrollo sin autenticación. Lee el contrato de conexión, tiempos de espera y recuperación en la guía inglesa (selecciona English en el menú de idioma); los fallos devuelven un estado distinto de cero y conservan el Job para inspección.

## 1. Hornear un workload

Usa el script `ops/host/bake_workload.sh` para automatizar este proceso. Crea un job temporal, instala el workload, apaga la VM y aplana el resultado en una nueva imagen QCOW2 autónoma.

### Uso
```bash
./ops/host/bake_workload.sh <target_id> <workload_id> [output_image_name]
```

**Ejemplo**:
```bash
./ops/host/bake_workload.sh ubuntu-22-04-default wordpress-6.4 ubuntu-22.04-wordpress.qcow2
```

### Siguientes pasos
Cuando el script termina:
1. La imagen se guarda en `images/definitions/` de la raíz de datos configurada. El helper no la inspecciona: abre **Images**, solicita inspección y verifica distro, versión y usuario. Aplanar el disco no prueba que arranque ni que el Workload esté listo.
2.  (Opcional) Añade una entrada de target curada a `config/catalog/targets.yaml`
    si quieres sobreescribir los valores autodescubiertos o fijar ajustes
    específicos del escenario:
    ```yaml
    - id: ubuntu-22.04-wordpress-prebaked
      enabled: true
      distro: { slug: ubuntu, version: "22.04", family: debian }
      image:
        source_type: qcow2
        image_ref: ubuntu-22.04-wordpress.qcow2
        default_username: ubuntu
    ```
3. Actualiza el catálogo y valida la nueva imagen en un lab desechable. El software horneado ya está en la imagen; seleccionar otro Workload puede ejecutar su instalador.

---

## 2. Pre-hornear imágenes de atacante

Las imágenes de atacante (Kali, Parrot) suelen requerir herramientas y configuraciones SSH específicas. Usa `ops/host/prebake_attacker_image.sh` para prepararlas.

### Uso
```bash
./ops/host/prebake_attacker_image.sh <source_image> <output_image> [attacker_user] [ssh_public_key]
```

**Ejemplo**:
```bash
./ops/host/prebake_attacker_image.sh \
  images/definitions/kali-linux-2024.qcow2 \
  images/definitions/kali-prebaked.qcow2 \
  kali \
  storage/ssh_keys/astinuslab-default.pub
```

### ¿Por qué usar esto?
*   **Configuración SSH automática**: Inyecta la clave pública del runner de AstinusLab para que pueda controlar la VM atacante.
*   **Guest Agent**: Instala `qemu-guest-agent` para apagados ordenados.
*   **Velocidad**: No hace falta que cloud-init ejecute tareas pesadas de configuración en cada arranque.

---

## 3. Preparar imágenes target stock

Las VMs **target** de Playground se aprovisionan con un seed cloud-init por VM. Algunas imágenes cloud stock conservan estado cloud-init que impide reaplicar el seed, por lo que nunca quedan accesibles por SSH. A diferencia del prebaking de attacker, la preparación de target no hornea un usuario o clave: el paso clave es `cloud-init clean`, que borra el estado stale para que cloud-init se ejecute en el siguiente arranque.

### Uso
```bash
# dry run first (no root, just prints what would be prepared):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy --resolve-only

# prepare it (in place; network-free):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy

# online host or image missing ssh? also apt-install ssh + guest agent:
ops/host/prepare_target_image.sh debian-12 --with-packages
```

Puedes pasar un id de target de catálogo o un filename bajo `images/definitions/`. La imagen se reescribe atómicamente y su `.meta.json` se marca como `"prepared": true` al completar.

### Requisitos
`libguestfs-tools` (proporciona `virt-customize`). `--with-packages` necesita además apt/red dentro del appliance de build. Si `virt-customize` falla con un error de supermin, instala `libguestfs-tools`, permite leer `/boot/vmlinuz-*` y confirma que `libguestfs-test-tool` sale 0.

## 4. Consejos de rendimiento

*   **Copia con reflink**: AstinusLab usa `cp --reflink=auto` al crear overlays. En sistemas de archivos como **Btrfs** o **XFS**, esto hace que la preparación de imágenes sea casi instantánea y ahorra mucho espacio en disco.
*   **Almacenamiento SSD**: Coloca siempre tus directorios `images/definitions` y `storage/` en un SSD para el mejor rendimiento de las VMs.
