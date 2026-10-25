# Arquitectura del backend i desenvolupament

El backend d'AstinusLab és un sistema asíncron d'alt rendiment dissenyat per gestionar el cicle de vida complet de les VMs de lab forense.

## Stack tecnològic

*   **Llenguatge**: Python 3.12 / 3.13 (matriu provada)
*   **Framework web**: FastAPI (API REST asíncrona + plantilles d'UI Jinja2)
*   **Cua de tasques**: Celery (orquestració asíncrona de jobs en segon pla)
*   **Base de dades**: SQLite (ORM SQLAlchemy)
*   **Virtualització**: libvirt (QEMU/KVM)
*   **Comunicació**: SSH (per a la interacció amb el guest), virtiofs (per compartir fitxers host-guest)
*   **Inspecció d'imatges**: `virt-inspector` de `libguestfs-tools` quan està disponible, amb heurístiques de nom de fitxer com a fallback

---

## Prerequisits de l'host per a l'extracció d'ISF

L'extracció de símbols ISF (Volatility 3) requereix els paquets següents a l'host.
L'orquestrador executa una comprovació preflight a l'arrencada del job i avorta amb
un missatge accionable si en falta algun.

```bash
sudo apt install \
  rpm2cpio cpio \
  libguestfs-tools \
  qemu-utils \
  libvirt-clients
```

`rpm2cpio` és necessari per extreure `vmlinux` dels RPM de kernel-debuginfo de
Fedora/RHEL. `libguestfs-tools` proporciona `virt-customize` i `virt-inspector`.
`qemu-utils` proporciona `qemu-img`. `libvirt-clients` proporciona `virsh`.

---

## Visió general de l'arquitectura

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
|  SQLite  |  |  Broker Celery (cua SQLite)   |
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
          |    QEMU    |  | via SSH  |  |          |
          +------------+  +----------+  +----------+
```

Els ajustos de l'aplicació es carreguen des de `astinuslab.toml` al checkout del codi
font / directori d'instal·lació. `src/astinuslab/core/config.py` també aplica
sobreescriptures d'entorn `ASTINUSLAB_<SETTINGS_FIELD>` per als camps del model
aplanat `Settings`.

### Components principals

| Component | Responsabilitat |
| :--- | :--- |
| **API (`api/routes.py`)** | Gestiona les peticions REST, la validació d'entrada i l'enviament de jobs. |
| **Serveis de domini (`jobs/job_services.py`, `forensics/forensic_services.py`, `forensics/case_services.py`)** | Orquestren la lògica de negoci entre repositoris i tasques. |
| **Repositori (`src/astinuslab/db/repositories.py`)** | Gestiona la persistència a DB i aplica la **màquina d'estats de Job**. |
| **Flux d'execució de Job (`orchestration/runners/boot_capture_runner.py`, `orchestration/deps.py`)** | El motor del cicle de vida de la VM (Boot, Wait, Capture, Teardown). |
| **Cicle de vida de VM (`virtualization/vm_lifecycle.py`)** | Generació de XML de domini libvirt de baix nivell i gestió d'overlays de disc. |
| **Suport de workloads (`workloads/workload_support.py`)** | Gestiona el lliurament d'scripts, l'execució i el monitoratge de salut. |
| **Paquet Playground (`playground/`)** | Propietari de l'assemblatge de l'API de Playground, rutes, orquestració, helpers de persistència, workloads, reconciliació i receptes JSON. Les façanes d'arrel `playground_*.py` han estat eliminades. |

---

## 1. La màquina d'estats de Job

La màquina d'estats és la font de veritat per a totes les activitats del job. Les transicions s'apliquen estrictament a `JobRepository.transition_job_status()`.

**Estats clau:**
*   `queued`: Estat inicial.
*   `preparing_image`: Creant overlays QCOW2 i seeds de cloud-init.
*   `waiting_for_guest_access`: VM arrencada, esperant resolució d'IP i SSH.
*   `mounting_output_share`: Preparant el mount virtiofs o SSH per al lliurament d'artefactes.
*   `generating_isf`: Construint o reutilitzant un ISF de Volatility 3 sense capturar memòria/disc.
*   `running_scenario_script`: Executant scripts personalitzats post-boot o d'escenari.
*   `provisioning_workload`: Executant scripts d'instal·lació i verificant la salut.
*   `waiting_for_capture`: VM a punt per a l'operador.
*   `capture_requested`: Tasca d'adquisició disparada via UI o API.
*   `acquiring_memory`: Executant `virDomainCoreDump` (captura de RAM).
*   `acquiring_disk`: Executant `qemu-img convert` (captura de disc).
*   `publishing_artifact`: Finalitzant i movent els artefactes capturats a l'emmagatzematge de l'host.
*   `cancel_requested`: Cancel·lació pendent abans del desmuntatge complet. Tingues en compte que les accions `cancel` i `force-fail` ara terminen immediatament i forçosament qualsevol subprocés d'OS actiu subjacent que s'executi al worker per a aquell job.
*   `completed` / `failed` / `cancelled`: Estats terminals.

---

## 2. Execució de tasques en segon pla

Les operacions de llarga durada (com l'arrencada de la VM o el bolcat de memòria) es despatxen com a tasques de Celery.

*   **Broker**: Basat en SQLite (via `kombu.transport.sqlite`).
*   **Definició de tasques**: Ubicada a `src/astinuslab/jobs/celery_app.py`.
*   **Mapeig**: `TASK_MAP` mapeja els tipus d'acció a funcions de Python.

---

## 3. Comunicació host-guest

AstinusLab evita els agents "pesats" dins del guest. En lloc d'això, fa servir:
*   **SSH**: Transport principal per a l'execució d'ordres.
*   **virtiofs**: Compartició de directoris de l'host d'alt rendiment (muntat a `/mnt/astinuslab` al guest).
*   **QEMU Guest Agent (opcional)**: Es fa servir per a apagades ordenades i recol·lecció avançada de metadades si és present.
*   **Esdeveniments de libvirt**: Es fan servir per detectar canvis d'estat d'energia de la VM.

---

## 4. Flux de desenvolupament

### Afegir un nou endpoint d'API
1.  Defineix el schema Pydantic a `src/astinuslab/api/schemas.py`.
2.  Afegeix la ruta a `src/astinuslab/api/routes.py`.
3.  Implementa la lògica a `src/astinuslab/jobs/job_services.py` (Jobs), `src/astinuslab/forensics/forensic_services.py` (Acquisitions), `src/astinuslab/playground/` (Playground).
4.  Afegeix tests a `tests/python/api/test_api.py`.

### Afegir una nova acció de segon pla
1.  Implementa la lògica a `src/astinuslab/orchestration/runners/boot_capture_runner.py` o un mòdul de suport.
2.  Afegeix un string de tipus d'acció a `TASK_MAP` a `src/astinuslab/jobs/celery_app.py`.
3.  Assegura't que la màquina d'estats de job a `src/astinuslab/db/repositories.py` suporta les transicions requerides.

---

## 5. Mandats de seguretat

*   **Sanititzaació d'artefactes**: Fes servir sempre `src/astinuslab/jobs/job_services.py:safe_artifact_path` per evitar el path traversal en descarregar bolcats.
*   **Maneig de credencials**: No emmagatzemis mai contrasenyes en text pla a la DB; fes servir el `Keystore` per a les claus SSH.
*   **Aïllament de xarxa**: Fes servir els hooks d'`iptables` a `workload_support.py` per assegurar que les VMs vulnerables no puguin atacar l'host o la LAN.

---

## 6. Proves

Executa la suite de tests completa abans de fer commit:
```bash
.venv/bin/python -m pytest tests/python/
```

Tests de mòduls individuals:
```bash
.venv/bin/python -m pytest tests/python/jobs/test_repositories.py
.venv/bin/python -m pytest tests/python/orchestration/test_lab_orchestrator.py
```
