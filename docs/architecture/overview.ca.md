# Visió general de l'arquitectura

AstinusLab és un sistema d'orquestració de labs local. L'API i la UI accepten peticions de l'operador, persisteixen l'estat dels jobs a SQLite, encuen feina de llarga durada mitjançant Celery, i un host runner controla libvirt, SSH, captura de disc, captura de memòria i publicació d'artefactes.

```text
Browser / API clients
        |
        v
FastAPI -----> SQLite application state
        |
        v
Celery SQLite broker
        |                           |
        v                           v
Lifecycle runner + Beat       Analysis worker
(concurrency 1, threads)       (analysis queue, concurrency 1)
        |                           |
        +--> libvirt / QEMU / KVM    +--> Volatility plugins
        +--> guest setup via SSH
        +--> capture / ISF work
        +--> artifacts and logs <---+--> reports
```

## Límits

| Límit | Responsabilitat |
| --- | --- |
| App FastAPI | API REST, UI HTML, validació de peticions, creació de jobs, descàrregues d'artefactes. |
| Base de dades SQLite | Registres de jobs, transicions, esdeveniments, accions d'host, runtimes, adquisicions, estat del playground. |
| Worker de Celery | Executa accions d'orquestració privilegiades i de llarga durada fora del flux petició/resposta. |
| Scripts auxiliars d'host | Realitzen operacions root restringides mitjançant entrades d'allow-list de sudoers instal·lades. |
| libvirt/QEMU | Cicle de vida de VMs, xarxes, overlays de disc, bolcats de memòria i conversió de disc. |
| Catàlegs | Definicions declaratives de target, escenari, workload i VM atacant. |
| Emmagatzematge | Artefactes capturats, logs, fitxers temporals, bases de dades de Celery, claus SSH, memòria cau de workloads. |

## Modes d'operació

El mode de desenvolupament s'executa des del repositori mitjançant `./astinuslab.sh`. El mode release copia l'app a `/opt/astinuslab`, crea un virtualenv, instal·la els scripts auxiliars i les unitats systemd, i executa els serveis com a `astinuslab-api@USER` i `astinuslab-runner@USER`.

Els dos modes no s'han d'executar junts perquè tots dos runners poden consumir la mateixa cua i controlar els mateixos recursos de libvirt de l'host.

## Model de seguretat

AstinusLab compta amb autenticació opcional d'API amb rol únic d'administrador. Per defecte continua pensat per a laboratoris locals de confiança. El mode de desenvolupament s'associa a loopback tret que se substitueixi l'adreça d'enllaç; els enllaços fora de loopback requereixen autenticació d'API o una excepció insegura explícita. Per a xarxes compartides, situeu-lo darrere de TLS, un servidor intermediari invers, VPN i restriccions de tallafoc. L'inici de sessió al navegador (`/login`) emet una galeta de sessió independent amb hash al servidor.
