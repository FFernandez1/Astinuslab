# Visión general de la arquitectura

AstinusLab es un sistema de orquestación de labs local. La API y la UI aceptan peticiones del operador, persisten el estado de los jobs en SQLite, encolan trabajo de larga duración mediante Celery, y un host runner controla libvirt, SSH, captura de disco, captura de memoria y publicación de artefactos.

```text
Browser / API clients
        |
        v
FastAPI -----> SQLite application state
        |
        v
Celery SQLite broker
        |                           |
        v                           v
Lifecycle runner + Beat       Analysis worker
(concurrency 1, threads)       (analysis queue, concurrency 1)
        |                           |
        +--> libvirt / QEMU / KVM    +--> Volatility plugins
        +--> guest setup via SSH
        +--> capture / ISF work
        +--> artifacts and logs <---+--> reports
```

## Límites

| Límite | Responsabilidad |
| --- | --- |
| App FastAPI | API REST, UI HTML, validación de peticiones, creación de jobs, descargas de artefactos. |
| Base de datos SQLite | Registros de jobs, transiciones, eventos, acciones de host, runtimes, adquisiciones, estado del playground. |
| Worker de Celery | Ejecuta acciones de orquestación privilegiadas y de larga duración fuera del flujo petición/respuesta. |
| Scripts auxiliares de host | Realizan operaciones root restringidas mediante entradas de allow-list de sudoers instaladas. |
| libvirt/QEMU | Ciclo de vida de VMs, redes, overlays de disco, volcados de memoria y conversión de disco. |
| Catálogos | Definiciones declarativas de target, escenario, workload y VM atacante. |
| Almacenamiento | Artefactos capturados, logs, archivos temporales, bases de datos de Celery, claves SSH, caché de workloads. |

## Modos de operación

El modo de desarrollo se ejecuta desde el repositorio mediante `./astinuslab.sh`. El modo release copia la app a `/opt/astinuslab`, crea un virtualenv, instala los scripts auxiliares y las unidades systemd, y ejecuta los servicios como `astinuslab-api@USER` y `astinuslab-runner@USER`.

Los dos modos no deben ejecutarse juntos porque ambos runners pueden consumir la misma cola y controlar los mismos recursos de libvirt del host.

## Modelo de seguridad

AstinusLab cuenta con autenticación opcional de API con rol único de admin. Por defecto sigue pensado para su uso en entornos locales confiables. El modo de desarrollo se vincula a loopback a menos que se sobreescriba el host de enlace; los enlaces fuera de loopback requieren autenticación de API o una anulación insegura explícita. Para redes compartidas, sitúelo detrás de TLS, un proxy inverso, VPN y restricciones de cortafuegos. El inicio de sesión en el navegador (`/login`) emite una cookie de sesión independiente con hash en el servidor.
