# Desenvolupament de workloads i escenaris

Aquesta guia explica com crear, configurar i depurar workloads i escenaris (tasques automatitzades) a AstinusLab.

## Visió general

Una **VM Task** a AstinusLab pot ser un workload persistent o un script d'escenari autònom:

1.  **Workload**: Un conjunt de serveis o configuracions instal·lades per a ús interactiu (p. ex., una aplicació web vulnerable). Es fa servir amb el mode `manual_trigger`.
2.  **Escenari**: Una configuració de cicle de vida que determina quan es produeix una captura. Els escenaris personalitzats fan servir el mode `post_script` per executar una tasca autònoma i capturar immediatament en completar-se.

---

## 1. Crear un workload únic

### Pas A: Crear l'script d'instal·lació
Col·loca el teu script bash a `guest/workloads/<workload-id>/install.sh`.

**Pautes per als scripts d'instal·lació:**
*   **Fes servir `set -euo pipefail`**: Assegura que l'script surti davant de qualsevol error.
*   **No interactiu**: Fes servir `export DEBIAN_FRONTEND=noninteractive` i els flags `-y` per als gestors de paquets (p. ex., `apt-get install -y`).
*   **Idempotència**: Els scripts haurien de poder executar-se idealment diverses vegades, encara que AstinusLab normalment els executa un cop sobre una imatge neta.
*   **Logging**: Fes servir `echo "[my-workload] message"` per proporcionar actualitzacions de progrés que apareixen a la UI d'AstinusLab.
*   **ShellCheck**: AstinusLab executa `shellcheck` sobre el teu script abans de l'execució. Evita variables sense fer servir o errors de sintaxi.

**Exemple (`guest/workloads/hello-world/install.sh`):**
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

### Pas B: Registrar a `config/catalog/workloads.yaml`
Afegeix una entrada a la llista `workloads`:

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

Els stacks et permeten combinar workloads existents sense duplicar scripts.

**Exemple (`config/catalog/workloads.yaml`):**
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

## 3. Referència de configuració

| Camp | Tipus | Descripció |
| :--- | :--- | :--- |
| `id` | string | Identificador únic que es fa servir a l'API i la DB. |
| `type` | string | `stack` per a workloads multi-component, omet per a un de sol. |
| `display_name` | string | Nom llegible que es mostra a la UI. |
| `install_script` | path | Ruta relativa a l'arrel del repositori. |
| `health_check` | object | (Opcional) URL a sondejar per verificar que el workload està a punt. |
| `services` | list | Serveis a mostrar al panell "Lab Services" de la UI. |
| `cve_ids` | list | CVEs associats amb aquest workload (per a cerca). |
| `network_isolation` | bool | Si és true, bloqueja l'accés sortint de la VM a internet/LAN després de l'aprovisionament. |
| `compatibility` | object | Regles per emparellar aquest workload amb imatges de SO target. |

### Regles de compatibilitat
Els workloads es poden restringir a targets específics segons `family` (p. ex., `debian`, `redhat`), `distro` (p. ex., `ubuntu`) o `version`.

```yaml
compatibility:
  include:
    - family: debian
      version: '22.04'
  exclude:
    - distro: kali
```

---

## 4. El cicle de vida de l'aprovisionament

Quan un usuari llança un workload:
1.  **Preparació d'imatge**: AstinusLab crea un overlay QCOW2 de la imatge base target.
2.  **Arrencada de la VM**: La VM s'inicia amb libvirt.
3.  **Espera de SSH**: El runner espera que el guest sigui accessible via SSH.
4.  **Sincronització del workload**: El repositori es munta al guest via `virtiofs` (o es copia via SSH si es fa servir el fallback).
5.  **Execució**: L'`install_script` s'executa dins del guest.
6.  **Health check**: El runner sondeja la URL de `health_check` fins que retorna el `expected_status`.
7.  **Snapshot**: Es crea un snapshot de libvirt (`lab-clean`).
8.  **A punt**: El job entra en estat `waiting_for_capture` i es lliura a l'usuari.

---

## 5. Crear escenaris personalitzats (tasques)

Mentre que els workloads són per a labs interactius, els **Escenaris** et permeten automatitzar una tasca (p. ex., simular un atac o un esdeveniment del sistema) i capturar l'estat de la VM automàticament quan acaba.

### Pas A: Crear l'script de l'escenari
Els scripts d'escenari són scripts bash estàndard col·locats a `guest/scripts/`.

**Exemple (`guest/scripts/simulate-infection.sh`):**
```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[task] Simulating a malicious process..."
# In a real scenario, this might download a sample or execute a payload
sleep 10
nohup python3 -m http.server 9999 &

echo "[task] Simulation complete. Signalling for capture."
```

### Pas B: Registrar a `config/catalog/scenarios.yaml`
Afegeix un nou escenari amb `mode: post_script`:

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

### Quan fer servir escenaris vs. workloads?
*   **Fes servir un Escenari** quan vulguis una tasca "fire and forget" que condueix a una captura automatitzada.
*   **Fes servir un Workload** quan vulguis aprovisionar un entorn estable per a exploració manual o múltiples punts de captura.

---

## 6. Resolució de problemes i depuració

### Veure logs
*   **Costat de l'host**: Revisa `storage/logs/runner.log` per errors d'execució de l'script i codis de sortida.
*   **Costat de la UI**: El "Event Log" a la pàgina de detall del Job mostra la sortida en temps real de l'script d'instal·lació.

### Aprovisionament fallit
Si un workload falla:
1.  Comprova si `shellcheck` ha reportat errors al Event Log.
2.  Revisa el "Error Summary" per codis de sortida.
3.  Posa `keep_failed_vm = true` a `config/catalog/targets.yaml` per al target que estàs provant. Això evita que AstinusLab elimini la VM en cas de fallada, permetent-te fer SSH manualment i depurar:
    ```bash
    ssh -i storage/ssh_keys/key <guest_user>@<guest_ip>
    ```

### Problemes habituals
*   **Permission Denied**: Assegura't que el teu script fa servir `sudo` per a les ordres que requereixen root, ja que els scripts s'executen com el `guest_user` per defecte (normalment `ubuntu` o `cloud-user`).
*   **Dependències que falten**: Recorda que les imatges cloud base solen ser mínimes. Pot ser que necessitis executar `apt-get update` abans d'instal·lar paquets.
*   **Timeouts de xarxa**: Si el teu script descarrega fitxers grans, assegura't que el `timeout_seconds` al `health_check` sigui prou alt per cobrir tot el temps d'instal·lació.
