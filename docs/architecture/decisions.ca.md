# Decisions

Aquesta pàgina registra les decisions arquitectòniques actuals que afecten la configuració, les operacions i el manteniment.

## Usar adquisició del costat de l'host

Els artefactes de memòria i disc es capturen des de l'host mitjançant libvirt i eines QEMU. S'eviten agents del costat guest per integritat d'adquisició i compatibilitat de distribucions.

## Mantenir un únic mode de runner actiu

Els runners de desenvolupament i release no s'han d'executar al mateix temps. Tots dos poden consumir tasques de Celery i operar sobre recursos libvirt, de manera que el mode mixt pot enviar jobs al runner equivocat o col·lidir en noms de VM, xarxes i emmagatzematge.

## Usar SQLite per a l'estat local

El projecte fa servir SQLite per a la base de dades de l'aplicació, el broker de Celery i el backend de resultats de Celery. La concurrència suportada del runner és `1`, alineada amb el disseny local d'un sol host i evitant carreres d'estat compartit.

## Usar helpers privilegiats instal·lats

El runner no rep sudo sense contrasenya ampli. Pot cridar scripts helper instal·lats específics sota `/usr/local/libexec/astinuslab/` mitjançant `/etc/sudoers.d/astinuslab-runner`.

## Tractar els catàlegs com a dades d'origen mantingudes per l'usuari

Targets, scenarios, workloads i attackers es declaren en catàlegs YAML. El descobriment automàtic d'imatges pot afegir targets, però els mantenidors han de verificar usuaris, família de distro, perfil cloud-init, variant d'OS i comportament de xarxa.

## Fer el Playground efímer, mantenir les captures duradores

Les sessions, VMs, xarxes i overlays de disc de Playground són temporals. Les captures, manifestos, checksums i files d'adquisició persisteixen després de destruir la sessió.

## Generació d'ISF basada en VM

Les VMs fresques són la font de veritat per a la identitat del kernel Linux i la generació d'ISF de Volatility 3. Vegeu [Generació d'ISF basada en VM](adr-vm-based-isf-generation.md).

## La capacitat de VM és control d'admissió, no concurrència de workers (2026-07)

El límit configurable de VMs executant-se simultàniament (`orchestration.max_concurrent_vms`) s'aplica a la capa de producte comptant files `job_vms` en estats que consumeixen capacitat dins de la transacció d'admissió (`BEGIN IMMEDIATE`). Els workers de Celery romanen en `--concurrency=1`: el broker SQLite, els locks de captura en procés, la serialització de tasques beat i la reconciliació d'un sol host assumeixen un runner serial, i aquesta invariant està fixada per `tests/python/test_deploy_config.py`.

El valor predeterminat és sense límit, preservant el comportament existent. Els estats transitoris de VM com `destroying` i `teardown_failed` compten per al límit fins que la reconciliació els resol.

## Usar límits de paquet, entorn d'execució i runtime per a l'estructura del repositori (2026-08)

L'objectiu per etapes usa `src/astinuslab` per a l'aplicació instal·lable i els recursos web i de migració que pertanyen al paquet, una jerarquia canònica `tests`, `ops` per al codi d'host i desplegament, i `tools` per a utilitats de contribució i release. Manté `guest`, `config`, `images`, `vol3` i `storage` com a límits visibles de primer nivell perquè moure'ls sota contenidors genèrics afegiria risc de rutes sense aclarir la propietat.

Les rutes del repositori són interfícies de migració: cada moviment requereix una matriu de productors i consumidors, verificació i rollback. Els imports interns i les rutes de codi es poden trencar dins d'una etapa atòmica, mentre que les rutes HTTP, noms de tasques Celery, IDs de revisió Alembic, noms d'unitats systemd, claus de configuració, formats d'artefactes i catàlegs, i URLs públiques estàtiques es mantenen com a contractes estables.

Vegeu la [política d'ubicació del repositori](repository-placement-policy.md), l'[estructura objectiu](repository-target-layout.md) i la [matriu de migració de rutes](path-migration-matrix.md). Aquests cinc registres interns d'estructura són canònics en anglès i estan fora de la navegació publicada de MkDocs.
