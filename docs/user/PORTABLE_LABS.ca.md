# Paquets de laboratori portàtils

**Experimental i sense acceptació per a 1.0.0.** Els paquets estan desactivats
per defecte i fora dels fluxos compatibles de la primera versió. Continua
pendent l’acceptació del cicle complet, en dos amfitrions, sense xarxa i de
rendiment (P1–P3). Aquesta pàgina documenta el codi experimental per a proves
controlades, sense aquestes garanties. L’etiqueta **Bloquejat** no certifica
l’acceptació de la versió.

El selector d’intents de verificació permet tornar a consultar intents en execució, fallits i cancel·lats recentment, amb un motiu segur i un ID que es pot copiar. L’historial de verificacions completades mostra el títol, la versió i el termini real d’exportació en UTC. Les certificacions caducades o no disponibles continuen sent llegibles, amb l’exportació desactivada; després de caducar, reconstrueix el laboratori i torna a verificar-lo. Els rebutjos d’exportació apareixen al costat de la fila afectada i conserven les opcions d’inclusió de fitxers i redistribució.

Els paquets estan dissenyats per compartir una experiència de laboratori versionada entre
instal·lacions locals d’AstinusLab. Inclouen topologia, recursos, runbook,
llicències, empremtes SHA-256 i, quan es permet, capes d’arrencada immutables.
No és un servei al núvol.

Només per a proves experimentals controlades, activa `[packages].enabled = true` a
`astinuslab.toml` i obre **Paquets** a la barra lateral.

## Flux del creador

1. Activa els paquets i reinicia l’API/aplicació per registrar les rutes i l’entrada lateral. Construeix i valida el laboratori a Playground; mantén oberta aquella sessió.
2. Obre **Paquets → Crea un paquet**. Introdueix ID, versió, títol, creador i llicència i prem **Carregar des del Playground actual**. Es genera el JSON canònic `LabDefinition` i s’omple l’ID de sessió sense congelar ni canviar el laboratori. Les estimacions de recursos parteixen de les mides declarades de les VMs: revisa-les. Canviar les metadades requereix tornar a carregar; les opcions avançades s’editen al JSON.
3. Afegeix runbook, objectius, pistes, checkpoints i referències de fitxers. Objectius/pistes són matrius JSON de cadenes; els checkpoints són observacions/comprovacions tipades del guest, mai hooks del host. Els fitxers observats són JSON finit sota `/var/lib/astinuslab/observations`, llegit per QEMU Guest Agent amb un límit de 64 KiB. L’autor de la imatge ha d’impedir enllaços simbòlics cap a altres rutes: QGA aplica una arrel lèxica.
4. Prem **Desar esborrany i verificar execució neta** amb la sessió encara oberta. La verificació la congela automàticament, s’executa de manera asíncrona i la neteja en acabar o fallar. No premis **End Session** abans. Segueix el progrés i qualsevol fallada o cancel·lació.
5. Els intents correctes romanen a les verificacions completades després de netejar la sessió o recarregar el navegador. Exporta-hi la verificació escollida: l’ID vincula definició, runbook i capes atestades. La incrustació està desactivada per defecte. Si l’atestació exigeix capes incrustades, activa-la i confirma explícitament els drets de redistribució; altrament es rebutja amb `embedding_required` o `redistribution_unconfirmed`. Mai es canvia silenciosament el contingut verificat.
6. Per compartir només topologia i instruccions, exporta **Sense bloqueig** des d’una sessió Playground activa. Es marca com a no reproduïble i no demostra bytes inicials idèntics.

Cal afegir un runbook no buit abans de verificar. Exporta durant les 24 hores posteriors a la verificació correcta: després caduca l’atestació i l’exportació retorna `attestation_expired`. Conserva l’arxiu descarregat. Per obtenir una altra atestació, reconstrueix el laboratori i verifica’l de nou; el registre completat no recupera la sessió ja netejada.

Una recepta JSON de Playground descriu la topologia visual per importar-la a Playground. El JSON canònic `LabDefinition` és l’entrada del creador amb identitat, recursos i experiència. El fitxer `.astinuslab` és el paquet que rep **Importar / inspeccionar**. No són formats intercanviables: carregar des de Playground en fa la conversió. La inspecció i el renderitzat són passius; no executen el runbook ni el contingut del guest.

## Flux del receptor

**Utilitza un paquet** és la vista inicial. **Obre el paquet** recupera les instruccions desades sense tornar a importar. Les accions corresponen al paquet seleccionat; cada panell d’execució manté el nom del seu propi paquet. **Obre el laboratori a Playground** comprova que la sessió de la instància continua activa. Si la neteja és incompleta, conserva l’ID i consulta **Logs**: els recursos poden continuar reservats; no s’ofereix cap reparació automàtica.

A **Importar / inspeccionar**, selecciona un arxiu `.astinuslab`. La inspecció
és inert: valida límits, camins, signatures i inventari SHA-256 sense crear VM
ni executar contingut. Importa’l només després de revisar identitat, confiança,
llicència, recursos i actius.

Després executa **Prepare** per resoldre bytes incrustats, memòria cau local i,
finalment, fonts fixades permeses explícitament. La xarxa de fonts requereix
opt-in i política local. Executa **Preflight**, revisa emmagatzematge, capacitat,
topologia, reachability i advertiments; confirma cada advertiment i l’egress
abans de **Launch**. El servidor recalcula el preflight.

El llançament retorna una operació persistent. Espera `ready`; **Reset** recrea
les capes escrivibles des de la capa immutable i **Teardown** allibera la
instància sense eliminar contingut referenciat. La supressió i GC esperen que
desapareguin referències i leases.

## Amfitrió i límits

El perfil previst per a l’acceptació és Ubuntu 24.04 LTS sobre x86_64 amb KVM.
Cap amfitrió està acceptat per al flux portàtil complet. Les versions
posteriors d’Ubuntu i altres distribucions Linux no estan verificades; l’ús
experimental no implica compatibilitat ni verificació. Un laboratori petit sol
suggerir 2–4 fils CPU, 4–8 GB de RAM i 40 GB lliures. El flux és local: no hi ha
publicació al núvol ni coordinació multi-host. Una exportació sense bloqueig
només intercanvia topologia i runbook. Cal evidència d’un amfitrió real per
afirmar reproductibilitat.

## API

El mateix cicle és disponible a `/api/v1/lab-packages`: `inspect`,
`imports/{inspection_id}/commit`, llista, `prepare`, `preflight`, `launch`,
`operations/{instance_id}`, `drafts` i `verify-clean-run`. Les mutacions
requereixen CSRF i, si està configurada, autenticació administrativa. Conserva
les claus d’idempotència en reintentar Prepare o Launch.
