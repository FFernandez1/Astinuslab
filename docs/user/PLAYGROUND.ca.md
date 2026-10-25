# Mode Playground

Utilitza Playground per modificar el laboratori de manera interactiva i capturar evidències sota demanda; utilitza [Jobs](../usage/examples.md) per a execucions repetibles de Scenario. Si no hi ha imatges per a un rol, segueix **Gestiona les imatges** o la **Guia d’imatges**. Si falla la càrrega del catàleg, **Torna a carregar les imatges** conserva el formulari i no crea cap laboratori. **Formulari complet — preparació del host no comprovada** només confirma el formulari; la presència d’una imatge no demostra que els fitxers de Workload ni els requisits del host estiguin preparats.

Vegeu també: [Referència de l'API REST de Playground](../api/playground.md).

El Playground és el flux d'entrada principal d'AstinusLab per a labs d'atac i forense. Et permet aixecar targets vulnerables, VMs atacants i Network Nodes, connectar-los mitjançant xarxes multi-segment, executar experiments de seguretat, capturar evidència sota demanda i desmuntar-ho tot quan acabis — tot en una sola sessió.

Els exemples d'API de sota fan servir el helper reutilitzable `api_curl` en Bash documentat a [Exemples](../usage/examples.md), que gestiona l'autenticació Bearer en mode release i la inicialització del token CSRF en peticions de mutació en mode desenvolupament.


Per a la primera prova, utilitza **+ Target → No workload** i captura una VM bàsica. La sessió es crea automàticament. Per a una càrrega vulnerable a la xarxa aïllada predeterminada, prepara abans una memòria cau d’imatge/càrrega coincident a Jobs o utilitza una imatge preparada sense connexió. La instal·lació online necessita una xarxa amb sortida triada explícitament; l’assistent no concedeix Internet ni comprova la memòria cau. Consulta [Preparar càrregues](PREBAKED_IMAGES.md) abans de llançar. Review revisa el formulari, no la preparació del host.

## Què és el Playground?

Pensa en el Playground com un lab efímer on pots:

- **Aixecar targets, atacants i Network Nodes** sense crear primer un Job. Tria imatges del catàleg, etiqueta-les i arrenca-les.
- **Construir xarxes** — aïllades o enrutades, amb CIDRs personalitzats. Fes servir múltiples xarxes per modelar topologies de pivot.
- **Capturar evidència** — bolcats de memòria i disc en qualsevol VM en execució, en qualsevol moment, amb sidecars de manifest i SHA-256.
- **Treballar de manera interactiva** — inspecciona VMs, executa atacs o comprovacions defensives, dispara captures sobre la marxa.
- La neteja és asíncrona; tancar la sessió allibera VMs i xarxes i conserva evidències completades a Storage. Espera l’estat final i resol qualsevol fallada.

### Restriccions clau

- **Capacitat**: diversos Jobs poden conservar targets vius i atacants opcionals dins dels límits agregats de RAM, disc i nombre de VM. L'únic worker de cicle de vida serialitza operacions; no limita el sistema a un únic Job viu. Un Playground ordinari és exclusiu amb Jobs i només admet una sessió activa. Les instàncies experimentals de Packages tenen el seu propi cicle de vida i queden fora de l’abast compatible de 1.0.0. Consulta [Configuració](../usage/configuration.md).
- L’efecte d’un reinici depèn del servei i del propietari de la sessió; consulta la secció de reinicis i neteja.
- **Importació/exportació de receptes i paquets** — la creació guiada de labs a la UI és la porta d'entrada de V1. Les receptes actuals de Playground són JSON (`schema_version: 1`) i es poden exportar des de la sessió activa o importar a una sessió. Els paquets d'escenari embolcallen aquesta recepta YAML, metadades i scripts opcionals en un zip per compartir fàcilment. Els fluxos YAML escrits a mà continuen sent el camí de repetibilitat a llarg termini.

---

## Iniciar una sessió de Playground

1. Obre **Playground** des de la barra lateral.
2. Utilitza **+ Target**, **+ Attacker**, **+ Network Node** o importa una recepta.
3. La primera acció crea o reutilitza automàticament la sessió; la UI no té un botó Start Session.

El llenç mostra la topologia. En seleccionar una VM, el panell de detalls mostra estat, IPs, NICs, SSH, serveis i controls de captura. El peu mostra la sessió i **End Session** quan existeix. Els artefactes es conserven a **Storage**. L’API permet crear explícitament una sessió buida amb `POST /api/v1/playground/sessions`.

---

## Crear xarxes

Les xarxes defineixen la topologia de connectivitat de les teves VMs. Cada xarxa té un nom, un rang CIDR i un flag d'aïllament.

### Xarxes aïllades vs. enrutades

- **Aïllada** (`isolated=true`) — aquesta xarxa libvirt no reenvia trànsit a xarxes externes. Els convidats es poden comunicar entre si i amb l'amfitrió mitjançant l'adreça del pont, segons les regles del tallafoc. Això no aïlla els serveis de l'amfitrió: restringeix els ports d'escolta per separat.
- **Enrutada** (`isolated=false`) — les VMs d'aquesta xarxa poden arribar a l'host i a altres xarxes enrutades via NAT/enrutament. Fes-la servir per a connectivitat sortint o per pontejar segments.

### Crear una xarxa

1. Obre **+ Target**, **+ Attacker** o **+ Network Node** a la capçalera. Al pas de xarxes de l'assistent, prem **+ Nova** (**+ New** en anglès) per obrir **Afegeix una xarxa a la VM**.
2. Introdueix un nom (p. ex., `internal`, `dmz`, `guest`) o accepta el suggerit.
3. Introdueix un **CIDR** o accepta el següent rang lliure `10.42.x.0/24` suggerit. Els CIDR d'una sessió no es poden solapar; una petició amb solapament se substitueix per un rang lliure i mostra un avís.
4. Mantén marcada l'opció d'aïllament per desactivar el reenviament extern; desmarca-la per a connectivitat encaminada (NAT).
5. Confirma la xarxa i completa l'assistent per crear la VM. La xarxa passa de `pending` a `active` durant la creació del laboratori; afegir-la al formulari encara no inicia recursos.

---

## Desplegar VMs

Una VM s'arrenca des d'una imatge del catàleg i es connecta a una o més xarxes via NICs (interfícies de xarxa). Targets, atacants i Network Nodes fan servir tots aquest flux de VM.

### Network Nodes

Els Network Nodes són VMs lleugeres amb diverses NIC per a una imatge que ja proporcioni funcions d'encaminador, tallafoc, proxy, VPN o IDS. AstinusLab connecta les NIC i aplica els recursos predeterminats (1 vCPU, 512 MB de RAM, 4 GB de disc); la configuració del reenviament, les regles i els serveis de xarxa correspon a l'operador. Connecta dues o més NIC quan vulguis situar el node entre segments.

Les captures estan desactivades per defecte per als Network Nodes; captura les VMs target quan necessitis evidència forense.

### Desplegar una VM

1. A la capçalera, prem **+ Target**, **+ Attacker** o **+ Network Node**.
2. Introdueix una etiqueta única dins de la sessió i tria una imatge de SO del catàleg. Només es mostren imatges aptes per a Playground.
3. Per a un Target, tria opcionalment un Workload compatible. Revisa els seus requisits de preparació abans de continuar.
4. Selecciona les xarxes: cadascuna afegeix una NIC. Per a un pivot o un Network Node entre segments, selecciona'n dues o més. Sense selecció, es crea i s'utilitza una xarxa aïllada `default` (`10.42.1.0/24`). Els Targets permeten ajustar vCPU, memòria i disc; els altres rols apliquen els seus valors predeterminats.
5. Revisa el formulari i confirma la creació. La primera acció de laboratori crea o reutilitza automàticament la sessió; obrir o completar el formulari no comprova la preparació del host.

AstinusLab arrenca la VM i actualitza les IP a mesura que estan disponibles. Selecciona la màquina per obrir el panell de detalls, que en estat `running` mostra:

- **Label** i **Image**
- **State** (`pending`, `booting`, `running`, `destroying`, `failed`)
- **IPs per NIC** — la IP del guest assignada a cada interfície de xarxa
- **Botons d'acció** — reiniciar, apagar, arrencar, destruir la VM, **Capturar RAM** i **Capturar disc**

---

## Accedir a les VMs via SSH

Fins que l'accés a la consola VNC/SPICE estigui disponible en una versió futura, has d'interactuar amb les VMs via SSH. AstinusLab injecta automàticament una clau SSH a les VMs que inicia.

Per accedir per SSH a una VM en una xarxa **encaminada o aïllada** des de la teva màquina amfitriona, utilitza la clau gestionada de l'espai de treball i la IP del convidat que es mostra a la targeta de la VM:

Usa l'ordre SSH mostrada per a aquella VM i la seva IP/usuari. El nom de la clau seleccionada es desa a `<storage_dir>/ssh_keys/.active`; el fitxer privat és `<storage_dir>/ssh_keys/<nom-actiu>`, sense sufix. El nom inicial del llançador és `astinuslab-default`, però pot canviar a **Security**. Resol `storage_dir` des de la configuració: en una instal·lació sol dependre de l'arrel de dades, no de `current`. Una VM existent pot conservar la clau injectada en crear-la encara que canviï la selecció; conserva aquella clau. Amb identitats de servei separades, usa l'accés autoritzat i no copiïs claus privades a ubicacions públiques.
*(Consulta la pàgina del catàleg d'Imatges si no estàs segur del `<username>` predeterminat per a una imatge específica.)*

**Xarxes aïllades:** l'amfitrió té una adreça al pont libvirt. Pots utilitzar la IP
i la clau indicades per a SSH directe si SSH i els tallafocs ho permeten. Una VM
pivot és opcional per accedir des d'un altre segment. Els convidats també poden
arribar als serveis de l'amfitrió; desactivar el reenviament no substitueix el tallafoc.
Consulta la [semàntica de libvirt](https://libvirt.org/formatnetwork.html#isolated-network-config).

---

## Veure la topologia

El Playground mostra sempre el **mapa del laboratori**, amb VMs, xarxes i les seves connexions. Arrossega els nodes per organitzar-lo; la disposició es desa automàticament. Selecciona una màquina per obrir-ne el panell de detalls. Utilitza el mapa per revisar les connexions entre segments abans de capturar.

---

## Importar i exportar receptes i paquets

Playground admet dos formats per compartir:

- **Recepta JSON** — `PlaygroundRecipe` sense embolcallar des de la sessió activa.
- **Zip de paquet d'escenari** — arxiu compartible amb `metadata.yaml`, `scenario.yaml` i entrades opcionals de scripts relatives i segures.

### Exportar una recepta

1. Inicia o obre una sessió de Playground.
2. Construeix xarxes, desplega VMs target, atacant i Network Node, i organitza la topologia.
3. Fes clic a **Export Recipe**.
4. AstinusLab descarrega `<session-id>.recipe.json`.

Les receptes exportades inclouen:

- `schema_version: 1`
- `name`
- `networks` amb noms, CIDRs i flags d'aïllament
- `vms` amb etiquetes, rols, IDs d'imatge, IDs de workload opcionals, `vcpus`, `memory_mb`, `disk_gb` opcionals i noms de xarxa
- les posicions de nodes de `topology`

### Importar una recepta

1. Fes clic a **Import Recipe**.
2. Enganxa el JSON de la recepta.
3. Envia la importació.

AstinusLab valida noms de xarxa i etiquetes de VM únics, rebutja referències a xarxes desconegudes, crea les xarxes que falten, desplega VMs i restaura les posicions de topologia. La importació a una sessió existent falla amb `409 Conflict` si els noms de la recepta col·lideixen amb xarxes o VMs existents.

### Exportar un paquet d'escenari

Fes servir l'API REST per descarregar el Playground actiu com a paquet compartible d'un clic:

```bash
api_curl -OJ "$BASE_URL/api/v1/scenario-packages/playground/current"
```

Contingut del paquet:

- `metadata.yaml` — tipus de paquet, versió d'esquema i metadades visibles.
- `scenario.yaml` — dades de la recepta de Playground.
- Rutes de script opcionals — entrades relatives segures incloses pels helpers de paquet del backend quan futurs fluxos de catàleg/workload les referenciïn.

### Importar un paquet d'escenari amb recursos ajustats

Puja el zip a una altra instància d'AstinusLab:

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip"
```

Per redimensionar màquines sense editar l'arxiu, passa `resource_overrides` com a JSON indexat per etiqueta de VM:

```bash
api_curl -X POST "$BASE_URL/api/v1/scenario-packages/import" \
  -F "file=@dvwa-lab.zip" \
  -F 'resource_overrides={"target-1":{"vcpus":4,"memory_mb":8192,"disk_gb":80}}'
```

Els camps permesos són `vcpus`, `memory_mb` i `disk_gb`. La importació valida les sobreescriptures amb el mateix esquema de recepta de Playground abans de crear xarxes o desplegar VMs.

---

## Capturar artefactes forenses

Pots disparar captures de memòria i disc en VMs target i atacant en execució. Les captures estan desactivades per defecte per als Network Nodes; captura les VMs target quan necessitis evidència forense.

### Captura de memòria

1. Selecciona la VM al mapa per obrir-ne el panell de detalls.
2. Prem **Capturar RAM** (**Capture RAM** en anglès).
3. AstinusLab adquireix la RAM del guest via `virDomainCoreDump` (format ELF, sense modificació del guest).
4. El progrés apareix a la consola de captura. En acabar, l'evidència apareix al panell de detalls i a **Storage**, amb enllaços a l'artefacte, el manifest i el SHA-256.

### Captura de disc

1. Selecciona la VM al mapa per obrir-ne el panell de detalls.
2. Prem **Capturar disc** (**Capture Disk** en anglès).
3. AstinusLab pausa el domini breument, converteix el disc en un artefacte qcow2 sense compressió i reprèn la VM.
4. L'artefacte apareix al **Storage**.

### Accedir als artefactes capturats

Totes les captures de la sessió apareixen al **Storage** amb enllaços de descàrrega i metadades (tipus, mida, marca de temps). Fes clic a l'enllaç per descarregar directament.

### Cicle de vida de l'artefacte

Els artefactes capturats són evidència forense i es tracten de manera separada de la sessió efímera que els va produir:

- **Les captures persisteixen entre reinicis del worker.** Quan el worker de Celery es reinicia, el escombrat de recuperació d'arrencada només purga els overlays de disc de VM obsolets (sota `playground_disk_dir`) de sessions que ja no estan actives. El directori de captures (`playground_capture_dir`) mai no el toca la recuperació, així que els bolcats de memòria, imatges de disc, manifestos i sidecars SHA-256 completats romanen intactes.
- **Les captures persisteixen després de la destrucció de la sessió.** Finalitzar la sessió de playground destrueix les seves VMs i xarxes, però els artefactes ja produïts per `Capture Memory` / `Capture Disk` queden al disc i les seves files `Acquisition` queden a la base de dades. Continuen disponibles a través d**Storage** i l'explorador d'emmagatzematge fins que els eliminis explícitament.
- **Cadena de custòdia.** Cada captura s'escriu amb un manifest i un fitxer SHA-256 al costat; aquests s'escriuen un cop en el moment de la captura i mai no els modifiquen els camins de recuperació, neteja o fi de sessió.

---

## Control d'energia de la VM

El panell de detalls de la VM té botons d'acció. Espera que acabi una transició d'estat abans de repetir una operació:

- **Reboot** — reinici ordenat via ACPI. La VM continua arrencada i connectada a les seves xarxes.
- **Shutdown** — apagada ordenada. La VM s'atura però el registre de la VM roman. Fes clic a **Start** per tornar-la a aixecar.
- **Start** — arrenca una VM que estava apagada (només disponible quan la VM no està en execució).
- **Destroy** — apagada ordenada → destrucció forçada. La VM s'elimina, el seu disc s'esborra i el registre de la VM es marca com a destruït.

---

## Aturar la sessió

Quan acabis:

1. Prem **Finalitza la sessió** (**End Session** en anglès) al peu de la sessió i confirma la sol·licitud.
2. **Neteja sol·licitada** (`destroy_requested`) confirma l'acceptació; **Netejant…** (`destroying`) indica que continua en curs. Espera **Sessió finalitzada. Es conserven les evidències capturades.** (`cancelled` després de Finalitza la sessió, o `completed`).
3. Si apareix **Neteja incompleta** (`destroy_failed` o `failed`) o l'estat no està disponible, consulta Logs amb l'ID de sessió. Els recursos del host poden continuar reservats; no donis la neteja per acabada. Les evidències completades es conserven a Storage.

Tots els artefactes capturats continuen disponibles al **Storage** fins que els eliminis manualment des de l'Emmagatzematge.

---

## Restriccions i regles de validació

- **Sessió activa única** — no pots iniciar un nou playground si ja n'hi ha un en execució. Primer has de destruir la sessió actual.
- **Unicitat d'etiquetes** — les etiquetes de VM han de ser úniques dins d'una sessió.
- **Solapament de CIDR** — els CIDRs de xarxa dins d'una sessió no s'han de solapar.
- **Xarxa en ús** — no pots eliminar una xarxa mentre hi hagi VMs encara connectades. Destrueix primer les VMs, o passa un flag de cascada (només API).
- **Estat de captura** — només pots disparar captures en VMs target i atacant en estat `running`. Les captures estan desactivades per defecte per als Network Nodes.
- **Sense execució de jobs solapada** — si hi ha un Job forense actiu, no pots iniciar un playground, i viceversa.

---

## Model de seguretat

V1 és localhost/local-de-confiança per defecte. Si vincules l'API/UI fora de loopback, habilita l'autenticació d'API amb un únic admin i usa igualment TLS, VPN, firewall o reverse proxy per a xarxes compartides.

Les VMs guest són intencionadament hostils o vulnerables. L'enduriment de guest hostil s'hauria de seleccionar durant la creació del lab quan estigui disponible; fins que aquests controls estiguin complets, prefereix xarxes aïllades per a labs d'exploit i tracta les credencials del guest, les URLs de servei i els artefactes capturats com a sensibles.

---

## Limitacions conegudes (diferides)

- **Accés a consola** — el proxy de consola VNC/SPICE està planificat per a una versió futura. Fins llavors, fes servir la IP del guest per fer SSH directament des de la teva màquina (veure [Accedir a les VMs via SSH](#accedir-a-les-vms-via-ssh)).
- **Snapshots i restauració** — el playground no suporta checkpoints de desar-estat / restaurar.
- **Controls de recursos** — l'API accepta `vcpus`, `memory_mb` i `disk_gb` per VM. L'assistent de Target permet ajustar vCPU, memòria i disc; els creadors d'Attacker i Network Node apliquen els valors predeterminats del seu rol.
- **Imatges pujades per l'usuari** — no pots pujar fitxers qcow2 crus fora del catàleg per a ús al playground.
- **Targets Windows** — roadmap; V1 se centra en labs vulnerables d'aplicacions web Linux.
- **Telemetria en viu i experiències d'informes més completes**, més enllà dels resums de Volatility i els informes de plugins ja publicats — roadmap; V1 se centra en captures, artefactes i sortida de Volatility 3.

---

## Fluxos de treball d'exemple

### Bolcat de memòria d'una sola VM

1. Inicia una sessió.
2. Desplega una VM des de la imatge que triïs.
3. Espera que arribi a l'estat `running`.
4. Prem **Capturar RAM**.
5. Descarrega el bolcat des d**Storage**.
6. Destrueix la sessió.

### Topologia de pivot multi-segment

1. Inicia una sessió.
2. Crea tres xarxes: `dmz` (10.0.0.0/24, enrutada), `internal` (10.1.0.0/24, aïllada), `sensitive` (10.2.0.0/24, aïllada).
3. Desplega una VM `attacker` només a `dmz`.
4. Desplega una VM `pivot` a `dmz` i `internal`.
5. Desplega una VM `target` a `internal` i `sensitive`.
6. L'atacant pot arribar al pivot via `dmz`; el pivot veu tots dos segments; el target està darrere del pivot.
7. Dispara captures en qualsevol VM per obtenir snapshots forenses en qualsevol punt.
8. Destrueix la sessió quan acabis.

### Explotar i capturar

1. Inicia una sessió.
2. Crea una xarxa enrutada i desplega una VM vulnerable.
3. Des de la teva màquina host, fes SSH a la IP del guest i executa un exploit.
4. Un cop l'estat de l'exploit està capturat en memòria, prem **Capturar RAM** des de la UI.
5. Analitza el bolcat amb Volatility 3 o les teves eines preferides; Volatility 3 és el camí central d'anàlisi de memòria de V1.
6. Destrueix la sessió.

---

## Resolució de problemes

### La VM no arrenca o esgota el temps

- Comprova que la imatge és vàlida i té un nom d'usuari per defecte configurat (visible a la pàgina d'Imatges).
- Assegura't que la connectivitat SSH no estigui bloquejada per regles de tallafocs.
- Revisa els logs del runner per errors de sondeig d'IP del guest.

### La creació de xarxa falla amb error de solapament de CIDR

- Revisa els CIDRs de les xarxes existents. AstinusLab no permet rangs solapats dins d'una sessió.
- Tria un CIDR disjunt i reintenta.

### La captura es penja o falla

- Assegura't que la VM estigui en estat `running` (revisa la targeta de la VM).
- Les captures de memòria poden trigar diversos minuts en imatges grans. Revisa els logs del runner per veure el progrés.
- Les captures de disc pausen la VM breument (~1s per a discos petits, més per a grans). Dona temps perquè la pausa i la conversió es completin.

### No es pot eliminar una xarxa

- Verifica que no hi hagi VMs encara connectades a la xarxa. Destrueix o reconnecta primer totes les VMs dependents.
- Revisa el missatge d'error a la UI per veure la llista de VMs que bloquegen l'eliminació.

### Reinicis i neteja

Reiniciar només l’API o el worker d’anàlisi no executa la neteja d’inici de Playground. Reiniciar el runner de cicle de vida invoca la recuperació i acaba les sessions Playground ordinàries. Les sessions congelades del creador i les sessions de paquets actius estan protegides pel seu cicle de verificació/instància. Reiniciar tot el host també reinicia aquells serveis i interromp els guests; protegir el registre no garanteix execució ininterrompuda de la VM.

**End Session** encua la neteja. La pàgina segueix aquesta sessió després de recarregar i només confirma el tancament amb `cancelled` (el resultat d’End Session) o `completed`. `destroy_failed` o `failed` requereix atenció; els recursos poden continuar reservats. Consulta **Logs** amb l’ID de sessió conservat. Un registre absent o un estat no disponible no confirma la neteja. Les captures completades i els sidecars romanen a **Storage**.


---

## Passos següents

- **Analitza amb Volatility 3** — un cop tinguis un bolcat de memòria, fes servir [Volatility](VOLATILITY3.md) per analitzar la memòria del guest amb els teus plugins preferits.
- **Aprèn el flux de Jobs** — per a adquisicions repetibles basades en escenaris, vegeu [Primers passos](../usage/getting-started.md).
- **Configura xarxes personalitzades** — el playground fa servir xarxes libvirt estàndard. Els usuaris avançats poden inspeccionar l'XML de xarxa amb `virsh net-dumpxml` o modificar les definicions del catàleg per a topologies complexes.
