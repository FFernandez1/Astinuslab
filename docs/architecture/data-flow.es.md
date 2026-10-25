# Flujo de datos

## Flujo de un Job forense

```text
1. Operator -> FastAPI: POST /api/v1/jobs
2. FastAPI -> SQLite: persist queued Job
3. FastAPI -> Celery broker: enqueue task
4. Lifecycle runner -> SQLite: preparing_image
5. Lifecycle runner -> libvirt: overlay, seed, domain
6. Lifecycle runner -> guest: wait for IP/SSH; run scenario/workload
7. Lifecycle runner -> libvirt: capture memory and/or disk
8. Lifecycle runner -> storage: artifact + manifest + SHA-256
9. Lifecycle runner -> SQLite: completed / failed / cancelled
10. Operator -> FastAPI: download published evidence
```

Todos los cambios de estado de un job deben pasar por la lógica de transición del repositorio. Los estados terminales son `completed`, `failed` y `cancelled`.

## Flujo de lab manual

Los jobs de lab manual usan escenarios `manual_trigger`:

1. El runner arranca la VM target.
2. El workload seleccionado se instala y se comprueba su salud.
3. Se registran metadatos opcionales de runtime de la VM atacante.
4. El job espera en `waiting_for_capture`.
5. El operador llama a `/api/v1/jobs/{job_id}/capture` o usa la UI.
6. El runner captura memoria, disco o ambos.

## Flujo del Playground

El Playground usa el mismo host runner pero una superficie de API y un modelo de estado diferentes:

1. Crear o reutilizar una sesión de playground activa.
2. Crear redes de sesión respaldadas por libvirt.
3. Desplegar VMs desde imágenes del catálogo.
4. Conectar o desconectar NICs según sea necesario.
5. Disparar capturas de memoria o disco a nivel de VM.
6. Destruir la sesión.

Los overlays de VM del playground son efímeros. Los artefactos de captura del playground persisten hasta que se eliminan explícitamente.

## Flujo de artefactos

La captura de memoria usa la funcionalidad de core dump de libvirt desde el host. La captura de disco convierte el estado del disco de la VM en un artefacto descargable. Para los artefactos completados, AstinusLab almacena metadatos de ruta, nombre de archivo, tamaño y SHA-256, y sirve los archivos a través de los endpoints de artefactos con cabeceras de checksum cuando están disponibles.

## Flujo de Volatility 3

Para el análisis de memoria de Linux, AstinusLab identifica el kernel del guest y prueba los proveedores de ISF en orden:

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

Si la cadena del host falla, AstinusLab prueba el fallback del guest en vivo, que puede usar un `vmlinux` coincidente con DWARF, debuginfod o una instalación de paquetes de depuración permitida explícitamente. `dwarf2json` no admite una entrada que solo contenga BTF y este fallback nunca la utiliza. Los ISFs reutilizables se almacenan en `vol3/symbols/linux/`. La salida de Volatility por job se almacena en `storage/dumps/<job_id>/vol3/`.
