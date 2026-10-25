# Arquitectura del backend y desarrollo

El backend de AstinusLab es un sistema asíncrono de alto rendimiento diseñado para gestionar el ciclo de vida completo de las VMs de lab forense.

## Stack tecnológico

*   **Lenguaje**: Python 3.12 / 3.13 (matriz probada)
*   **Framework web**: FastAPI (API REST asíncrona + plantillas de UI Jinja2)
*   **Cola de tareas**: Celery (orquestación asíncrona de jobs en segundo plano)
*   **Base de datos**: SQLite (ORM SQLAlchemy)
*   **Virtualización**: libvirt (QEMU/KVM)
*   **Comunicación**: SSH (para interacción con el guest), virtiofs (para compartir archivos host-guest)
*   **Inspección de imágenes**: `virt-inspector` de `libguestfs-tools` cuando está disponible, con heurísticas de nombre de archivo como fallback

---

## Prerrequisitos del host para la extracción de ISF

La extracción de símbolos ISF (Volatility 3) requiere los siguientes paquetes en el
host. El orquestador ejecuta una comprobación preflight al arrancar el job y aborta
con un mensaje accionable si falta alguno.

```bash
sudo apt install \
  rpm2cpio cpio \
  libguestfs-tools \
  qemu-utils \
  libvirt-clients
```

`rpm2cpio` es necesario para extraer `vmlinux` de los RPM de kernel-debuginfo de
Fedora/RHEL. `libguestfs-tools` proporciona `virt-customize` y `virt-inspector`.
`qemu-utils` proporciona `qemu-img`. `libvirt-clients` proporciona `virsh`.

---

## Visión general de la arquitectura

```text
+-----------------------+
|  Frontend UI / HTMX   |
+-----------------------+
            |
            v
+-----------------------+
|      FastAPI API      |
+-----------------------+
     |             |
     v             v
+----------+  +-------------------------------+
|  SQLite  |  |  Celery Broker (cola SQLite)  |
+----------+  +-------------------------------+
                               |
                               v
              +-------------------------------+
              |  Celery Worker / Orchestrator  |
              +-------------------------------+
                 |             |             |
                 v             v             v
          +------------+  +----------+  +----------+
          |  libvirt / |  | VM Guest |  |  SQLite  |
          |    QEMU    |  | vía SSH  |  |          |
          +------------+  +----------+  +----------+
```

Los ajustes de la aplicación se cargan desde `astinuslab.toml` en el checkout del
código fuente / directorio de instalación. `src/astinuslab/core/config.py` también aplica
sobreescrituras de entorno `ASTINUSLAB_<SETTINGS_FIELD>` para los campos del modelo
aplanado `Settings`.

### Componentes principales

| Componente | Responsabilidad |
| :--- | :--- |
| **API (`api/routes.py`)** | Maneja las peticiones REST, la validación de entrada y el envío de jobs. |
| **Servicios de dominio (`jobs/job_services.py`, `forensics/forensic_services.py`, `forensics/case_services.py`)** | Orquestan la lógica de negocio entre repositorios y tareas. |
| **Repositorio (`src/astinuslab/db/repositories.py`)** | Gestiona la persistencia en DB y aplica la **máquina de estados de Job**. |
| **Flujo de ejecución de Job (`orchestration/runners/boot_capture_runner.py`, `orchestration/deps.py`)** | El motor del ciclo de vida de la VM (Boot, Wait, Capture, Teardown). |
| **Ciclo de vida de VM (`virtualization/vm_lifecycle.py`)** | Generación de XML de dominio libvirt de bajo nivel y gestión de overlays de disco. |
| **Soporte de workloads (`workloads/workload_support.py`)** | Maneja la entrega de scripts, la ejecución y la monitorización de salud. |
| **Paquete Playground (`playground/`)** | Propietario del ensamblaje de la API de Playground, rutas, orquestación, helpers de persistencia, workloads, reconciliación y recetas JSON. Las fachadas de nivel raíz `playground_*.py` han sido eliminadas. |

---

## 1. La máquina de estados de Job

La máquina de estados es la fuente de verdad para todas las actividades del job. Las transiciones se aplican estrictamente en `JobRepository.transition_job_status()`.

**Estados clave:**
*   `queued`: Estado inicial.
*   `preparing_image`: Creando overlays QCOW2 y seeds de cloud-init.
*   `waiting_for_guest_access`: VM arrancada, esperando resolución de IP y SSH.
*   `mounting_output_share`: Preparando el mount virtiofs o SSH para la entrega de artefactos.
*   `generating_isf`: Construyendo o reutilizando un ISF de Volatility 3 sin capturar memoria/disco.
*   `running_scenario_script`: Ejecutando scripts personalizados post-boot o de escenario.
*   `provisioning_workload`: Ejecutando scripts de instalación y verificando la salud.
*   `waiting_for_capture`: VM lista para el operador.
*   `capture_requested`: Tarea de adquisición disparada vía UI o API.
*   `acquiring_memory`: Ejecutando `virDomainCoreDump` (captura de RAM).
*   `acquiring_disk`: Ejecutando `qemu-img convert` (captura de disco).
*   `publishing_artifact`: Finalizando y moviendo los artefactos capturados al almacenamiento del host.
*   `cancel_requested`: Cancelación pendiente antes del desmontaje completo. Ten en cuenta que las acciones `cancel` y `force-fail` ahora terminan inmediata y forzosamente cualquier subproceso de OS activo subyacente que se ejecute en el worker para ese job.
*   `completed` / `failed` / `cancelled`: Estados terminales.

---

## 2. Ejecución de tareas en segundo plano

Las operaciones de larga duración (como el arranque de la VM o el volcado de memoria) se despachan como tareas de Celery.

*   **Broker**: Basado en SQLite (vía `kombu.transport.sqlite`).
*   **Definición de tareas**: Ubicada en `src/astinuslab/jobs/celery_app.py`.
*   **Mapeo**: `TASK_MAP` mapea los tipos de acción a funciones de Python.

---

## 3. Comunicación host-guest

AstinusLab evita los agentes "pesados" dentro del guest. En su lugar, usa:
*   **SSH**: Transporte principal para la ejecución de comandos.
*   **virtiofs**: Compartición de directorios del host de alto rendimiento (montado en `/mnt/astinuslab` en el guest).
*   **QEMU Guest Agent (opcional)**: Usado para apagados ordenados y recolección avanzada de metadatos si está presente.
*   **Eventos de libvirt**: Usados para detectar cambios de estado de energía de la VM.

---

## 4. Flujo de desarrollo

### Añadir un nuevo endpoint de API
1.  Define el schema Pydantic en `src/astinuslab/api/schemas.py`.
2.  Añade la ruta a `src/astinuslab/api/routes.py`.
3.  Implementa la lógica en `src/astinuslab/jobs/job_services.py` (Jobs), `src/astinuslab/forensics/forensic_services.py` (Acquisitions), `src/astinuslab/playground/` (Playground).
4.  Añade tests en `tests/python/api/test_api.py`.

### Añadir una nueva acción de segundo plano
1.  Implementa la lógica en `src/astinuslab/orchestration/runners/boot_capture_runner.py` o un módulo de soporte.
2.  Añade un string de tipo de acción a `TASK_MAP` en `src/astinuslab/jobs/celery_app.py`.
3.  Asegúrate de que la máquina de estados de job en `src/astinuslab/db/repositories.py` soporta las transiciones requeridas.

---

## 5. Mandatos de seguridad

*   **Sanitización de artefactos**: Usa siempre `src/astinuslab/jobs/job_services.py:safe_artifact_path` para evitar el path traversal al descargar volcados.
*   **Manejo de credenciales**: Nunca almacenes contraseñas en texto plano en la DB; usa el `Keystore` para las claves SSH.
*   **Aislamiento de red**: Usa los hooks de `iptables` en `workload_support.py` para asegurar que las VMs vulnerables no puedan atacar al host o la LAN.

---

## 6. Pruebas

Ejecuta la suite de tests completa antes de hacer commit:
```bash
.venv/bin/python -m pytest tests/python/
```

Tests de módulos individuales:
```bash
.venv/bin/python -m pytest tests/python/jobs/test_repositories.py
.venv/bin/python -m pytest tests/python/orchestration/test_lab_orchestrator.py
```
