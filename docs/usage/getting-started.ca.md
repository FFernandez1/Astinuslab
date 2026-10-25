# Primers passos

AstinusLab aprovisiona VMs Linux sobre KVM/libvirt, executa la configuració de l'escenari o workload, captura artefactes de memòria i disc des de l'host, registra checksums i metadades, i després desmunta els recursos del lab.


El host provat és Ubuntu 24.04 LTS x86_64 amb KVM. Les receptes per a altres hosts són experimentals. L’espai de descàrrega no equival a la capacitat operativa: el smoke estricte avisa per sota de 150 GB lliures o 16 GB de RAM; cada laboratori necessita el seu propi pressupost.

## Tria un mode d'execució

Fes servir un mode cada vegada:

| Mode | Fes-lo servir quan | Punt d'entrada |
| --- | --- | --- |
| Desenvolupament | Edites codi, proves en local o iteres sobre la documentació | `./astinuslab.sh` |
| Release | Executes AstinusLab com un servei local de llarga durada | `ops/deploy/install.sh` més unitats systemd |

No executis el runner de desenvolupament i el runner de systemd alhora. Comparteixen Celery, libvirt i els recursos de nomenclatura de VMs.

Les instal·lacions release activen l'autenticació admin per defecte. Una instal·lació neta mostra el token en crear-lo i una vegada més al bloc final; desa'l immediatament. Les actualitzacions conserven el hash existent. Per a una instal·lació sense autenticació, explícitament només de desenvolupament, passa `--allow-unauthenticated-local` a `ops/deploy/install.sh`. Consulta [Autenticació](../api/authentication.md) per a les regles de hash i opcions.

## Primera instal·lació release

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER"
cd /opt/astinuslab/current
sudo -u "$USER" ./ops/images/download_images.sh
./astinuslab.sh smoke --require-running
./astinuslab.sh guide-volatility  # opcional; només mostra una guia de lectura
```

El preflight és de només lectura i retorna un codi diferent de zero si falta un requisit. Desa el token de `CREDENTIALS — SAVE NOW`, mantén almenys 10 GB lliures per als aproximadament 3 GB d'imatges i obre la UI només quan passi el smoke estricte.

## Primer arrencada en local

```bash
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host --with-mac
./ops/images/download_images.sh
./astinuslab.sh start
./astinuslab.sh smoke --require-running
```

`./astinuslab.sh setup` inicialitza cada catàleg actiu de `config/catalog/` des del seu
fitxer `*.example.yaml` corresponent quan falta el fitxer actiu; no cal copiar
fitxers manualment (vegeu [Configuració](configuration.md)).

Obre `http://localhost:8000`.

Si també vols el servidor de documentació:

```bash
./astinuslab.sh start --docs
```

El servidor de docs fa servir `http://localhost:9000` per defecte.

## Afegir una imatge cloud

Col·loca imatges cloud de Linux a `images/definitions/`. Les entrades de catàleg existents referencien noms de fitxer com ara:

| Target ID | Nom de fitxer d'imatge esperat |
| --- | --- |
| `ubuntu-22-04-default` | `ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img` |
| `ubuntu-24.04` | `noble-server-cloudimg-amd64.img` |
| `debian-12` | `debian-12-genericcloud-amd64.qcow2` |
| `fedora-40` | `Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2` |

Per escanejar imatges acabades d'afegir:

```bash
./astinuslab.sh scan-images --dry-run
./astinuslab.sh scan-images
./astinuslab.sh restart
```

## Primera captura a Playground

1. Obre `http://localhost:8000` i inicia sessió si cal. La pàgina inicial és **Playground**. **Ajuda** redirigeix al servidor de documentació separat, normalment `http://localhost:9000`, en l'idioma triat. En desenvolupament, usa `./astinuslab.sh start --docs`. Amb una API ja activa, serveix les guies des d'un checkout amb les dependències de documentació: `.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000`. Si Ajuda no connecta, inicia aquest servidor o consulta els fitxers font. L'enllaç usa el `localhost` del navegador; un navegador remot necessita un servidor local o reenviament configurat.
2. Prem **+ Target**, selecciona una imatge disponible i deixa **No workload** per a la primera captura.
3. Revisa els recursos i la xarxa aïllada predeterminada i prem **Create target**. La sessió es crea automàticament; no hi ha cap botó separat per iniciar-la.
4. Espera que la VM estigui en execució, selecciona-la i captura memòria o disc des dels detalls. Espera que acabi abans de descarregar l’artefacte, el manifest i el checksum.
5. Les evidències es conserven a **Storage**, també després d’**End Session**. El tancament inicia una neteja asíncrona: espera el resultat final i investiga qualsevol fallada.

Per afegir una càrrega vulnerable, consulta primer [Preparar càrregues](../user/PREBAKED_IMAGES.md): en xarxes aïllades els instal·ladors necessiten una memòria cau o imatge preparada compatible; si necessiten Internet, tria explícitament una xarxa amb sortida. L’assistent revisa el formulari i mostra aquest requisit, però no comprova la memòria cau ni la preparació del host.

Després, [analitza la captura exacta](../user/VOLATILITY3.md) i [exporta’n les evidències](../reference/FORENSIC_INTEGRITY.md). Registra la identitat del kernel abans de tancar la VM. Els [paquets portàtils](../user/PORTABLE_LABS.md) continuen desactivats, són experimentals i queden fora dels fluxos compatibles de 1.0.0.

## Adquisició repetible amb Jobs

Obre **Jobs → New Job** des de la barra lateral. Selecciona un target i `immediate-capture` per a captura directa o `lab-session` per a un laboratori manual. Llança el job i espera els artefactes. Jobs també prepara la memòria cau de càrregues. Tanca la sessió Playground ordinària i confirma’n la neteja abans de passar a Jobs.

## Passos següents

- [Execució en local](running-locally.md) cobreix la configuració completa i el runbook.
- [Configuració](configuration.md) explica `astinuslab.toml`, l'entorn del llançador i els catàlegs.
- [Exemples](examples.md) ofereix crides habituals a l'API.
- [Resolució de problemes](troubleshooting.md) llista errors i solucions habituals.
