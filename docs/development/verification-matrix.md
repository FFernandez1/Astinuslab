# Select checks for a change

This matrix is the shared entry point for contributor and PR guidance.
Use an isolated environment and inspect the tier's prerequisites before running it.
For a focused edit, run the affected tests during development, then the applicable
gate at the review boundary. Do not label an unavailable or skipped gate as passing.

| Changed surface | Applicable checks | Evidence and limits |
| --- | --- | --- |
| Python/API/service behaviour | Focused domain tests, Ruff lint/format and Mypy; use the [testing commands and prerequisites](testing.md), including the backend coverage gate before integration | Demonstrate the regression and expected behaviour; fake-mode results do not establish real-host acceptance |
| Jobs/worker/recovery | The Python checks plus affected `tests/python/jobs/`, `orchestration/` and `integration/` cases | Include failure, cancellation and idempotency/recovery where affected; the real-worker SQLite test is not proof of broker restart/redelivery |
| Repository layout and hygiene | `.venv/bin/python -m pytest tests/python/tooling/test_repository_hygiene.py tests/python/tooling/test_test_topology.py -q` | Check tracked files, ignore rules and actual source references |
| General tooling/installer/package changes | Explicit `.venv/bin/python -m pytest tests/python/tooling -q`; static checks for changed code | Tooling is excluded from application discovery; temporary/fake installer tests do not authorize installation on the host |
| UI/templates/routes | `npm run lint:js`, `npm run format:check`, `npm run test:js:unit`, `npm run test:smoke`, `npm run test:integration`; affected Python route tests | Check relevant user journeys, keyboard/focus, error states and responsive rendering; use `npm run test:remediation` for workbench async behaviour |
| English docs/navigation/theme | `NO_MKDOCS_2_WARNING=1 .venv/bin/python -m mkdocs build -f docs/mkdocs.yml --strict`; affected tooling docs checks; `npm run test:docs` for rendered docs behaviour | Confirm locale parity or documented fallback. Internal maintainer records are not part of the repository or the site |
| Shell/host helpers | Bash syntax and ShellCheck on affected scripts; focused `tests/python/host/` and `tooling/` checks | CI's `shell-boundary` job owns the full lint glob set. Do not execute a helper just to validate its syntax |
| Wheel/package resources | `ASTINUSLAB_RUN_WHEEL_CONTRACT=1 .venv/bin/python -m pytest tests/python/tooling/test_installed_artifact.py -q` | Builds and checks an isolated installed artifact; preserve runtime and resource contracts |
| Dependencies/security boundaries | Relevant negative tests, tooling dependency/secret checks and corresponding CI jobs | Do not weaken a policy or suppress findings to get a pass; keep scanner output redacted |
| Real acquisition, VM lifecycle or release | The separately authorized real-host/release procedure | Record host prerequisites and exact candidate evidence; this is never an incidental local check |

The broad Mypy command covers `ops/deploy`, where the installer's extracted
Python modules live, but still excludes tooling. CI also checks these
maintained repository and topology contracts explicitly; run the same check
when changing them:

```bash
.venv/bin/python -m mypy \
  tests/python/tooling/test_mypy_strictness.py \
  tests/python/tooling/test_repository_hygiene.py \
  tests/python/tooling/test_test_topology.py
```

CI remains the executable source for its complete command set. Review
`.github/workflows/ci.yml` when a gate changes; update this matrix and affected
consumers together. The scheduled `.github/workflows/host-nightly.yml` is separate:
a workflow definition alone does not demonstrate runner availability or a recent pass.

For logs/settings controls, Volatility capture-context fields or shared page
gutters, run `npx playwright test panel-geometry --project=smoke` with an isolated
fake backend. The suite measures rendered sizes, responsive layout and keyboard
traversal. It covers two control widths and four representative page titles;
extend the relevant checks when changing other layouts.

Full-browser VM/SSH acceptance is separately authorized and default-off.
`npm run test:e2e:full` explicitly selects it: unavailable libvirt or an absent
active SSH key now fails collection. The fresh-VM row must run and pass. The
existing-target SSH row remains optional when no targets are running; report that
skip as missing existing-target evidence. Synthetic prerequisite unit tests are
not proof of real-host readiness.

Ordinary backend CI explicitly installs ACL tools, QEMU image utilities, pigz,
rsync, curl, ShellCheck and fakeroot, and creates ACL test subjects only on its
disposable runner. Pytest saves backend/tooling JUnit reports with skip reasons.
`tools/quality/check_ci_test_report.py` rejects missing required native-tool
modules, unexpected skips and failed cases. Its only in-scope skip exception is
the named root-only installer mutation check: ordinary CI runs unprivileged.
Run the gate against a report containing its complete declared module set:

```bash
python tools/quality/check_ci_test_report.py backend test-results/backend.xml
python tools/quality/check_ci_test_report.py tooling test-results/tooling.xml
```

Do not run the CI account/package setup on an operator workstation. Reference
images, real-host acceptance, systemd-version compatibility and Windows executable
acceptance remain separate scopes; their skips are reported, not claimed as passes.
