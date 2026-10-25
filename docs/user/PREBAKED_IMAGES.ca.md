# Imatges pre-cuites i rendiment

Per a Windows, consulta [Imatges de Windows](WINDOWS_IMAGES.md), la guia autoritativa en anglès. Les receptes Linux d’aquesta pàgina no preparen Windows. Conserva les còpies de referència i recuperació.

Instal·lar workloads complexos (com un stack LAMP complet) pot trigar diversos minuts per job. Per aconseguir llançaments de lab quasi instantanis, AstinusLab et permet "coure" un workload directament en una nova imatge base.

## 0b. Càrregues de Playground en xarxes aïllades

La xarxa predeterminada de Playground no reenvia trànsit guest a Internet. Els instal·ladors sense memòria cau que utilitzen repositoris o Git necessiten preparació abans de llançar en aïllament. L’assistent mostra aquest requisit sense comprovar la memòria cau ni activar sortida.

1. Tanca una sessió Playground ordinària i confirma’n la neteja abans de passar a Jobs. Activa la clau SSH a **Security** i prepara la imatge base seleccionada.
2. En un host amb sortida explícita per a Jobs (normalment la xarxa NAT `default` de libvirt), executa un `lab-session` amb la imatge/càrrega desitjada. Espera l’aprovisionament correcte i `waiting_for_capture`; confirma una entrada coincident a `<storage_dir>/workload-cache/` abans de cancel·lar/tancar el Job i esperar-ne la neteja.
3. Torna a Playground i tria la mateixa imatge/càrrega a la xarxa aïllada. La clau de memòria cau vincula ID del target, càrrega, identitat d’imatge base i empremta de l’instal·lador. Una entrada coincident omet la instal·lació. Si falta, s’executa l’instal·lador en viu: prepara abans o tria explícitament una xarxa amb sortida.

Defineix `api_curl` des dels [Exemples API](../usage/examples.md) abans d’enviar el Job de preparació:

```bash
api_curl -sS -X POST "$BASE_URL/api/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"ubuntu-24.04","workload_id":"dvwa",
       "scenario_id":"lab-session","execution_mode":"manual_trigger"}'
```


Abans d’executar el helper, configura `ASTINUSLAB_DATA_DIR` amb l’arrel de dades i `ASTINUSLAB_TOKEN` amb el bearer token d’administrador. `BASE_URL` permet indicar l’URL de l’API local. Sense token, el helper usa cookie i capçalera CSRF per a desenvolupament sense autenticació. Consulta el contracte de connexió, els temps d’espera i la recuperació a la guia anglesa (selecciona English al menú d’idioma); els errors retornen un codi no nul i conserven el Job per inspeccionar-lo.

## 1. Coure un workload

Fes servir l'script `ops/host/bake_workload.sh` per automatitzar aquest procés. Crea un job temporal, instal·la el workload, apaga la VM i aplana el resultat en una nova imatge QCOW2 autònoma.

### Ús
```bash
./ops/host/bake_workload.sh <target_id> <workload_id> [output_image_name]
```

**Exemple**:
```bash
./ops/host/bake_workload.sh ubuntu-22-04-default wordpress-6.4 ubuntu-22.04-wordpress.qcow2
```

### Passos següents
Quan l'script acaba:
1. La imatge es desa a `images/definitions/` de l’arrel de dades configurada. El helper no la inspecciona: obre **Images**, demana la inspecció i verifica distribució, versió i usuari. Aplanar el disc no prova l’arrencada ni que el Workload estigui preparat.
2.  (Opcional) Afegeix una entrada de target curada a `config/catalog/targets.yaml`
    si vols sobreescriure els valors autodescoberts o fixar ajustos
    específics de l'escenari:
    ```yaml
    - id: ubuntu-22.04-wordpress-prebaked
      enabled: true
      distro: { slug: ubuntu, version: "22.04", family: debian }
      image:
        source_type: qcow2
        image_ref: ubuntu-22.04-wordpress.qcow2
        default_username: ubuntu
    ```
3. Actualitza el catàleg i valida la nova imatge en un lab d’un sol ús. El programari preparat ja és a la imatge; seleccionar un altre Workload pot executar-ne l’instal·lador.

---

## 2. Pre-coure imatges d'atacant

Les imatges d'atacant (Kali, Parrot) sovint requereixen eines i configuracions SSH específiques. Fes servir `ops/host/prebake_attacker_image.sh` per preparar-les.

### Ús
```bash
./ops/host/prebake_attacker_image.sh <source_image> <output_image> [attacker_user] [ssh_public_key]
```

**Exemple**:
```bash
./ops/host/prebake_attacker_image.sh \
  images/definitions/kali-linux-2024.qcow2 \
  images/definitions/kali-prebaked.qcow2 \
  kali \
  storage/ssh_keys/astinuslab-default.pub
```

### Per què fer-ho servir?
*   **Configuració SSH automàtica**: Injecta la clau pública del runner d'AstinusLab perquè pugui controlar la VM atacant.
*   **Guest Agent**: Instal·la `qemu-guest-agent` per a apagades ordenades.
*   **Velocitat**: No cal que cloud-init executi tasques pesades de configuració a cada arrencada.

---

## 3. Preparar imatges target stock

Les VMs **target** de Playground s'aprovisionen amb un seed cloud-init per VM. Algunes imatges cloud stock conserven estat cloud-init que impedeix reaplicar el seed, de manera que mai queden accessibles per SSH. A diferència del prebaking d'attacker, la preparació de target no cou un usuari o clau: el pas clau és `cloud-init clean`, que esborra l'estat stale perquè cloud-init s'executi al següent boot.

### Ús
```bash
# dry run first (no root, just prints what would be prepared):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy --resolve-only

# prepare it (in place; network-free):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy

# online host or image missing ssh? also apt-install ssh + guest agent:
ops/host/prepare_target_image.sh debian-12 --with-packages
```

Pots passar un id de target de catàleg o un filename sota `images/definitions/`. La imatge es reescriu atòmicament i el seu `.meta.json` es marca com `"prepared": true` en completar.

### Requisits
`libguestfs-tools` (proporciona `virt-customize`). `--with-packages` necessita també apt/xarxa dins de l'appliance de build. Si `virt-customize` falla amb un error de supermin, instal·la `libguestfs-tools`, permet llegir `/boot/vmlinuz-*` i confirma que `libguestfs-test-tool` surt 0.

## 4. Consells de rendiment

*   **Còpia amb reflink**: AstinusLab fa servir `cp --reflink=auto` en crear overlays. En sistemes de fitxers com ara **Btrfs** o **XFS**, això fa que la preparació d'imatges sigui quasi instantània i estalvia molt espai de disc.
*   **Emmagatzematge SSD**: Col·loca sempre els directoris `images/definitions` i `storage/` en un SSD per al millor rendiment de les VMs.
