# Hotspots del codi i pressupostos de mida

Aquesta pàgina llista fitxers i funcions que superen els pressupostos de mida
tous. L'objectiu és la visibilitat, no una política dura: els fitxers grans es
toleren quan la responsabilitat és genuïnament única, però el creixement
il·limitat hauria de disparar una extracció enfocada abans del següent canvi no
trivial.

Quan toquis qualsevol element d'aquesta llista i el canvi afegeixi més de ~20
línies netes, prefereix un petit pas d'extracció que baixi el compte.

Els números següents estan verificats per `tests/unit/test_hotspot_budgets.py`,
de manera que no poden desviar-se: si un element llistat creix, la suite falla
fins que es reescriu la columna de LOC, i un fitxer o funció que superi un
llindar de descoberta falla fins que s'afegeix a la taula. Els sostres tous
**no** s'apliquen -- diverses entrades estan per sobre del seu sostre per
decisió deliberada, i les notes expliquen per què.

El que sí que s'imposa és la direcció del canvi. La mateixa suite registra, per
taula, quantes entrades superen el seu sostre, per quantes línies en total i
quant sumen els sostres. Les tres xifres es redeclaren exactament, i la regla
per redeclarar-les és que només poden baixar. Apujar un sostre perquè una
entrada compleixi no funciona: la suma dels sostres canvia i la suite avisa que
la taula ha empitjorat. La complexitat ciclomàtica té el seu propi trinquet a
`pyproject.toml` (`lint.mccabe.max-complexity`), fixat a la pitjor funció de
l'arbre perquè res de nou pugui ser pitjor del que ja és el pitjor.

## Com es mesuren els números

- **LOC de fitxer** és el compte de línies físiques, tal com el reporta `wc -l`.
- **LOC de funció** va de la línia `def` fins a l'última línia del cos. Els
  decoradors queden exclosos, així que afegir-ne un no mou el número.
- L'escaneig de funcions cobreix funcions de nivell de mòdul i mètodes de
  classes de nivell de mòdul. Els closures niats dins d'una funció ja es compten
  dins de la seva entrada contenidora.
- **LOC de fitxer de shell** és el compte de línies físiques d'un `*.sh`
  seguit per Git.
- **Python incrustat en shell** és el nombre de línies dins dels cossos
  heredoc `python3 - ... <<'PY'` d'aquest script, trobats amb el mateix
  helper de descoberta que fan servir els gates de lint, de manera que mai
  no poden discrepar sobre què compta com a Python incrustat.

## Llindars de descoberta

Qualsevol cosa que arribi o superi aquestes mides **ha** d'aparèixer a sota.
Les dues primeres s'apliquen a `src/astinuslab`; les dues últimes s'apliquen a
cada `*.sh` seguit per Git:

- Fitxers: **900** línies.
- Funcions: **180** línies.
- Fitxers de shell: **700** línies.
- Python incrustat en shell: **250** línies.

Són més laxos que els sostres per entrada a propòsit. Un sostre diu on hauria
d'acabar un mòdul; un llindar diu el punt a partir del qual quedar-se fora
d'aquesta pàgina deixa de ser una opció. Llistar-hi quelcom més petit sempre
està permès, i unes quantes entrades es fan servir per seguiment proactiu tot i
estar encara per sota del seu sostre.

## Gates de baseline

Darrera mesura completa 2026-09-05, a `feature/portable-lab-sharing`:

- `.venv/bin/python -m ruff check .`: passant.
- `.venv/bin/python -m ruff format --check .`: passant, 499 fitxers.
- `.venv/bin/python -m mypy src tests/python`: passant amb mypy 2.1.0, 455 fitxers font.
- `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q`:
  passant, 4005 passed, 18 skipped, cobertura total 86.02%.
- `.venv/bin/python -m pytest tests/python/tooling -q`: passant, 933 passed,
  2 skipped. Tornat a comprovar el 2026-09-06 després de moure el disseny durable
  de portable-lab-sharing a `docs/plans/`, deixant `docs/superpowers/` local a les
  eines i sense versionar.

Els fitxers Python de backend més grans continuen sent els mòduls de flux
d'orquestració / Volatility 3. Torna a executar els gates de qualitat després de
tocar qualsevol mòdul llistat; el gate de cobertura actiu és el 86% de
línies *i branques*.

## Fitxers llargs

| Fitxer | LOC actual | Sostre tou | Notes |
|------|-------------|--------------|-------|
| `src/astinuslab/i18n_data/en.py` | 975 | 1100 | Dades d’idioma amb claus i marcadors equivalents en les quatre llengües. Conserva la terminologia i les comprovacions de paritat en dividir. |
| `src/astinuslab/i18n_data/es.py` | 975 | 1100 | Dades d’idioma amb claus i marcadors equivalents en les quatre llengües. Conserva la terminologia i les comprovacions de paritat en dividir. |
| `src/astinuslab/i18n_data/fr.py` | 975 | 1100 | Dades d’idioma amb claus i marcadors equivalents en les quatre llengües. Conserva la terminologia i les comprovacions de paritat en dividir. |
| `src/astinuslab/i18n_data/ca.py` | 975 | 1100 | Dades d’idioma amb claus i marcadors equivalents en les quatre llengües. Conserva la terminologia i les comprovacions de paritat en dividir. |
| `src/astinuslab/lab_packages/package_service.py` | 1827 | 1000 | Façana del cicle de vida dels paquets portables: importació, preparació, llançament, reinici, desmuntatge, esborrat i reconciliació. El mòdul nou més gran de la pàgina. El camí de reconciliació i la màquina d'estats per acció són les dues peces cohesives que convé extreure primer. |
| `src/astinuslab/lab_packages/prepare_service.py` | 963 | 900 | Admissió de Prepare, leases de retenció, reclamacions d'execució i propietat dels actius verificats. R33 separa la reutilització immutable compartida de la mutació exclusiva; conserva l'ordre dels bloquejos i les proves de recuperació en extreure'n parts. |
| `src/astinuslab/lab_packages/verification_service.py` | 1463 | 1000 | Verificació d'arrencada neta del costat del creador: cicle de vida de la sessió, tancament de workers, avaluació de checkpoints i cancel·lació. El tancament i l'avaluació són separables; mantén la comptabilitat de leases en un sol lloc en dividir. |
| `src/astinuslab/lab_packages/archive.py` | 1270 | 1000 | Lector i escriptor de `.astinuslab` més les comprovacions de seguretat de l'arxiu: travessia de rutes, enllaços, rutes duplicades o amb col·lisió de majúscules, entrades no declarades i bombes de compressió. Aquí la longitud és la llista d'amenaces; extreu l'escriptor abans que els validadors. |
| `src/astinuslab/lab_packages/api.py` | 1523 | 900 | Rutes FastAPI del flux de paquets. Cossos de ruta plans, així que divideix per etapa del flux -- inspecció/importació, preparació, llançament, verificació -- a mesura que creixi. |
| `src/astinuslab/forensics/vol3_support.py` | 1207 | 1000 | Construcció de símbols Volatility 3 / ISF al paquet `forensics/`. La cadena de construcció de símbols està fortament acoblada a l'estat del proveïdor; extreu només amb cobertura dirigida. |
| `src/astinuslab/playground/repositories.py` | 1269 | 900 | Persistència del Playground: cicle de vida de files de VM, xarxa i NIC, més els helpers de reclamació compare-and-swap. Majoritàriament funcions de consulta planes, així que el creixement és barat de dividir per entitat. |
| `src/astinuslab/db/models.py` | 1163 | 1200 | Definicions declaratives de taules de tot l'esquema; ha crescut amb les taules de paquet, instància i verificació. El creixement és una classe per taula i no lògica enredada, així que el sostre generós és deliberat. Divideix per domini només si el fitxer deixa de llegir-se de dalt a baix. |
| `src/astinuslab/catalog.py` | 1096 | 900 | Càrrega de catàleg, selectors de compatibilitat i inferència de descoberta d'imatges. Mantén les addicions estretes o extreu helpers cohesius de parseig/normalització. |
| `src/astinuslab/jobs/package_instance_coordinator.py` | 1518 | 900 | Coordinador del costat de Celery per a les accions d'instància de paquet: reserva, arrencada bloquejada, assignació d'overlays i neteja de reclamacions obsoletes. La neteja i la reserva prenen locks; extreu-les juntes o no les extreguis. |
| `src/astinuslab/storage/publication.py` | 1070 | 900 | Publicació d'artefactes, hashing i escriptura de manifests. Divideix-ho per tipus d'artefacte si torna a créixer. |
| `src/astinuslab/orchestration/deps.py` | 777 | 1500 | Contenidor de dependències explícit que va substituir el shim de reexportació dissolt d'`orchestrator.py`; majoritàriament closures de cablejat per defecte. Sostre deliberadament generós -- vegeu la nota de `JobRunDeps.default` a sota. |
| `src/astinuslab/db/repositories.py` | 1033 | 900 | Consultes de persistència i controls del cicle de vida; paginació limitada i recomptes agrupats afegits per l’auditoria del backend. Separar per entitat abans d’ampliar-lo. |

## Funcions llargues

| Funció | LOC | Sostre tou | Notes |
|----------|-----|--------------|-------|
| `src/astinuslab/orchestration/deps.py::JobRunDeps.default` | 623 | 600 | El bloc de cablejat de producció: un closure per dependència injectada. La complexitat ciclomàtica és 1, així que aquí la llargada és una propietat del patró de cablejat explícit i no de lògica embullada. Tracta el *creixement* com el senyal, no el número absolut; si supera el sostre, divideix el contenidor per domini en comptes d'escurçar els closures. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare_windows` | 235 | 180 | Preflight de Windows, firmware per instància, bootstrap QGA i neteja. Preservar l’ordre de les fases i les proves d’arrencada. |
| `src/astinuslab/orchestration/runners/boot_capture_runner.py::boot_and_prepare` | 210 | 180 | Selecció de plataforma i preparació de l’arrencada Linux. Preservar el límit entre plataformes i la neteja d’errors. |
| `src/astinuslab/forensics/vol3_analysis.py::_run_reserved_analysis` | 417 | 180 | Risc de regressió més alt que els mòduls de flux de job-run. |
| `src/astinuslab/forensics/capture_support.py::_acquire_and_publish_locked` | 320 | 180 | Manté la semàntica de bloqueig del costat de captura. |
| `src/astinuslab/forensics/vol3_support.py::run_vol3_symbol_build` | 280 | 180 | La cadena de construcció de símbols està fortament acoblada a l'estat del proveïdor. |
| `src/astinuslab/jobs/startup_recovery.py::cleanup_stale_playground_resources` | 266 | 180 | Neteja d'arrencada amb revalidació de propietat en la neteja de libvirt, del sistema de fitxers i de la base de dades. Manté intactes els recursos dels paquets, la verificació congelada i la custòdia de les captures; extreu una etapa cada cop només amb cobertura específica de custòdia. |
| `src/astinuslab/forensics/vol3_plugin_run.py::run_vol3_plugin` | 395 | 180 | Invocació del plugin, selecció d'ISF i persistència de resultats en un sol cos. |
| `src/astinuslab/lab_packages/package_service.py::LabPackageService.reconcile` | 237 | 180 | Reconciliació d'arrencada per a instàncies de paquet, leases de contingut i accions interrompudes. Mateixa classe de risc que `reconcile_libvirt_resources`: un bug aquí filtra emmagatzematge i VMs, així que extreu una etapa cada vegada amb cobertura dirigida. |
| `src/astinuslab/playground/vm_lifecycle.py::spawn_vm` | 190 | 180 | Baixa des de 416, i sense canvis per la divisió en dominis: va canviar de mòdul, no es va retallar més. Queda l'esquelet try/except/finally que posseeix el lease de reclamació i la neteja de fallades, més les crides a les vuit fases extretes d'ella. Cada retall addicional afegiria un lloc de crida tan llarg com el bloc que elimina. |
| `src/astinuslab/lab_packages/export_service.py::export_locked` | 230 | 180 | Exportació bloquejada: comprovació d'atestació, congelació de la definició, càlcul de digests de capes i assemblatge de l'arxiu. La meitat de digests i assemblatge és l'extracció més neta. |
| `src/astinuslab/db/__init__.py::restore_database` | 209 | 180 | Restauració de còpia amb comprovacions d'esquema i rollback. Intensiu en branques per naturalesa; mantén el camí de rollback en un sol lloc en dividir-ho. |
| `src/astinuslab/lab_packages/verification_service.py::_run_verification` | 205 | 180 | Un intent de verificació d'arrencada neta de cap a cap. Amb moltes branques perquè cada etapa porta una sortida per cancel·lació i una altra per fallada. |
| `src/astinuslab/lab_packages/archive.py::write_archive_to` | 201 | 180 | Escriptor de l'arxiu: ordre de membres, manifest canònic i streaming de payloads en una sola passada perquè la sortida continuï sent determinista byte a byte. Dividir-lo posa en risc el determinisme que existeix per garantir. |
| `src/astinuslab/lab_packages/archive.py::inspect_archive` | 201 | 180 | El camí d'inspecció passiva: totes les comprovacions de seguretat de l'arxiu que s'executen abans d'importar res. La longitud és la llista d'amenaces; mantén les comprovacions en una seqüència llegible. |
| `src/astinuslab/lab_packages/prepare_service.py::run_prepare` | 41 | 180 | Adquisició de contingut: primer els bytes incrustats, després la memòria cau local i després les fonts fixades explícitament, amb publicació atòmica i represa. |
| `src/astinuslab/virtualization/process_support.py::run_command` | 221 | 180 | Bounded process supervision and exception-preserving cleanup; keep custody transitions together. |
| `src/astinuslab/lab_packages/launch_service.py::launch_package` | 197 | 180 | Llançament transaccional: recàlcul del preflight, reserva, creació d'overlays i rollback davant qualsevol fallada. Just per sobre del sostre; mantén sencer el camí de rollback. |
| `src/astinuslab/jobs/runtime_support.py::parse_guest_ip` | 156 | 150 | El parseig de xarxa del guest té moltes branques de fallback. Mantén els canvis estrets i coberts per tests de compatibilitat. |

## Scripts de shell llargs

El shell és la superfície menys comprovada del projecte. ShellCheck i
`bash -n` atrapen sintaxi i cometes; res no llegeix aquests fitxers per
lògica, no hi ha mesura de cobertura, i l'única eina d'acceptació
executable -- `tests/shell/host/upgrade_matrix.sh` encara necessita un amfitrió d'un sol ús i una persona que el llanci. Ara es pot disparar com a `.github/workflows/upgrade-acceptance.yml` en lloc de muntar-lo a mà, però deliberadament no està programat: instal·la i desinstal·la de debò, com a root, així que s'executa on algú ha declarat un amfitrió prescindible. Els dos defectes
bloquejants trobats per l'acceptació de host eren a `ops/deploy/install.sh`,
i un era un patró `rsync --exclude` sense ancorar. La mida és l'únic
indicador barat d'aquest risc, així que es pressuposta aquí com la
superfície de Python.

| Script | LOC actual | Sostre tou | Notes |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh` | 3590 | 2500 | Continua sent el fitxer de més conseqüència del repositori: s'executa com a root, crea identitats, mou ACL i política MAC, i confirma o reverteix una actualització. 136 funcions, així que la seva longitud és composició i no un únic bloc. Baixa des de 5151 ara que els cossos d'instantània/restauració d'ACL, manifest de travessa, registre de transaccions, clau SSH, configuració gestionada, permisos, autoria de configuració, marcador d'identitat i neteja heretada són mòduls reals. Redueix-lo més traient cossos de heredoc (vegeu la taula següent), no fusionant funcions de shell. |
| `tests/shell/host/upgrade_matrix.sh` | 1374 | 1200 | La matriu d'acceptació d'instal·lació, actualització i rollback. La llargada és el nombre de files i l'evidència que cada fila registra; dividir-la dividiria l'informe únic que llegeix qui revisa una release. Es pressuposta perquè el creixement segueixi sent deliberat. |
| `astinuslab.sh` | 1211 | 900 | Llançador d'operador i `setup-host`. Barreja control de serveis en mode dev amb preparació de host; la meitat de preparació de host és la separable. |
| `ops/deploy/configure-privileged-roots.sh` | 782 | 700 | Configuració privilegiada de l'arrel d'emmagatzematge, amb registre de transaccions i rollback exacte. El rollback mateix viu a `ops/deploy/host_access.py`, que aquest script i l'instal·lador criden, i la comprovació del marcador d'identitat a `ops/deploy/privileged_state.py`, que criden els tres punts d'entrada privilegiats. El 35% del que queda continua sent Python incrustat. |

## Python incrustat en shell

1.581 línies de Python viuen dins de heredocs `<<'PY'`. Ruff, Mypy i la
cobertura es basen en `*.py`, així que cap d'ells veu aquest codi; fins que
el helper de descoberta es va ampliar per a continuacions amb barra
invertida, 791 d'aquestes línies ni tan sols es comprovaven sintàcticament.
`tests/python/tooling/test_host_scripts.py` ara compila cada cos i hi passa
pyflakes, però això és un terra, no comprovació de tipus ni cobertura.
Tracta un número creixent aquí com el senyal d'enviar la lògica com a mòdul
real sota `ops/` i cridar-lo, no com a motiu per apujar el sostre.

2.419 línies ja han seguit aquest camí: `ops/deploy/acl_snapshot.py`,
`ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`,
`ops/deploy/managed_config.py`, `ops/deploy/permissions.py` i
`ops/deploy/privileged_state.py` no els importa ningú i s'invoquen com a
subprocessos, exactament com feien els heredocs, però ara Ruff, Mypy i els
seus propis tests unitaris els llegeixen. `ops/deploy/uninstall.sh` va deixar
així totes dues taules: va passar de 819 línies i 480 de Python incrustat a 349
i cap.

| Script | LOC de Python incrustat | Sostre tou | Notes |
|--------|-------------|--------------|-------|
| `ops/deploy/install.sh::embedded-python` | 677 | 800 | Baixa des de 2333: els cossos d'instantània/restauració d'ACL, manifest de travessa, registre de transaccions, clau SSH, configuració gestionada, permisos, autoria de configuració, marcador d'identitat i neteja heretada de vol3 són ara `ops/deploy/acl_snapshot.py`, `ops/deploy/host_access.py`, `ops/deploy/ssh_key.py`, `ops/deploy/managed_config.py`, `ops/deploy/permissions.py` i `ops/deploy/privileged_state.py`. El que queda és una cua llarga sense cap cos gran. |
| `ops/deploy/configure-privileged-roots.sh::embedded-python` | 276 | 300 | Sota el seu sostre des que el cos de rollback de 281 línies va passar a `ops/deploy/host_access.py`, compartit amb l'instal·lador que escriu els registres que llegeix, i la comprovació del marcador d'identitat va passar a `ops/deploy/privileged_state.py`. Queda la comprovació d'autoritat de la font d'instal·lació, de 92 línies. |

## Què fer quan aquesta llista creix

1. Tria l'element més petit que superi el seu sostre.
2. Extreu el bloc contigu de responsabilitat única més llarg a un
   helper germà al mateix mòdul (o un `_helpers.py` germà si emergeix
   un tema clar).
3. El helper ha de prendre totes les entrades com a paràmetres i retornar valors
   explícitament. Sense estat mutable compartit.
4. Executa el slice de tests dirigit per al mòdul tocat, després el
   `ASTINUSLAB_FAKE_LIBVIRT=1 pytest -q` complet.
5. Actualitza els números de LOC en aquest fitxer al mateix commit.
   `tests/python/tooling/test_hotspot_budgets.py` falla fins que ho facis.

Per a una entrada de shell, el pas 2 és diferent: l'objectiu d'extracció és
un mòdul `*.py` real sota `ops/`, invocat com a subprocés, no una altra
funció de shell. Això és el que mou el codi sota Ruff, Mypy i cobertura.
