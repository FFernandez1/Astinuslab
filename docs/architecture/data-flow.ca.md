# Flux de dades

## Flux d'un Job forense

```text
1. Operator -> FastAPI: POST /api/v1/jobs
2. FastAPI -> SQLite: persist queued Job
3. FastAPI -> Celery broker: enqueue task
4. Lifecycle runner -> SQLite: preparing_image
5. Lifecycle runner -> libvirt: overlay, seed, domain
6. Lifecycle runner -> guest: wait for IP/SSH; run scenario/workload
7. Lifecycle runner -> libvirt: capture memory and/or disk
8. Lifecycle runner -> storage: artifact + manifest + SHA-256
9. Lifecycle runner -> SQLite: completed / failed / cancelled
10. Operator -> FastAPI: download published evidence
```

Tots els canvis d'estat d'un job han de passar per la lògica de transició del repository. Els estats terminals són `completed`, `failed` i `cancelled`.

## Flux de lab manual

Els jobs de lab manual fan servir escenaris `manual_trigger`:

1. El runner arrenca la VM target.
2. El workload seleccionat s'instal·la i se'n comprova la salut.
3. Es registren metadades opcionals de runtime de la VM atacant.
4. El job espera a `waiting_for_capture`.
5. L'operador crida `/api/v1/jobs/{job_id}/capture` o fa servir la UI.
6. El runner captura memòria, disc o tots dos.

## Flux del Playground

El Playground fa servir el mateix host runner però una superfície d'API i un model d'estat diferents:

1. Crear o reutilitzar una sessió de playground activa.
2. Crear xarxes de sessió basades en libvirt.
3. Desplegar VMs des d'imatges del catàleg.
4. Connectar o desconnectar NICs segons calgui.
5. Disparar captures de memòria o disc a nivell de VM.
6. Destruir la sessió.

Els overlays de VM del playground són efímers. Els artefactes de captura del playground persisteixen fins que s'eliminen explícitament.

## Flux d'artefactes

La captura de memòria fa servir la funcionalitat de core dump de libvirt des de l'host. La captura de disc converteix l'estat del disc de la VM en un artefacte descarregable. Per als artefactes completats, AstinusLab emmagatzema metadades de ruta, nom de fitxer, mida i SHA-256, i serveix els fitxers a través dels endpoints d'artefactes amb capçaleres de checksum quan estan disponibles.

## Flux de Volatility 3

Per a l'anàlisi de memòria de Linux, AstinusLab identifica el kernel del guest i prova els proveïdors d'ISF en ordre:

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

Si la cadena de l'host falla, AstinusLab prova el fallback del guest en viu, que pot fer servir un `vmlinux` coincident amb DWARF, debuginfod o una instal·lació de paquets de depuració permesa explícitament. `dwarf2json` no admet una entrada que només contingui BTF i aquest fallback no la fa servir mai. Els ISFs reutilitzables s'emmagatzemen a `vol3/symbols/linux/`. La sortida de Volatility per job s'emmagatzema a `storage/dumps/<job_id>/vol3/`.
