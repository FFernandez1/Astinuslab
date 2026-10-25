## Summary

<!-- What does this PR change and why? -->

## Verification

<!-- Use docs/development/testing.md, "Select checks for a change".
Record exact commands/results and any skipped or unavailable gate. -->

- [ ] Acceptance behaviour demonstrated; regression check included for behaviour changes
- [ ] Applicable Python lint, format, type and test gates run
- [ ] Repository tooling tests selected explicitly if tooling/instructions changed
- [ ] Applicable JS/browser/visual checks run if UI/routes changed
- [ ] Strict docs build and relevant locale/fallback checks run if published docs changed
- [ ] Final diff reviewed; changed commands/paths updated in their instruction consumers

Commands and results:

<!-- Include remaining failures, missing prerequisites and limits of fake-mode evidence. -->

## Product-language checklist

Required for PRs touching docs, UI copy, routes, Playground, Jobs, artifacts,
evidence, or product language. Delete this section if not applicable.

- [ ] Canonical terms preserved: **Job, Scenario, Target, Workload, Acquisition, Playground, Attacker**
- [ ] Playground remains the front-door workflow
- [ ] V1 trust model unchanged (localhost / trusted-local by default, optional single-admin API auth for network binds)
- [ ] Volatility 3 remains the core V1 evidence path
- [ ] i18n key parity kept across en/es/fr/ca (`pytest tests/python/ui/test_i18n_parity.py`)
- [ ] Roadmap items (Windows automated provisioning parity, broad live telemetry, generated reports) not presented as shipped V1

## Notes

<!-- Risks, follow-ups, anything reviewers should know -->
