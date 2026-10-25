# Desarrollo de workloads y escenarios

Esta guía explica cómo crear, configurar y depurar workloads y escenarios (tareas automatizadas) en AstinusLab.

## Visión general

Una **VM Task** en AstinusLab puede ser un workload persistente o un script de escenario autónomo:

1.  **Workload**: Un conjunto de servicios o configuraciones instaladas para uso interactivo (p. ej., una aplicación web vulnerable). Se usa con el modo `manual_trigger`.
2.  **Escenario**: Una configuración de ciclo de vida que determina cuándo ocurre una captura. Los escenarios personalizados usan el modo `post_script` para ejecutar una tarea autónoma y capturar inmediatamente al completarse.

---

## 1. Crear un workload único

### Paso A: Crear el script de instalación
Coloca tu script bash en `guest/workloads/<workload-id>/install.sh`.

**Pautas para los scripts de instalación:**
*   **Usa `set -euo pipefail`**: Asegura que el script salga ante cualquier error.
*   **No interactivo**: Usa `export DEBIAN_FRONTEND=noninteractive` y los flags `-y` para los gestores de paquetes (p. ej., `apt-get install -y`).
*   **Idempotencia**: Los scripts deberían idealmente poder ejecutarse varias veces, aunque AstinusLab normalmente los ejecuta una vez sobre una imagen limpia.
*   **Logging**: Usa `echo "[my-workload] message"` para proporcionar actualizaciones de progreso que aparecen en la UI de AstinusLab.
*   **ShellCheck**: AstinusLab ejecuta `shellcheck` sobre tu script antes de la ejecución. Evita variables sin usar o errores de sintaxis.

**Ejemplo (`guest/workloads/hello-world/install.sh`):**
```bash
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[hello-world] Installing Nginx..."
sudo apt-get update -qq
sudo apt-get install -y nginx

echo "[hello-world] Configuring landing page..."
echo "<h1>AstinusLab Workload Ready</h1>" | sudo tee /var/www/html/index.html

echo "[hello-world] Installation complete."
```

### Paso B: Registrar en `config/catalog/workloads.yaml`
Añade una entrada a la lista `workloads`:

```yaml
- id: hello-world
  enabled: true
  display_name: Hello World Nginx
  tags:
    - web
    - tutorial
  install_script: guest/workloads/hello-world/install.sh
  health_check:
    url_path: /
    port: 80
    expected_status: 200
    timeout_seconds: 60
  services:
    - name: HTTP
      port: 80
      url_path: /
  compatibility:
    include:
      - family: debian
```

---

## 2. Crear un stack de workloads

Los stacks te permiten combinar workloads existentes sin duplicar scripts.

**Ejemplo (`config/catalog/workloads.yaml`):**
```yaml
- id: lamp-stack
  type: stack
  enabled: true
  display_name: Full LAMP Stack
  components:
    - mysql-base
    - apache-php
    - wordpress-6.4
  compatibility:
    include:
      - family: debian
```

---

## 3. Referencia de configuración

| Campo | Tipo | Descripción |
| :--- | :--- | :--- |
| `id` | string | Identificador único usado en la API y la DB. |
| `type` | string | `stack` para workloads multi-componente, omitir para uno único. |
| `display_name` | string | Nombre legible mostrado en la UI. |
| `install_script` | path | Ruta relativa a la raíz del repositorio. |
| `health_check` | object | (Opcional) URL a sondear para verificar que el workload está listo. |
| `services` | list | Servicios a mostrar en el panel "Lab Services" de la UI. |
| `cve_ids` | list | CVEs asociados con este workload (para búsqueda). |
| `network_isolation` | bool | Si es true, bloquea el acceso de la VM a internet/LAN saliente tras el aprovisionamiento. |
| `compatibility` | object | Reglas para emparejar este workload con imágenes de SO target. |

### Reglas de compatibilidad
Los workloads pueden restringirse a targets específicos según `family` (p. ej., `debian`, `redhat`), `distro` (p. ej., `ubuntu`) o `version`.

```yaml
compatibility:
  include:
    - family: debian
      version: '22.04'
  exclude:
    - distro: kali
```

---

## 4. El ciclo de vida del aprovisionamiento

Cuando un usuario lanza un workload:
1.  **Preparación de imagen**: AstinusLab crea un overlay QCOW2 de la imagen base target.
2.  **Arranque de la VM**: La VM se inicia usando libvirt.
3.  **Espera de SSH**: El runner espera a que el guest sea accesible vía SSH.
4.  **Sincronización del workload**: El repositorio se monta en el guest vía `virtiofs` (o se copia vía SSH si se usa el fallback).
5.  **Ejecución**: El `install_script` se ejecuta dentro del guest.
6.  **Health check**: El runner sondea la URL de `health_check` hasta que devuelve el `expected_status`.
7.  **Snapshot**: Se crea un snapshot de libvirt (`lab-clean`).
8.  **Listo**: El job entra en estado `waiting_for_capture` y se entrega al usuario.

---

## 5. Crear escenarios personalizados (tareas)

Mientras que los workloads son para labs interactivos, los **Escenarios** te permiten automatizar una tarea (p. ej., simular un ataque o un evento del sistema) y capturar el estado de la VM automáticamente cuando termina.

### Paso A: Crear el script del escenario
Los scripts de escenario son scripts bash estándar colocados en `guest/scripts/`.

**Ejemplo (`guest/scripts/simulate-infection.sh`):**
```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[task] Simulating a malicious process..."
# In a real scenario, this might download a sample or execute a payload
sleep 10
nohup python3 -m http.server 9999 &

echo "[task] Simulation complete. Signalling for capture."
```

### Paso B: Registrar en `config/catalog/scenarios.yaml`
Añade un nuevo escenario usando `mode: post_script`:

```yaml
- id: auto-infect-capture
  enabled: true
  mode: post_script
  description: Simulates an infection and captures immediately
  script:
    enabled: true
    path: guest/scripts/simulate-infection.sh
    timeout_seconds: 300
    fail_job_on_nonzero_exit: true
```

### ¿Cuándo usar escenarios vs. workloads?
*   **Usa un Escenario** cuando quieras una tarea "fire and forget" que conduce a una captura automatizada.
*   **Usa un Workload** cuando quieras aprovisionar un entorno estable para exploración manual o múltiples puntos de captura.

---

## 6. Resolución de problemas y depuración

### Ver logs
*   **Lado del host**: Revisa `storage/logs/runner.log` por errores de ejecución del script y códigos de salida.
*   **Lado de la UI**: El "Event Log" en la página de detalle del Job muestra la salida en tiempo real del script de instalación.

### Aprovisionamiento fallido
Si un workload falla:
1.  Comprueba si `shellcheck` reportó errores en el Event Log.
2.  Revisa el "Error Summary" por códigos de salida.
3.  Pon `keep_failed_vm = true` en `config/catalog/targets.yaml` para el target que estás probando. Esto evita que AstinusLab elimine la VM en caso de fallo, permitiéndote hacer SSH manualmente y depurar:
    ```bash
    ssh -i storage/ssh_keys/key <guest_user>@<guest_ip>
    ```

### Problemas comunes
*   **Permission Denied**: Asegúrate de que tu script usa `sudo` para los comandos que requieren root, ya que los scripts se ejecutan como el `guest_user` por defecto (normalmente `ubuntu` o `cloud-user`).
*   **Dependencias faltantes**: Recuerda que las imágenes cloud base suelen ser mínimas. Puede que necesites ejecutar `apt-get update` antes de instalar paquetes.
*   **Timeouts de red**: Si tu script descarga archivos grandes, asegúrate de que el `timeout_seconds` en el `health_check` sea lo bastante alto para cubrir todo el tiempo de instalación.
