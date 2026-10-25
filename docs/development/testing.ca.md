# Proves

Els tests automatitzats es configuren a `pyproject.toml` i GitHub Actions.

Després de `./astinuslab.sh setup`, instal·la el toolchain frontend fixat i Chromium per a les proves de navegador:

```bash
npm ci
npx playwright install chromium
```

Consulta la [matriu de verificació](verification-matrix.md), mantinguda en anglès amb fallback per evitar duplicar les instruccions. Aïlla les tasques simultànies i conserva els seus canvis.

## Tests del backend

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
```

Executar un fitxer concret:

```bash
.venv/bin/python -m pytest tests/python/api/test_api.py -q
```

Executar el gate de cobertura de CI:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

El llindar de cobertura és `84` tant a `pyproject.toml` com a `.github/workflows/ci.yml`.

La mesura és cobertura de **branques** (`branch = true` a `pyproject.toml`), no de línies. Aquest codi és dens en clàusules de guarda, i una guarda amb el cos mai executat encara compta com a línia coberta. Comptar arcs va donar 84,3% on les línies donaven 86,7%, així que el llindar es va reajustar a la xifra mesurada. Puja'l a mesura que es tanquin arcs; no el baixis.

### Layout de carpetes de tests del backend

Els tests sota `tests/python/` s'organitzen en carpetes de domini: API, forensics, jobs, playground, lab_packages, host, UI, security, integration, e2e i `host_real`. Executa una carpeta de domini de manera aïllada amb ordres com:

```bash
.venv/bin/python -m pytest tests/python/playground -q
.venv/bin/python -m pytest tests/python/forensics -q
.venv/bin/python -m pytest tests/python/jobs -q
ASTINUSLAB_REAL_HOST=1 .venv/bin/python -m pytest tests/python/host_real -q  # només host KVM/libvirt real
```

`tests/python/ui/` conté tests pytest de rutes FastAPI/Jinja renderitzades al servidor; `tests/browser/` conté tests de navegador Playwright.

## Tests unitaris del repositori

Els tests unitaris de l'instal·lador, helpers de host, launcher i tema de documentació viuen a `tests/python/tooling`:

```bash
.venv/bin/python -m pytest tests/python/tooling -q
```

## Nivells de test

Fes servir el nivell segur més baix que cobreixi el canvi:

| Nivell | Ordre | Abast | Seguretat |
| --- | --- | --- | --- |
| Nivell 0 estàtic | `.venv/bin/python -m ruff check .`, `.venv/bin/python -m mypy src tests/python ops/deploy`, `.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict`, `npm run lint:js` | Sintaxi, imports, tipus, documentació i lint de JavaScript | Sense VM ni mutació del host |
| Nivell 1 backend | `ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q` | Tests unitaris, serveis, repositoris, API i UI renderitzada pel servidor | Usa fakes/fixtures; els fitxers e2e s'ometen excepte si es demanen explícitament |
| Nivell 1b tooling del repositori | `.venv/bin/python -m pytest tests/python/tooling -q` | Tests unitaris de l'instal·lador, launcher, helpers de host i tema de documentació | Usa fitxers temporals i scripts falsos |
| Nivell 2 backend e2e | `.venv/bin/python -m pytest tests/python/e2e -q` | Cobertura de navegador Playwright propietat del backend contra un backend fake-libvirt | S'executa a CI després dels smoke tests frontend |
| Nivell 2 smoke UI | `npm run test:smoke` | Smoke tests de navegador amb mocks de rutes/API | Sense POSTs de provisió ni mutació de libvirt |
| Nivell 2 integració UI | `npm run test:integration` | Checks de lectura/validació amb backend real | Manual per ara; mantenir aïllat de l'estat runtime del desenvolupador |
| Nivell 3 e2e VM complet | `ASTINUSLAB_E2E_FULL=1 npm run test:e2e:full` | Fluxos complets navegador + backend + VMs libvirt | Només manual; pot mutar libvirt i emmagatzematge del host |

### Executar un job més enllà de l'admissió

Crear un Job és segur a qualsevol lloc. *Executar-lo* no ho és: una execució
instal·la una imatge base mitjançant `sudo libvirt_storage_admin.sh` i defineix
un domini, de manera que en un host amb aquesta regla de sudo configurada —
l'estat normal d'una màquina que executa aquest producte — llançar un job des
d'un test provisiona una VM real amb un disc real. Ni `ASTINUSLAB_FAKE_LIBVIRT`
ni `ASTINUSLAB_FAKE_PLAYGROUND` cobreixen aquest camí.

```bash
ASTINUSLAB_FAKE_ORCHESTRATION=1
```

substitueix les dependències de `JobRunDeps` que arriben al host, i res més: els
runners recorren el graf d'estats real i escriuen els esdeveniments reals. Un job
`manual_trigger` arriba a `waiting_for_capture`, així que la progressió d'etapes,
la cancel·lació i el disparador de captura queden a l'abast. Un job de captura fa
la mateixa arrencada i després **falla** on hauria produït evidència: fingir una
arrencada és fingir una observació, fingir una adquisició és falsificar un
artefacte, i el flag no ho fa.

Qualsevol ordre que creuaria cap al host llança una excepció en lloc de retornar
un èxit simulat, de manera que un camí no previst pel fake fa fallar el test en
comptes d'arribar a la màquina. El flag està desactivat tret que es defineixi, el
reporten `/health` i el bàner de fake mode, i és deliberadament independent dels
altres dos perquè el nivell de Jobs continuï provant l'admissió contra un backend
que mai executa.

Els nivells de navegador Playwright fan servir `venv/bin/python` per defecte i poden apuntar a un altre intèrpret quan un host de release o una imatge de CI fa servir una altra ruta de venv:

```bash
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:smoke
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:integration
ASTINUSLAB_PYTHON=/absolute/path/to/python npm run test:e2e:full
```

No executis Nivell 3 en un host amb labs actius fins comprovar VMs, xarxes i rutes d'emmagatzematge en execució. Mantén e2e VM complet fora del CI de PR per defecte tret que existeixi un runner libvirt dedicat.

## Evidència de release (libvirt real)

CI fa servir fakes de libvirt, així que abans de crear una etiqueta de release executa els camins de VM real en un host libvirt i registra el resultat. Una suite pytest verda és obligatòria, però no és suficient per donar GO perquè CI no cobreix tots els camins de libvirt, guest, captura i artefactes de producció.

1. Abans de recollir evidència del host, parteix del commit de release: `git status --short` ha de ser buit i cal registrar el valor exacte de `git rev-parse HEAD` a les notes de release o al paquet d’evidència.
2. Assegura una imatge preparada (amb el backing file present a `images/definitions/`) i una clau SSH activa generada a la pàgina Security (`/ssh-keys`).
3. Executa el Tier-3 full E2E: `npm run test:e2e:full`.
4. Executa els workflows verificats en host sobre el commit de release registrat i prepara el JSON de set files i les proves descrites al procediment intern d'acceptació.
5. Recull i valida independentment el bundle privat amb `collect_release_evidence.sh` i `validate_release_evidence.py` segons aquell procediment.
6. Mantén el bundle privat sota control del responsable de release. Només la projecció `public/`, generada i revisada, es pot publicar.

| # | Workflow | Com executar-lo | Evidència requerida |
| --- | --- | --- | --- |
| 1 | Jobs / `immediate`, memòria | API o UI, qualsevol target | Job completat, trio de l'artefacte de memòria, sha256 independent i prova del workflow |
| 2 | Jobs / `post_script` o interactiu, disc o `both` | API o UI, qualsevol target (el disc es rebutja en mode `immediate`) | Job completat, trio de l'artefacte de disc, sha256 independent i prova del workflow |
| 3 | Jobs / `post_script` | Escenari `workload-sample-post-script` | Job completat, trio de l'artefacte, sha256 independent i log de `running_scenario_script` fins a `waiting_for_capture` |
| 4 | Captura interactiva de Job | Disparar captura des de la UI | Job completat, trio de l'artefacte, traça Playwright i captura de pantalla |
| 5 | Instal·lació de workload a Playground | Instal·lar DVWA des d'una imatge local ja preparada, sense sortida externa | Identitat del workload completat i prova d'accessibilitat o backend equivalent; sense trio d'artefacte |
| 6 | Captura de memòria a Playground | Botó de captura de la UI | Adquisició completada, trio de l'artefacte de memòria, traça Playwright i captura de pantalla |
| 7 | Captura de disc a Playground | Botó de captura de la UI | Adquisició completada, trio de l'artefacte de disc, prova de represa, traça Playwright i captura de pantalla |

GO requereix evidència de les set files produïda al commit de release en un host KVM real. Una fila absent sense waiver complet de sis camps força NO-GO; qualsevol waiver complet i vigent limita el veredicte a GO-WITH-RISKS. No es preveuen waivers per a 1.0.0.

El collector valida els artefactes raw on són, però no els copia mai. `evidence.json` privat és l'autoritat; manifestos, sidecars, proves, checksums i `HOST_WORKFLOW_CHECKLIST.md` són material d'integritat o vistes generades. No editis mai el checklist per convertir-lo en PASS. El validator independent ha d'informar PASS per al candidat exacte i net abans de revisar la release.

## Marcadors de pytest

Els marcadors base es registren a `pyproject.toml` perquè les execucions enfocades siguin explícites:

```bash
.venv/bin/python -m pytest --markers
.venv/bin/python -m pytest tests/python -m "not e2e" --collect-only -q
.venv/bin/python -m pytest tests/python/e2e -m e2e --collect-only -q
.venv/bin/python -m pytest tests/python/tooling -q
```

## Lint i type check

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src tests/python ops/deploy
```

Ruff selecciona `E9`, `F`, `I`, `UP`, `B` i `S`. Mypy està configurat per a Python 3.12 amb `check_untyped_defs = true`.

## Scripts de shell

Quan ShellCheck està instal·lat:

```bash
shellcheck ops/host/*.sh
```

## Documentació

```bash
.venv/bin/python -m pip install "mkdocs-material>=9.5" "mkdocs-static-i18n>=1.2"
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict
```

## CI

`.github/workflows/ci.yml` s'executa en push i pull request. El job backend s'executa amb Python 3.12 i 3.13; el job frontend/Playwright, inclòs pytest e2e del backend, s'executa amb Python 3.12:

1. Instal·lar `libvirt-dev` i `pkg-config`.
2. Instal·la `requirements-dev.lock` — generat a partir de `uv.lock`, mai editat manualment.
   Consulta [Bloqueig de dependències](../architecture/dependency-locking.md).
3. Instal·lar el projecte amb `python -m pip install -e . --no-deps`.
4. Executar lint i comprovació de format de Ruff.
5. Executar Mypy.
6. Executar pytest del backend amb el gate de cobertura.
7. Executar els tests de tooling del repositori des de `tests/python/tooling`.
8. Executar lint frontend, comprovació de format, tests unitaris JavaScript i smoke tests Playwright.
9. Instal·lar Chromium per a Python Playwright.
10. Executar pytest e2e del backend des de `tests/python/e2e`.
11. Pujar els artefactes HTML de cobertura i de l'informe Playwright.

`.github/workflows/vol3-symbols.yml` s'executa en pull requests que toquen rutes relacionades amb Volatility 3 i símbols.
