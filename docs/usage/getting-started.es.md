# Primeros pasos

AstinusLab aprovisiona VMs Linux sobre KVM/libvirt, ejecuta la configuración del escenario o workload, captura artefactos de memoria y disco desde el host, registra checksums y metadatos, y luego desmonta los recursos del lab.


El host probado es Ubuntu 24.04 LTS x86_64 con KVM. Las recetas para otros hosts son experimentales. El espacio de descarga no equivale a la capacidad operativa: el smoke estricto avisa por debajo de 150 GB libres o 16 GB de RAM; cada laboratorio necesita su propio presupuesto.

## Elige un modo de ejecución

Usa un modo a la vez:

| Modo | Úsalo cuando | Punto de entrada |
| --- | --- | --- |
| Desarrollo | Editas código, pruebas en local o iteras sobre la documentación | `./astinuslab.sh` |
| Release | Ejecutas AstinusLab como un servicio local de larga duración | `ops/deploy/install.sh` más unidades systemd |

No ejecutes el runner de desarrollo y el runner de systemd a la vez. Comparten Celery, libvirt y los recursos de nombrado de VMs.

Las instalaciones release activan la autenticación admin por defecto. Una instalación limpia muestra el token al crearlo y una vez más en el bloque final; guárdalo de inmediato. Las actualizaciones conservan el hash existente. Para una instalación sin autenticación, explícitamente solo de desarrollo, pasa `--allow-unauthenticated-local` a `ops/deploy/install.sh`. Consulta [Autenticación](../api/authentication.md) para las reglas de hash y flags.

## Primera instalación release

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER"
cd /opt/astinuslab/current
sudo -u "$USER" ./ops/images/download_images.sh
./astinuslab.sh smoke --require-running
./astinuslab.sh guide-volatility  # opcional; solo imprime una guía de solo lectura
```

El preflight es de solo lectura y devuelve un código distinto de cero si falta un requisito. Guarda el token de `CREDENTIALS — SAVE NOW`, reserva al menos 10 GB libres para los aproximadamente 3 GB de imágenes y abre la UI solo cuando el smoke estricto pase.

## Primer arranque local

```bash
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host --with-mac
./ops/images/download_images.sh
./astinuslab.sh start
./astinuslab.sh smoke --require-running
```

`./astinuslab.sh setup` inicializa cada catálogo activo en `config/catalog/` desde su
archivo `*.example.yaml` correspondiente cuando falta el archivo activo; no hace
falta copiar archivos manualmente (consulta [Configuración](configuration.md)).

Abre `http://localhost:8000`.

Si también quieres el servidor de documentación:

```bash
./astinuslab.sh start --docs
```

El servidor de docs usa `http://localhost:9000` por defecto.

## Añadir una imagen cloud

Coloca imágenes cloud de Linux en `images/definitions/`. Las entradas de catálogo existentes referencian nombres de archivo como:

| Target ID | Nombre de imagen esperado |
| --- | --- |
| `ubuntu-22-04-default` | `ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img` |
| `ubuntu-24.04` | `noble-server-cloudimg-amd64.img` |
| `debian-12` | `debian-12-genericcloud-amd64.qcow2` |
| `fedora-40` | `Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2` |

Para escanear imágenes recién añadidas:

```bash
./astinuslab.sh scan-images --dry-run
./astinuslab.sh scan-images
./astinuslab.sh restart
```

## Primera captura en Playground

1. Abre `http://localhost:8000` e inicia sesión si hay autenticación. La página inicial es **Playground**. **Ayuda** redirige al servidor de documentación separado, normalmente `http://localhost:9000`, en el idioma elegido. En desarrollo, usa `./astinuslab.sh start --docs`. Con una API ya activa, sirve las guías desde un checkout con dependencias de documentación: `.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000`. Si Ayuda no conecta, inicia ese servidor o consulta los archivos fuente. El enlace usa el `localhost` del navegador; un navegador remoto necesita un servidor local o reenvío configurado.
2. Pulsa **+ Target**, selecciona una imagen disponible y deja **No workload** para la primera captura.
3. Revisa los recursos y la red aislada predeterminada y pulsa **Create target**. La sesión se crea automáticamente; no hay un botón para iniciarla por separado.
4. Espera a que la VM esté en ejecución, selecciónala y captura memoria o disco desde sus detalles. Espera a que termine antes de descargar el artefacto, manifiesto y checksum.
5. Las evidencias se conservan en **Storage**, también después de **End Session**. El cierre inicia una limpieza asíncrona: espera el resultado final e investiga cualquier fallo.

Para añadir una carga vulnerable, consulta primero [Preparar cargas](../user/PREBAKED_IMAGES.md): en redes aisladas los instaladores necesitan una caché o imagen preparada compatible; si necesitan Internet, elige explícitamente una red con salida. El asistente revisa el formulario y muestra este requisito, pero no comprueba la caché ni la preparación del host.

Después, [analiza la captura exacta](../user/VOLATILITY3.md) y [exporta sus evidencias](../reference/FORENSIC_INTEGRITY.md). Registra la identidad del kernel antes de cerrar la VM. Los [paquetes portátiles](../user/PORTABLE_LABS.md) siguen desactivados, son experimentales y quedan fuera de los flujos soportados de 1.0.0.

## Adquisición repetible con Jobs

Abre **Jobs → New Job** desde la barra lateral. Selecciona un target y `immediate-capture` para captura directa o `lab-session` para un laboratorio manual. Lanza el job y espera los artefactos. Jobs también prepara la caché de cargas. Cierra la sesión Playground ordinaria y confirma su limpieza antes de cambiar a Jobs.

## Siguientes pasos

- [Ejecución local](running-locally.md) cubre la configuración completa y el runbook.
- [Configuración](configuration.md) explica `astinuslab.toml`, el entorno del lanzador y los catálogos.
- [Ejemplos](examples.md) ofrece llamadas comunes a la API.
- [Resolución de problemas](troubleshooting.md) lista errores y soluciones habituales.
