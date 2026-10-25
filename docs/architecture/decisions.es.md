# Decisiones

Esta página registra las decisiones arquitectónicas actuales que afectan a la configuración, las operaciones y el mantenimiento.

## Usar adquisición del lado del host

Los artefactos de memoria y disco se capturan desde el host mediante libvirt y herramientas QEMU. Se evitan agentes del lado guest por integridad de adquisición y compatibilidad de distribuciones.

## Mantener un único modo de runner activo

Los runners de desarrollo y release no deben ejecutarse al mismo tiempo. Ambos pueden consumir tareas de Celery y operar sobre recursos libvirt, por lo que el modo mixto puede enviar jobs al runner equivocado o colisionar en nombres de VM, redes y almacenamiento.

## Usar SQLite para el estado local

El proyecto usa SQLite para la base de datos de la aplicación, el broker de Celery y el backend de resultados de Celery. La concurrencia soportada del runner es `1`, alineada con el diseño local de un solo host y evitando carreras de estado compartido.

## Usar helpers privilegiados instalados

El runner no recibe sudo sin contraseña amplio. Puede llamar scripts helper instalados específicos bajo `/usr/local/libexec/astinuslab/` mediante `/etc/sudoers.d/astinuslab-runner`.

## Tratar los catálogos como datos de origen mantenidos por el usuario

Targets, scenarios, workloads y attackers se declaran en catálogos YAML. El descubrimiento automático de imágenes puede añadir targets, pero los mantenedores deben verificar usuarios, familia de distro, perfil cloud-init, variante de OS y comportamiento de red.

## Hacer el Playground efímero, mantener las capturas duraderas

Las sesiones, VMs, redes y overlays de disco de Playground son temporales. Las capturas, manifiestos, checksums y filas de adquisición persisten después de destruir la sesión.

## Generación de ISF basada en VM

Las VMs frescas son la fuente de verdad para la identidad del kernel Linux y la generación de ISF de Volatility 3. Consulta [Generación de ISF basada en VM](adr-vm-based-isf-generation.md).

## La capacidad de VM es control de admisión, no concurrencia de workers (2026-07)

El límite configurable de VMs ejecutándose simultáneamente (`orchestration.max_concurrent_vms`) se aplica en la capa de producto contando filas `job_vms` en estados que consumen capacidad dentro de la transacción de admisión (`BEGIN IMMEDIATE`). Los workers de Celery permanecen en `--concurrency=1`: el broker SQLite, los locks de captura en proceso, la serialización de tareas beat y la reconciliación de un solo host asumen un runner serial, y esa invariante está fijada por `tests/python/test_deploy_config.py`.

El valor predeterminado es sin límite, preservando el comportamiento existente. Estados transitorios de VM como `destroying` y `teardown_failed` cuentan para el límite hasta que la reconciliación los resuelve.

## Usar límites de paquete, entorno de ejecución y runtime para la estructura del repositorio (2026-08)

El objetivo por etapas usa `src/astinuslab` para la aplicación instalable y los recursos web y de migración que pertenecen al paquete, una jerarquía canónica `tests`, `ops` para código de host y despliegue, y `tools` para utilidades de contribución y release. Mantiene `guest`, `config`, `images`, `vol3` y `storage` como límites visibles de primer nivel porque moverlos bajo contenedores genéricos añadiría riesgo de rutas sin aclarar la propiedad.

Las rutas del repositorio son interfaces de migración: cada movimiento requiere una matriz de productores y consumidores, verificación y rollback. Los imports internos y las rutas de código pueden romperse dentro de una etapa atómica, mientras que las rutas HTTP, nombres de tareas Celery, IDs de revisión Alembic, nombres de unidades systemd, claves de configuración, formatos de artefactos y catálogos, y URLs públicas estáticas permanecen como contratos estables.

Consulta la [política de ubicación del repositorio](repository-placement-policy.md), la [estructura objetivo](repository-target-layout.md) y la [matriz de migración de rutas](path-migration-matrix.md). Estos cinco registros internos de estructura son canónicos en inglés y están fuera de la navegación publicada de MkDocs.
