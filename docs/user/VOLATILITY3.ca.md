# Volatility 3

Si l’estat d’una anàlisi acceptada no està disponible, el resultat continua sent desconegut. Restableix la connexió o inicia sessió de nou i tria **Comprova l’estat** per consultar la mateixa execució; això no envia una altra anàlisi. En recarregar la mateixa pestanya es restaura l’observació de l’execució acceptada i de l’entrada exacta. L’execució continua desactivada mentre el resultat no es resolgui. Triar una altra entrada o un altre connector canvia el context; el text de les regles YARA no es desa per a la recàrrega.

AstinusLab fa servir Volatility 3 per a l'anàlisi de memòria de Linux. A V1, Volatility 3 és central en el flux Captura → Anàlisi → Evidència per als labs de Linux. El límit important és:

- Els ISFs són artefactes de memòria cau global reutilitzables a `vol3/symbols/linux/`.
- La sortida de plugins és local al job a `storage/dumps/<job_id>/vol3/`.

## Analitzar una captura exacta de Playground

1. Abans de tancar la VM, registra el kernel Linux en el moment de la captura (`uname -r`), l’arquitectura (`uname -m`) i com els has verificat, vinculats a la VM, sessió i adquisició. Conserva la identitat de compilació i els símbols. Una altra arrencada de la mateixa imatge no demostra el kernel capturat.
2. Completa una captura de **memòria** a Playground. A **Volatility**, selecciona aquella font exacta mitjançant la identitat d’artefacte/adquisició; un nom semblant no és suficient. Un disc no és una captura de memòria.
3. Introdueix el kernel capturat, l’arquitectura `x86_64` i una nota de procedència que expliqui la verificació. Marca que has verificat el context contra aquella captura i desa’l. No confirmis una versió deduïda només del catàleg.
4. Es crea un Job amb `execution_mode=artifact_analysis`, vinculat a l’artefacte original, checksum, sessió i adquisició. Registra anàlisi derivada sense arrencar ni recapturar una VM. Un context de kernel contradictori es rebutja.
5. Adjunta o puja un ISF compatible. Les metadades de kernel Linux i arquitectura han de coincidir amb el context; canviar l’etiqueta d’un fitxer incompatible no serveix. Prepara els símbols abans de capturar quan sigui possible; un job només-ISF d’una altra arrencada només serveix si coincideix la identitat exacta del kernel/compilació.
6. Executa un plugin Linux com `linux.pslist.PsList` i espera el resultat final. Descarrega l’informe, manifest i checksum des del Job d’anàlisi o els endpoints Artifact. Conserva per separat la memòria original: [exportació d’evidències](../reference/FORENSIC_INTEGRITY.md).

L’API utilitza `POST /api/v1/volatility/analysis-contexts` amb `artifact_id`, `kernel_release`, `architecture: "x86_64"`, `verified: true` i una nota `provenance` no buida. Utilitza la preparació d’autenticació/CSRF dels [Exemples](../usage/examples.md). Es rebutgen discs, bytes absents, propietat incoherent o context sense verificar.


## Eines externes: instal·lació i recuperació

Volatility 3 Framework 2.28.0 i `dwarf2json` són eines externes instal·lades
per l'operador. Llegeix el VSL upstream i l'inventari de
`THIRD_PARTY_NOTICES.md`; AstinusLab mai no descarrega eines ni n'accepta les
llicències en nom de l'operador.

```bash
cd /opt/astinuslab/current
./astinuslab.sh guide-volatility
```

Aquesta ordre és de només lectura: mostra l'estat i les ordres recomanades, però no baixa, instal·la, configura ni reinicia les eines.

### Instal·lar i verificar

Fes servir una ruta versionada fora del checkout d'AstinusLab. Aquest és un
exemple executat per l'operador, no per l'instal·lador:

```bash
sudo install -d -m 0755 /opt/volatility3-2.28.0
sudo chown "$(id -un):$(id -gn)" /opt/volatility3-2.28.0
python3.12 -m venv /opt/volatility3-2.28.0
/opt/volatility3-2.28.0/bin/python -m pip install 'volatility3==2.28.0'
/opt/volatility3-2.28.0/bin/vol -q frameworkinfo.FrameworkInfo
sudo install -d -m 0755 /opt/dwarf2json/0.9.0
sudo install -m 0755 /path/to/downloaded/dwarf2json-linux-amd64 /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
echo 'e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b  /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64' | sha256sum --check -
sha256sum /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64 --help
```

L'exemple mostra `Volatility 3 Framework 2.28.0`. La versió mínima admesa i la
versió provada són **2.28.0**. Les versions posteriors s'accepten amb `untested=true`
(`READY (UNTESTED)` a la guia); verifica els teus casos abans d'utilitzar-les.
Registra l'origen,
el tag o commit, l'arquitectura i el SHA-256 local de `dwarf2json`. Artefacte auditat:
`https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64`;
commit `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`.

### Configurar, recuperar i actualitzar

Edita la secció `[vol3]` existent a `astinuslab.toml`: substitueix o descomenta
les claus de la guia i conserva les altres. Crea la secció només si falta; no
annexis una segona taula `[vol3]`. Valida la sintaxi abans de reiniciar amb la ruta
real de les teves dades. La guia continua mostrant instruccions si el TOML està
mal format.

```bash
/opt/astinuslab/current/.venv/bin/python -c 'import pathlib,tomllib; tomllib.loads(pathlib.Path("/opt/astinuslab/astinuslab.toml").read_text()); print("TOML valid")'
```

Configura les rutes explícites i `dwarf2json_sha256`, reinicia els serveis i
comprova `/api/v1/volatility/readiness` o la pàgina Volatility. Els codis
`*_not_configured`, `*_not_found`, `*_not_executable`,
`volatility_version_unsupported`, `volatility_probe_failed`,
`dwarf2json_hash_not_configured` i `dwarf2json_hash_mismatch` indiquen l'acció
correctiva. Un `guest transfer mismatch` exigeix verificar el mateix hash a host i
guest; Upload ISF és l'alternativa manual.
La preparació de l'anàlisi i la de construcció ISF són independents: una pot estar `READY` mentre l'altra no està disponible.

Per actualitzar, atura l'encuat, instal·la en una ruta versionada nova, revisa
llicència/procedència/hash, executa la prova i els tests de regressió, actualitza
la configuració atòmicament, reinicia i verifica readiness. Conserva la ruta
anterior per al rollback i no sobrescriguis mai un binari. Per revertir, restaura
la ruta i el parell de hash anteriors, reinicia i verifica readiness.

## Volatility 101 per a AstinusLab

Si migres des d'instal·lacions Volatility estàndard, els jobs estrictes només-ISF poden semblar diferents. A AstinusLab, l'anàlisi Linux usa un Intermediate Symbol Format (ISF) ajustat al kernel exacte de la VM, no perfils endevinats. Flux típic: genera/cacheja l'ISF, executa el lab, captura memòria i analitza amb Volatility 3 fent servir aquest ISF.

## Símbols Windows (PDB)

Windows no construeix un ISF per kernel des de DWARF com Linux. Volatility 3 resol símbols des de PDB: per defecte des de Microsoft Symbol Server, o des d'una caché offline sota `vol3/symbols/windows/` per a labs aïllats. Els passos Linux de build ISF/live-guest no apliquen a targets Windows. Aquest suport de Windows és experimental: preparar targets Windows no és un flux automatitzat d'1.0.0, no té evidència d'acceptació de release i queda fora de l'abast suportat.

## Generació d'ISF

AstinusLab construeix un ISF per al kernel exacte en execució de la VM acabada d'arrencar. Això és arquitectura intencionada: la VM és la font de veritat per a la release del kernel, les metadades de distro i la disponibilitat de paquets.

El runner de producció pot construir un ISF mitjançant:

- Reutilitzar un ISF vàlid existent de la memòria cau local quan la release del kernel i el build ID conegut coincideixen.
- Obtenir paquets de depuració de la distro des dels repositoris de paquets del costat de l'host. Per a algunes distros, l'script de host fetch pot fer servir debuginfod internament quan es coneix el build ID del guest.
- Connectar-se al guest en viu i fer servir un `vmlinux` coincident amb DWARF, debuginfod o una instal·lació de paquets de depuració permesa explícitament quan el host fetch no pot produir un ISF vàlid. `dwarf2json` no admet una entrada que només contingui BTF i aquest fallback no la fa servir mai.

Cada job registra si ha produït un ISF nou o n'ha reutilitzat un de la memòria cau.

## Jobs estrictes només-ISF

Un job explícit només-ISF ha de produir o reutilitzar un ISF vàlid. Si no hi ha cap ISF vàlid disponible, el job falla. Això evita que l'anàlisi posterior faci servir símbols que falten, obsolets o no coincidents.

Els jobs només-ISF fan servir la cadena de proveïdors de l'host: `local_cache -> prebuilt_isf -> debuginfod -> host_fetch`. Si aquesta cadena falla, AstinusLab prova el fallback del guest en viu, que pot fer servir un `vmlinux` coincident amb DWARF, debuginfod o una instal·lació de paquets de depuració permesa explícitament. Una entrada que només contingui BTF no s'envia a `dwarf2json`. Els logs de host fetch es conserven a `storage/dumps/<job_id>/vol3/symbol-build-host.log`; els logs del fallback en viu es conserven a `storage/dumps/<job_id>/vol3/symbol-build-live.log`; els intents de proveïdors i les raons de skip/fallada es conserven a `storage/dumps/<job_id>/vol3/symbol-diagnostics.json`.

La identitat de la memòria cau té en compte el build ID. Un ISF en memòria cau amb un build ID coincident es reutilitza; un ISF en memòria cau amb un build ID diferent es tracta com un miss i es reconstrueix. Els ISFs heretats en memòria cau sense build ID registrat continuen sent soft hits perquè les memòries cau antigues segueixin funcionant, i AstinusLab registra el build ID conegut del job a la nova fila d'artefacte.

Les versions d'AstinusLab anteriors a la Fase C escrivien `vol3/symbols/linux/<kernel_release>.json`. Aquests fitxers continuen sent llegibles com a entrades de memòria cau soft-hit heretades quan no hi ha registrat cap build ID en conflicte. Els nous símbols qualificats per build ID fan servir `vol3/symbols/linux/<kernel_release>_<buildid-prefix>.json`.

## Override manual (pujar ISF / dump)

Quan la caché build-ID-aware i la cadena live-guest no poden produir símbols que ja tens, AstinusLab permet **Upload ISF** des del workbench `/volatility` i **Upload memory dump** des del detall del job. Tots dos overrides ometen salvaguardes i registren procedència **unverified**; fes-los servir només quan sàpigues que símbols i dump corresponen al kernel del job. Endpoints: `POST /api/v1/volatility/targets/{target_id}/isf/upload`, `POST /api/v1/jobs/{job_id}/isf/upload` i `POST /api/v1/jobs/{job_id}/dump/upload`.

### Executar només-ISF des de la UI

1. Obre **Volatility** a la barra lateral (`/volatility`).
2. Selecciona un target Linux.
3. Selecciona una ruta de build `immediate`.
4. Selecciona **Queue ISF build**.
5. Observa la targeta Volatility 3 ISF del detall del job enllaçat o la taula d'historial de builds de Volatility.

Els Jobs continuen executant la feina internament; la pàgina Volatility és el flux enfocat de preparació de símbols i **Jobs** queda com a registre d'execucions.

### Executar només-ISF des de l'API

Crea un job amb `build_vol3_isf: true`. AstinusLab el tracta com un job només-ISF per als modes `immediate`.

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "build_vol3_isf": true
}
```

## Sortida de l'anàlisi

Quan s'executa l'anàlisi de plugins, els resultats s'escriuen a `storage/dumps/<job_id>/vol3/`. Aquests fitxers pertanyen a aquell job i es poden eliminar juntament amb els artefactes del job. No són la memòria cau global de símbols.

`analysis-summary.json` registra `dump_path`, `dump_sha256`, `isf_path`, `isf_sha256`, `symbols_path` i `vol3_version`. Durant cada execució AstinusLab prepara l'ISF seleccionat en un directori de símbols local al job i apunta Volatility 3 a aquell directori perquè l'ISF registrat sigui l'únic ISF disponible per a l'execució de plugins.

L'anàlisi correcta també publica `analysis-summary.json` com un artefacte d'informe de primera classe:

- `analysis-summary.json.manifest.json` fa servir `artifact-manifest/v1` amb `producer: vol3_analysis` i `kind: report`.
- `analysis-summary.json.sha256` conté el sidecar SHA-256 del fitxer de resum.
- La fila d'Artefacte registra el fitxer d'informe, la ruta del manifest, la ruta del sidecar SHA-256, la mida i el checksum.
- La fila d'Informe enllaça l'artefacte de memòria d'entrada amb l'artefacte d'informe de sortida.

Plugins habituals inclouen `pslist`, `pstree`, `lsmod`, `mountinfo`, `sockstat`, `ip.Addr` i `ip.Link`.

### Evidència d'execucions de plugin del workbench

Les execucions sota demanda d'un sol plugin des del workbench **Volatility** també són evidència de primera classe. Cada execució completada escriu `storage/dumps/<job_id>/vol3/runs/<run_id>.json`, publica manifest i sidecar SHA-256, i crea files Artifact/Report que enllacen l'artefacte de memòria d'entrada amb el reporte de sortida.

### Executar l'anàlisi post-captura

1. Completa un job amb un bolcat de memòria.
2. Confirma que el job té un ISF vàlid registrat a la targeta Volatility 3 ISF.
3. Selecciona **Run Volatility 3** a la pàgina de detall del job.
4. Revisa `analysis-summary.json` i la sortida per plugin a `storage/dumps/<job_id>/vol3/`.

L'anàlisi també requereix que `settings.vol3_volatility_bin` i `settings.vol3_default_plugins` estiguin configurats.

## Resolució de problemes

### Falten símbols de depuració

Símptomes:

- El job només-ISF falla.
- Els logs esmenten que falten `vmlinux`, `debuginfo`, `dbgsym` o paquets del repositori de depuració.

Solucions:

- Confirma que la VM executa una distribució Linux x86_64 suportada de `docs/reference/DISTRO_SUPPORT.md`. Les arquitectures ARM64 (`aarch64`) explícitament no estan suportades per a la generació d'ISF ni l'anàlisi de memòria.
- Confirma que la release exacta del kernel té paquets de depuració coincidents.
- Habilita el repositori de símbols de depuració de la distro.
- Inspecciona `storage/dumps/<job_id>/vol3/symbol-build-host.log` per veure la fallada exacta del host fetch.
- Fes servir el fallback del guest en viu quan l'obtenció de paquets del costat de l'host no pugui resoldre el paquet.

### ISF no vàlid

Símptomes:

- El fitxer JSON s'escriu però Volatility el rebutja.
- Les execucions de plugins fallen abans de produir sortida útil.

Solucions:

- Elimina el fitxer defectuós de `vol3/symbols/linux/` i torna a executar la generació.
- Comprova que `dwarf2json` pot analitzar el `vmlinux` seleccionat.
- Confirma que el nom del fitxer ISF i les metadades del job coincideixen amb el kernel detectat.

### Fallades del repositori de paquets

Símptomes:

- Les ordres de paquets fallen durant el host fetch o la construcció al guest en viu.
- Els logs mostren errors de DNS, mirror, GPG, subscripció o metadades de release.

Solucions:

- Verifica la xarxa i el DNS des de l'host i el guest.
- Comprova la disponibilitat del mirror del repositori.
- Dona prou temps per a paquets de depuració grans; els paquets de símbols d'Ubuntu/RHEL poden superar 1 GB.
- Ajusta `[vol3].fetch_timeout_seconds` quan els mirrors són lents.
- Per a RHEL, confirma la subscripció del repositori o una font de debuginfo equivalent.
- Reintenta amb una release d'imatge cloud suportada si la release de la distro està al final de la seva vida.

### Fallades de plugins

Símptomes:

- La generació d'ISF té èxit però la sortida de plugins falta o és incompleta.
- Els fitxers a `storage/dumps/<job_id>/vol3/` contenen errors.

Solucions:

- Confirma que existeix el bolcat de memòria del job.
- Confirma que existeix l'ISF coincident a `vol3/symbols/linux/`.
- Inspecciona el log d'esdeveniments del job i la sortida de Volatility per job.
- Torna a executar l'anàlisi després de regenerar l'ISF si els símbols eren obsolets, no vàlids o construïts per a un build de kernel diferent (mateix `uname -r`, build ID diferent — p. ex. després d'una recompilació de seguretat del proveïdor).
