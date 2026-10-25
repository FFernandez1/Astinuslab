"""Topology contracts for the staged repository test layout.

Stage C3 completes browser-test consolidation under ``tests/browser``. The maps
describe the final ownership and discovery contracts.

Stage C owns the application-test move and exposes the only shared app-test API
as ``tests.python._support.helpers``. Stage F closes the temporary root-test
allowance by assigning every application test to an explicit domain. Tooling
tests remain out of default application discovery while retaining an explicit
runnable command.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest
import yaml

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
PYPROJECT = ROOT / "pyproject.toml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
HOST_NIGHTLY_WORKFLOW = ROOT / ".github/workflows/host-nightly.yml"
pytestmark = pytest.mark.unit


# Ownership lives here, rather than in migration prose, so the move commits
# update one small declarative contract and retain an executable history.
class SuiteContract(TypedDict):
    owner: str
    target: str
    legacy_source: str
    source: NotRequired[str]
    markers: NotRequired[frozenset[str]]
    ci_policy: NotRequired[str]
    playwright_projects: NotRequired[frozenset[str]]


class ToolBoundary(TypedDict):
    target: str
    legacy_source: str
    files: frozenset[str]
    executables: frozenset[str]
    recursive: NotRequired[bool]
    legacy_files: NotRequired[frozenset[str]]
    legacy_executables: NotRequired[frozenset[str]]


SUITES: dict[str, SuiteContract] = {
    "app_python": {
        "owner": "application test maintainers",
        "target": "tests/python",
        "legacy_source": "backend/tests",
        "markers": frozenset({"api", "e2e", "host", "host_real", "integration", "ui", "unit"}),
    },
    "tooling_python": {
        "owner": "repository tooling maintainers",
        "target": "tests/python/tooling",
        "legacy_source": "tests/unit",
        "markers": frozenset({"unit"}),
    },
    "host_shell": {
        "owner": "host and installer maintainers",
        "target": "tests/shell/host",
        "legacy_source": "tests/host",
        "ci_policy": "shell-boundary",
    },
    "browser": {
        "owner": "web test maintainers",
        "target": "tests/browser",
        "legacy_source": "tests/ui",
        "playwright_projects": frozenset({"smoke", "full", "integration", "docs", "remediation"}),
    },
}

STAGE_D_TOOL_BOUNDARIES: dict[str, ToolBoundary] = {
    "deployment": {
        "target": "ops/deploy",
        "legacy_source": "deploy",
        "files": frozenset(
            {
                "configure-privileged-roots.sh",
                "install.sh",
                "uninstall.sh",
                "acl_snapshot.py",
                "host_access.py",
                "ssh_key.py",
                "managed_config.py",
                "permissions.py",
                "privileged_state.py",
                "runtime_environment.py",
                "runtime_control_locks.py",
                "shared_databases.py",
                "service_logs.py",
            }
        ),
        "executables": frozenset({"configure-privileged-roots.sh", "install.sh", "uninstall.sh"}),
    },
    "systemd": {
        "target": "ops/systemd",
        "legacy_source": "deploy/systemd",
        "files": frozenset(
            {
                "astinuslab-analysis.service",
                "astinuslab-api.service",
                "astinuslab-runner.service",
            }
        ),
        "executables": frozenset(),
    },
    "images": {
        "target": "ops/images",
        "legacy_source": "scripts",
        "files": frozenset({"download_images.sh", "image_download_metadata.sh"}),
        "executables": frozenset({"download_images.sh"}),
    },
    "host": {
        "target": "ops/host",
        "legacy_source": "scripts/host",
        "files": frozenset(
            {
                "acl_policy.py",
                "acquire_disk_host.sh",
                "acquire_disk_remote.sh",
                "attach_virtiofs_to_vm.sh",
                "bake_workload.sh",
                "bootstrap_cloud_image_libvirt_storage.sh",
                "build_vol3_symbol_remote.sh",
                "check_guest_disk_space.sh",
                "check_job_vm.sh",
                "check_virtiofs_host_support.sh",
                "cleanup_job_vm.sh",
                "collect_guest_metadata.sh",
                "fix_libvirt_path_acl.py",
                "fix_libvirt_path_acl.sh",
                "install_sudoers_astinuslab.sh",
                "libvirt_storage_admin.py",
                "libvirt_storage_admin.sh",
                "mount_output_share_remote.sh",
                "network_isolation_admin.sh",
                "prebake_attacker_image.sh",
                "precook_image.sh",
                "prepare_host_dump_share.sh",
                "prepare_target_image.sh",
                "publish_disk_to_host.sh",
                "rebuild_cloud_image_libvirt_storage.sh",
                "rebuild_cloud_image_with_virtiofs.sh",
                "render_virtiofs_xml.sh",
                "run_guest_script.sh",
                "save_workload_cache.sh",
                "setup_mounted_output_for_job.sh",
                "start_cloud_image_libvirt_storage.sh",
                "start_cloud_image_libvirt_storage_virtiofs.sh",
                "validate_virtiofs_mount.sh",
                "windows_build/poll_build.sh",
            }
        ),
        "executables": frozenset(
            {
                "acquire_disk_host.sh",
                "acquire_disk_remote.sh",
                "attach_virtiofs_to_vm.sh",
                "bake_workload.sh",
                "bootstrap_cloud_image_libvirt_storage.sh",
                "build_vol3_symbol_remote.sh",
                "check_guest_disk_space.sh",
                "check_job_vm.sh",
                "check_virtiofs_host_support.sh",
                "cleanup_job_vm.sh",
                "collect_guest_metadata.sh",
                "fix_libvirt_path_acl.sh",
                "install_sudoers_astinuslab.sh",
                "libvirt_storage_admin.py",
                "libvirt_storage_admin.sh",
                "mount_output_share_remote.sh",
                "network_isolation_admin.sh",
                "prebake_attacker_image.sh",
                "precook_image.sh",
                "prepare_host_dump_share.sh",
                "prepare_target_image.sh",
                "publish_disk_to_host.sh",
                "rebuild_cloud_image_libvirt_storage.sh",
                "rebuild_cloud_image_with_virtiofs.sh",
                "render_virtiofs_xml.sh",
                "run_guest_script.sh",
                "save_workload_cache.sh",
                "setup_mounted_output_for_job.sh",
                "start_cloud_image_libvirt_storage.sh",
                "start_cloud_image_libvirt_storage_virtiofs.sh",
                "validate_virtiofs_mount.sh",
                "windows_build/poll_build.sh",
            }
        ),
    },
    "quality": {
        "target": "tools/quality",
        "legacy_source": "scripts/audit_error_fields.py",
        "files": frozenset(
            {
                "audit_error_fields.py",
                "check_ci_test_report.py",
                "check_coverage_floors.py",
                "legacy_audit.py",
                "secret_scan.py",
            }
        ),
        "executables": frozenset(),
    },
    "developer": {
        "target": "tools/dev",
        "legacy_source": "scripts/dev",
        "files": frozenset(
            {
                "clear_active_jobs.sh",
                "deep_cleanup.sh",
                "e2e_full_server.sh",
                "generate_env_reference.py",
                "init_db.sh",
                "playground_screenshots.cjs",
                "reset_backend_db.sh",
                "run_backend.sh",
                "run_isf_matrix.py",
                "setup_backend_venv.sh",
                # Training package drafts: one lab.json and its paired runbook
                # per exercise, plus the builder the package tests load by path.
                "training_packages/01-single-vulnerability/lab.json",
                "training_packages/01-single-vulnerability/runbook.md",
                "training_packages/02-pivot/lab.json",
                "training_packages/02-pivot/runbook.md",
                "training_packages/03-firewall/lab.json",
                "training_packages/03-firewall/runbook.md",
                "training_packages/README.md",
                "training_packages/build.py",
                # Windows reference-VM investigation helpers, kept beside the
                # tests that exercise their inspection and ACL contracts.
                "windows_candidate_trial/NativeAnswer.cs",
                "windows_candidate_trial/NativeAnswerRestriction.cs",
                "windows_candidate_trial/README.md",
                "windows_candidate_trial/Restrict-CachedAnswer.ps1",
                "windows_candidate_trial/cold_pair.py",
                "windows_candidate_trial/cold_state.py",
                "windows_candidate_trial/firstboot_pair.py",
                "windows_candidate_trial/guarded_shutdown.py",
                "windows_ntfs_acl/CandidateInspection.pm",
                "windows_ntfs_acl/CandidatePlacement.pm",
                "windows_ntfs_acl/README.md",
                "windows_ntfs_acl/RestrictedAcl.pm",
                "windows_ntfs_acl/SyntheticAclProbe.pm",
                "windows_ntfs_acl/acl_report.py",
            }
        ),
        "executables": frozenset({"e2e_full_server.sh", "run_isf_matrix.py"}),
    },
    "release": {
        "target": "tools/release",
        "legacy_source": "scripts/dev",
        "recursive": False,
        "files": frozenset(
            {
                "collect_lab_package_host_evidence.py",
                "collect_lab_package_host_evidence.sh",
                "collect_release_evidence.py",
                "collect_release_evidence.sh",
                "validate_release_evidence.py",
            }
        ),
        "executables": frozenset(
            {
                "collect_lab_package_host_evidence.py",
                "collect_lab_package_host_evidence.sh",
                "collect_release_evidence.py",
                "collect_release_evidence.sh",
                "validate_release_evidence.py",
            }
        ),
    },
}

# Includes 10 reconciliation/DB cases and removes one obsolete log-helper test.
# Collected application + tooling cases: 4990 + 1060.
# +3 for tests/python/e2e/test_jobs_ui.py, which covers Job creation through the
# wizard -- the browser suite had never submitted that form.
# +15 for the auth tier: deep-link return, off-site `next` refusal and expired-
# session recovery in tests/python/e2e/test_auth_ui.py, plus the _safe_next
# enumeration in tests/python/core/test_security_auth.py.
# +3 for tests/python/e2e/test_capture_to_analysis_ui.py, which carries one
# artifact from a Playground capture into the Volatility inventory.
# +9 for tests/python/e2e/test_playground_conflicts_ui.py: what the operator is
# told when the backend refuses, or quietly does something else.
# +3 for tests/python/e2e/test_playground_navigation_ui.py: reload and browser
# navigation against a live lab.
# +4 for tests/python/e2e/test_dataset_states_ui.py: the first five minutes of an
# install, and six months in.
# +7 for tests/python/e2e/test_destructive_admin_ui.py: the controls that delete
# things, and the dialog that gates them.
# +7 for tests/python/e2e/test_locale_ui.py: the product ships in four languages
# and was tested in one.
# +4 for tests/python/e2e/test_missing_job_ui.py: deep links to a Job that is no
# longer there.
# +3 for tests/python/e2e/test_retention_ui.py: Preview must not delete, and the
# run must obey both the confirmation and the window.
# +4 for tests/python/e2e/test_capture_guard_ui.py: a capture is irreversible and
# the endpoint has no idempotency key, so one click must mean one acquisition.
# +3 for tests/python/e2e/test_two_tabs_ui.py: two tabs build one lab together,
# and the tab that did not make a change has to find out about it.
# +11 for the fake job-orchestration path: 10 in
# tests/python/orchestration/test_fake_orchestration.py, which drive a job run
# that never reaches the host, plus one in tests/python/core/test_fake_mode.py
# asserting the new flag reaches the artifact manifest stamp.
# +6 for tests/python/e2e/test_job_lifecycle_ui.py: what a Job does after it is
# admitted -- the first browser coverage of a job that actually runs, spending
# the fake orchestration path above.
# +6 for tests/python/e2e/test_volatility_run_ui.py: what a completed run
# attributes its results to, which needed a stand-in for the operator-supplied
# Volatility binary this project never ships.
# +2 previously uncounted cases, +16 pending ACL cases, and +49 remediation cases: input custody,
# shared ISF publication, package responsiveness, and atomic recipe imports.
# Recounted from the isolated c2757b9 source: four existing application cases
# were missing from the recorded total.
# +1 equal-length source-hash mutation regression from the quality review.
# Main also includes the separate Windows task's 202 application cases.
# +10 request-validation, descriptor-cleanup and stale-dispatch regressions.
# +185 Windows reference-VM cases, recounted once that suite landed on main.
# +10 cases already on main and never counted: the recorded 5537 was stale
# before the isolation work below, which collected 5547 on an unmodified tree.
# +4 suite-isolation cases in tests/python/core/test_suite_isolation.py, which
# assert the conftest data root is not the checkout it runs in.
# +4 data-root cases in tests/python/core/test_suite_data_root.py: the seeded
# data root now has to carry every directory a Settings default names a file
# under, not only the catalog and image directories.
# +4 from splitting the topology PUT check: the bulk-write refusal is asserted
# as 405 per method instead of "404 or 405" once, plus a routed-path case that
# makes the 405 distinguishable from a deleted route.
# -5: tests/python/host/test_install_script_python_heredocs.py parametrizes over
# every heredoc in ops/deploy/install.sh, and five of them became real modules
# under ops/deploy/. The cases did not disappear; they moved to the tooling
# suite below, where they are unit tests of the modules instead of a compile().
# -2: the same heredoc parametrization again, for the managed-config pair.
# `snapshot_managed_config_state` and `restore_managed_config_state` became
# ops/deploy/managed_config.py, so their two compile() cases moved to the
# tooling suite as unit tests of the module.
# -6: the heredoc parametrization again, for the permissions group. The six
# `secure_*` / `remove_legacy_evidence_acls` bodies became
# ops/deploy/permissions.py, so their compile() cases moved to the tooling
# suite as unit tests of the module.
# -3, not -4, for the config-authoring group: four heredocs became
# ops/deploy/managed_config.py subcommands, but tests/python/host/
# test_install_script_python_heredocs.py matches `<<'PY'` only, and
# `neutralise_env_auth_overrides` used `<<'NEUTRALISE'`. That body was
# discovered by the tooling gate in test_host_scripts.py and never by this
# one, so removing it drops no application case.
# -31: tests/python/host/test_install_script_python_heredocs.py is deleted.
# It compiled the `<<'PY'` heredocs in ops/deploy/install.sh and uninstall.sh
# (30 parametrized cases plus one sanity case). test_host_scripts.py in this
# suite finds all 30 of those and 43 more across every tracked *.sh, runs
# pyflakes over each, and carries its own discovery-completeness guard, so
# nothing the deleted file asserted is now unchecked -- see the note on
# BASELINE_COMBINED_TEST_COUNT for what did change.
# +19 application cases: characterization coverage for
# `reconcile_libvirt_resources` before it is split into phase functions.
# The stale-NIC-detach pass went from 33% to 100% covered, legacy network
# teardown and power-state sync from 84%/85% to 100%, and the incomplete-NIC
# pass from 64% to 80% -- its remaining lines are branches that only a
# concurrent parent transition can reach, which the tests record as such.
# +9 application cases: characterization coverage for `spawn_vm` before it
# is split. The failure-cleanup region -- every custody loss between the
# spawn failing and the outcome being recorded -- went from 77% to fully
# covered, and the retry-identity guards that stop a redelivered spawn from
# adopting someone else's NIC are now pinned. spawn_vm overall: 90.5% -> 96%.
# +1 application case: the guard that `reconciliation_phases` reaches its
# shared primitives through the module object rather than binding them by
# name, so patching `reconciliation._owned_domain_name` still reaches the
# phases. It fails if that is rewritten as a direct import.
# +3 application cases: characterization coverage for `capture_disk` before the
# capture group is split out. Its two refusal paths had no test at all, while
# the memory twin of each was covered -- the not-running guard (which must also
# fail the acquisition rather than leave the UI polling a pending row) and the
# CaptureLockBusy path (two concurrent disk captures would drive virsh against
# one domain). capture_disk: 72.2% -> fully covered.
# +12 application cases: characterization coverage for the network domain before
# it is split out. Ten are custody paths where the task has already changed the
# host and then cannot record it -- a libvirt network left defined with no row
# claiming it is a leaked subnet, and the next create fails with "Network is
# already in use". The other two cover what every other network test patches
# away: the helper factory's libvirt wiring, and that the active-network wait
# re-reads the row on each poll instead of deciding once. create_network and
# destroy_network: 93.4% and 86.4% -> fully covered; the domain 88.5% -> 98.5%.
# +10 application cases: characterization coverage for the workload provisioning
# group before it is split out. By the time provisioning runs the VM is booted
# and its disk written, so the uncovered region was the one that decides what
# becomes of that work: both cleanup paths, both halves of each custody pair, a
# custody loss propagated without being recorded as a workload failure, and the
# `teardown_failed` / `failed` distinction that tells an operator whether
# anything is left to clean up. Plus the two helpers every other provisioning
# test patches away -- the libvirt-unreachable cleanup path, and what the
# installer is actually handed. The moving group: 78.1% -> 100%.
# +17 application cases: characterization coverage for `attach_nic` and
# `detach_nic` before they are split out. A NIC is the one playground resource
# that exists in two places at once -- a database row and a live interface on a
# running domain -- so every case is a point where those two can disagree: the
# three parent resources that must still be eligible, the idempotent redelivery
# that must not add a second interface, the claim that must be held before the
# host is touched, the detach-token states, the bounded claim lease and its
# stale-claim recovery, and both rollback outcomes when publication is lost.
# attach_nic 78.0% and detach_nic 84.7% -> both fully covered.
# +14 application cases: characterization coverage for the VM lifecycle before
# it is split out. This is the code that creates disks and defines domains, so
# every case is a point where the host has already been changed and the row
# recording it can no longer be trusted to be ours: destroy's idempotent return,
# both stages where a custody loss must release the lease rather than be written
# down as a teardown failure, both halves of its completion CAS, the ambiguous
# workload-enqueue publish and its cleanup residue, the immutable-backing
# boundary the worker re-checks for itself, NIC reservation under a spawn claim
# that has moved, the disk-root containment check, and spawn publication losing
# its row. The moving group: 94.7% -> 100%.
# +9 application cases: the two seams the module hangs from, covered because the
# VM split took `orchestrator.py` from 83.8% to 76.6% -- under its coverage floor
# -- by removing 342 fully covered statements. `_libvirt_conn` is the only place
# a real libvirt connection is opened and was at 18.2% because tests replace it;
# `_FakePlaygroundLifecycle` is the double that stands in for the host in every
# fake-mode test and was at 23.1% because it *is* the replacement. A fake that
# disagrees with the real driver makes the whole hermetic tier prove nothing, so
# its subnet-derived guest IP is pinned rather than assumed. The floor came back
# to 82.6% without being lowered.
# +37 application cases: characterization coverage for the no-follow filesystem
# primitives before they are split into `playground/filesystem_safety.py`, in
# tests/python/playground/test_filesystem_safety.py. That group was the least
# covered code left in `orchestrator.py` at 78.1%, and every uncovered line in it
# was a refusal -- a planted symlink, a non-directory where a disk belongs, an
# inode swapped between the check and the act, a cleanup walking off the storage
# volume. A refusal nobody has exercised is a refusal nobody has seen work, and
# these are the last guards standing between a teardown and the wrong inode, so
# the group is pinned before it moves rather than after. The group: 78.1% -> 99.1%,
# nine of its ten members fully covered, and every one of the twenty guards was
# mutation-checked by breaking it and confirming a case fails.
# +1 application case: the guard that the three names other modules still reach on
# `orchestrator` -- the two artifact entry points `reconciliation_phases` imports
# from it and the `_create_directory_no_follow` that `acquisition` and
# `vm_lifecycle` call -- really delegate to `filesystem_safety`, arguments and
# all. Every case that patches one of those on `orchestrator` to intercept a call
# made from another module depends on the hop, and a wrapper that stopped
# delegating or dropped `final_mode` would leave all of them green.
# +1 application case: the guard that the spawn path reaches the cloud-init seed
# writer through `cloud_init_seed` at call time, after that group left
# `vm_lifecycle.py`. Three cases patch the writer to make the spawn path skip it,
# fail inside it, or assert it never runs, and a by-name binding in
# `vm_lifecycle` would ignore all three. The case asserting a locked boot
# generates no cloud-init cannot catch that, because a locked boot never calls
# the writer, so it passes under either design; this one is what catches it.
# +117 observability cases, counted on the merged tree: 23 command redaction
# (virtualization/test_command_redaction.py), 18 log format and 16 log context
# (core/test_log_format.py, core/test_log_context.py), 15 operational metrics and
# 5 metrics API (core/test_operational_metrics.py,
# api/test_operational_metrics_api.py), 15 logging configuration
# (core/test_logging_config.py), 9 worker task signals
# (jobs/test_worker_task_signals.py), 6 serial-log retention, 5 Settings
# activity-panel, 4 Logs-page service-log and 1 active-log truncation case.
# +4 application cases: the Lab 1 static-address guest needs `get_ip` to fall
# back to the QEMU guest agent when the network has no DHCP lease for its MAC,
# and the three ways that fallback must decline and keep waiting -- a libvirt
# that cannot answer for the domain, an agent that answers with something other
# than a mapping, and addresses belonging to another interface.
# +88 quality-review regressions: 40 Prepare authority/custody/migration cases,
# 14 retention/download cases, 20 descriptor lifetime/protocol cases and 14
# bounded Jobs history cases. Shared-fixture relocation adds no collected tests.
# +6: orphan cleanup refusal, mixed outcome, failed unlink, empty selection,
# success and duplicate selection preserve custody and report truthful feedback.
# Baseline recount at 12b574b: 5938 (78 existing cases were unrecorded).
# +39 documentation/frontend regressions: 29 UI, 8 mocked bake, 1 ISF, 1 retention.
# +101 full-project review cases: 30 backup, 16 cleanup/recovery, 32 image custody,
# 12 package custody, 7 capture, settings, guardrail, logging and reset refusal.
# +18 operational firewall query/custody regressions from native continuation.
# +16 stopping-guest teardown and retained network-custody regression cases.
# +9 catalog-to-package architecture preservation and unsupported-runtime cases.
# +16 ambiguous firewall-query and exact-tag custody regression cases.
# +5 clean-environment helper identity and explicit-caller validation cases.
# +39: 12 split-log layout, 2 relocated-database clearing refusals,
# 15 import-cache custody and 10 verification-retained Prepare regressions.
# +2: actual transient cache-read I/O failure and distinct digest/size repair cases.
# +2: fresh helper ancestors under private umask preserve existing parent modes.
# +2: Beat creates a missing scratch directory but never a missing storage root.
# +4: workload cache flushes the guest first, quiesces with the agent and refuses an unflushed disk.
# +5: Playground directories outside the registered root fail root_policy readiness.
# +3: desktop-less open-folder refusal, xdg-open reaping and the virsh power-action timeout.
# +26: workload cache, workload editor, SSH key and guest script routes in jobs_routes.
BASELINE_APP_TEST_COUNT = 6231
# +1 tooling case: the truthy-fake-flag rejection in
# tests/python/tooling/test_release_evidence_collector.py is parametrized over
# every producer fake flag, and release evidence must prove the new one off too.
# +4 existing application cases, +12 portable agent-instruction cases, and
# +1 generated/source reference guard and +1 equal-length source-hash case;
# +12 required CI report-gate cases and +10 product regressions.
# Isolated combined collection is 6236; main adds 202 Windows cases.
# +185 Windows reference-VM cases and +5 repository-derived tooling cases.
# +14, matching the application total above: the same 10 uncounted cases already
# on main, plus the 4 new suite-isolation cases. Tooling itself did not grow.
# +8 tooling-only cases for the shell surface, application total unchanged: 2
# heredoc gates (discovery completeness, pyflakes), 4 shell size budgets, and 2
# installer sync guards (runtime-asset completeness, rsync anchoring).
# +4, matching the application total above: the data-root cases. Tooling did
# not grow.
# +4 more, again matching the application total: the topology-method cases.
# +5 tooling cases: the AppArmor rollback custody gate is covered by an
# unprotected template (parametrized over both unsafe flavours), an unprotected
# parent, a replacement race before the rename, and the SELinux relabel root.
# +78 for the heredoc extraction: -5 application cases as recorded above, +24
# in tests/python/tooling/test_acl_snapshot.py, +32 in
# tests/python/tooling/test_host_access.py, +25 in
# tests/python/tooling/test_ssh_key.py, and +2 installer wiring guards.
# +15 for the managed-config extraction: -2 application cases as recorded
# above, and +17 in tests/python/tooling/test_managed_config.py.
# +38 for the permissions extraction: -6 application cases as recorded above,
# and +44 in tests/python/tooling/test_permissions.py.
# +19 for the config-authoring extraction: -3 application cases as recorded
# above, and +22 in tests/python/tooling/test_managed_config.py.
# +18 tooling cases, application total unchanged: the three ACL
# effectiveness readings in ops/deploy/permissions.py became one
# (`acl_grants`), covered by its own parser cases, a mask-neutered and a
# default-only refusal per verifying phase, and equivalence cases against the
# three historical readings it replaced.
# -31, matching the application total above: the deleted heredoc gate. The
# embedded-Python check now lives only in the tooling suite, which
# `pytest tests/python` excludes by design -- CI runs both suites, but a
# developer running the application suite alone no longer compiles the
# heredocs. That is the one thing the deletion costs, and it is the price of
# not keeping a narrower duplicate that reported coverage it did not have.
# +4 tooling cases, application total unchanged: the overage ratchet in
# tests/python/tooling/test_hotspot_budgets.py is parametrized over the
# page's four tables.
# +19, matching the application total above: the reconciliation
# characterization cases. Tooling itself did not grow.
# +9, matching the application total above: the spawn_vm characterization
# cases. Tooling itself did not grow.
# +2 tooling cases, application total unchanged: the type-strictness
# ratchet in tests/python/tooling/test_mypy_strictness.py -- one case
# holding `mypy src --strict` findings per error code, one asserting the
# flags it excludes are really enabled tree-wide.
# +10 tooling cases, application total unchanged: the per-module coverage
# floors in tools/quality/check_coverage_floors.py, covered by
# tests/python/tooling/test_coverage_floors.py -- each way the gate must
# refuse (below floor, module absent from the report, unreadable or empty
# report), plus the guard that CI both emits the JSON and reads it.
# +9 tooling cases, application total unchanged: guards on
# .github/workflows/upgrade-acceptance.yml. That workflow installs and
# uninstalls AstinusLab as root, so the cases pin the properties that keep
# it safe -- manual dispatch only, never the provisioned nightly runner, a
# typed acknowledgement, a host-local disposable marker, no dispatch input
# interpolated into a shell, and the report uploaded even on failure.
# +1, matching the application total above: the phases-module guard.
# +3, matching the application total above: the capture_disk refusal cases.
# Tooling itself did not grow.
# +27 tooling cases, application total unchanged: the custody contract for
# ops/deploy/privileged_state.py, in tests/python/tooling/test_privileged_state.py.
# The install-root authority walk and the split-identity marker check were
# heredocs, and the marker check was three byte-identical copies compared by a
# test. Comparing texts proved the three agreed without proving what they agreed
# on, so these cases pin each refusal: near-miss marker bytes, a writable or
# symlinked marker, a writable, symlinked, non-directory or foreign-owned
# ancestor, and the accept path on a private root-owned tmpfs.
# +12, matching the application total above: the network custody cases.
# +10, matching the application total above: the workload custody cases.
# +17, matching the application total above: the NIC custody cases.
# +14, matching the application total above: the VM lifecycle custody cases.
# +9, matching the application total above: the two shared-seam cases.
# +37, matching the application total above: the no-follow primitive cases.
# +1, matching the application total above: the delegating-wrapper guard.
# +1, matching the application total above: the cloud-init seam guard.
# +8 tooling cases, application total unchanged: the contract for
# `ops/deploy/host_access.py revoke-all`, in tests/python/tooling/test_host_access.py.
# The Part 1 review of the split found this was the one entry point with no
# coverage of its own. Its primitives were pinned through `rollback`, but
# `revoke_all` is what `uninstall.sh` runs as root, and what it decides -- which
# lines stay pending, whether the manifest survives, what it prints for the shell
# to read with `-eq` -- was checkable only by inspecting it against the two
# heredocs it replaced. All 8 were mutation-checked against 7 mutants of that
# composition, including one that prints the old heredoc's `removed=N`.
# +2 tooling cases, application total unchanged: revocation has three outcomes now
# rather than two, so both ways a line can have nothing left to reverse are pinned,
# and so is the truthiness `rollback` was written against. That fix is the review
# finding about the printed count -- the heredoc reached `continue` for a target
# that was already gone and never counted it, and collapsing that into the success
# return made uninstall report grants removed where nothing was touched. 12 mutants
# of the composition and the outcome, all caught.
# +121: the 117 application cases above plus 4 launcher readiness-probe cases,
# which are tooling (tests/python/tooling/test_astinuslab_script.py).
# +25: the four application cases above plus 21 training-package tooling cases
# -- 14 draft and archive contracts (tests/python/tooling/test_training_packages.py)
# and 7 Lab 1 image-builder contracts (tests/python/tooling/test_training_lab1.py).
# +95 quality-review cases: the 88 application cases above and 7 tooling cases
# for the exact portable-skill allowlist and symlink-safe secret scanning.
# +6 application cases for orphan cleanup feedback; tooling count is unchanged.
# Same 117-case correction/addition as the application count; tooling stays 1382.
# +105: the 101 application regressions above and four docs install/audit CI cases.
# +20: those 18 application cases and two restrictive installer mode cases.
# +16 application cases above; tooling count is unchanged.
# +2 tooling cases execute the packaged migration-head helper and empty-head refusal.
# +10: those nine application cases and the valid moved-plan path regression.
# +6 executable archive-provenance cases: supported formats and damaged gzip.
# +17: those sixteen helper cases and the runner-only netlink policy contract.
# +11: five helper identity, four publication/rollback and two native probe cases.
# +148: 41 application cases above plus 107 split DB/log custody, configured
# matrix endpoint/log proof, rollback-state and measured-hotspot tooling cases.
# +29: complete R29 cross-UID recreation/journal and every-move retirement fixtures.
# +99: 96 additional package evidence/observation checks and three source/legal
# prerequisite tooling checks in the integrated working candidate. App count is unchanged.
# +7 launcher cases cover docs preparation failure/order and unchanged startup.
# +12 installer-mode cases retain candidate provenance and prior-upgrade guards.
# +3 custom-root observations cover stale groups, absent ACL and failed inspection.
# +7 cover administrative PATH lookup and bytecode-free deployment imports.
# +7 cover sudoers import immutability and exact evidence traversal ACLs.
# +5 cover repeated split-upgrade evidence ACL preservation and failure propagation.
# +3 cover private custody receipts and existing-file/symlink refusal.
# +8 cover uninstall inventory on split-only hosts and fail-closed refusal.
# +2 app cases cover Beat scratch-directory creation without fabricating a root.
# +9 app cases cover flushed workload caches and Playground root-policy readiness.
# -36: public-release cleanup removed agent-instruction (12), release-check dependency (18),
# legal/release-plan metadata (2), third-party release-plan (1) and hygiene (3) tooling cases.
# +3 app cases above: open-folder refusal and reaping, virsh power-action timeout.
# +26 application cases: jobs_routes admin route coverage.
BASELINE_COMBINED_TEST_COUNT = 7891

# Stage F assigned every formerly deferred application test to a domain. Keep
# this explicit empty set as a guard against silently adding new root tests.
DEFERRED_BACKEND_ROOT_TESTS: frozenset[str] = frozenset()

KNOWN_BACKEND_DUPLICATE_BASENAMES = frozenset(
    {
        "test_api.py",
        "test_catalog.py",
        "test_package_boundaries.py",
        "test_repositories.py",
        "test_startup_recovery.py",
        "test_ui.py",
        "test_vm_lifecycle.py",
    }
)

INTENTIONALLY_UNLINTED_SHELL_SCRIPTS: frozenset[str] = frozenset()


def _suite_roots(suite: SuiteContract) -> tuple[Path, ...]:
    roots = tuple(
        ROOT / path for path in (suite.get("source"), suite["target"]) if path is not None and (ROOT / path).exists()
    )
    assert roots, f"suite is missing declared test paths: {suite.get('source')}, {suite['target']}"

    if len(roots) == 2:
        source_files = {path.relative_to(roots[0]) for path in roots[0].rglob("*") if path.is_file()}
        target_files = {path.relative_to(roots[1]) for path in roots[1].rglob("*") if path.is_file()}
        assert source_files.isdisjoint(target_files), (
            f"suite files exist in both transitional roots: {source_files & target_files}"
        )
    return roots


def _app_python_files(pattern: str) -> list[Path]:
    tooling_target = (ROOT / str(SUITES["tooling_python"]["target"])).resolve()
    return [
        path
        for root in _suite_roots(SUITES["app_python"])
        for path in root.rglob(pattern)
        if tooling_target not in path.resolve().parents
    ]


def _collection_command(*, include_tooling: bool) -> list[str]:
    app_roots = _suite_roots(SUITES["app_python"])
    command = [sys.executable, "-m", "pytest", *(root.relative_to(ROOT).as_posix() for root in app_roots)]
    tooling_target = ROOT / str(SUITES["tooling_python"]["target"])
    if tooling_target.exists() and not include_tooling:
        command.append(f"--ignore={tooling_target.relative_to(ROOT).as_posix()}")
    if include_tooling:
        command.extend(root.relative_to(ROOT).as_posix() for root in _suite_roots(SUITES["tooling_python"]))
    return [*command, "--collect-only", "-q"]


def _collected_count(command: list[str]) -> int:
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
    match = re.search(r"(\d+) tests collected", result.stdout)
    assert match, result.stdout
    return int(match.group(1))


def _toml_string_list(option: str, text: str) -> list[str]:
    match = re.search(rf"^{re.escape(option)}\s*=\s*\[([^]]*)\]", text, re.MULTILINE | re.DOTALL)
    assert match, f"{option} must be declared in pyproject.toml"
    return re.findall(r"[\"\']([^\"\']+)[\"\']", match.group(1))


def _workflow_array(name: str) -> tuple[str, ...]:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    matches = re.findall(rf"^\s*{re.escape(name)}=\(\n(.*?)\n\s*\)\s*$", text, re.MULTILINE | re.DOTALL)
    assert matches, f"{name} array not found in CI shell policy"
    arrays = [tuple(line.strip() for line in match.splitlines() if line.strip()) for match in matches]
    assert all(entries == arrays[0] for entries in arrays), f"CI copies of {name} have drifted: {arrays}"
    return arrays[0]


def _tracked_shell_scripts() -> set[str]:
    result = subprocess.run(["git", "ls-files", "-z", "*.sh"], cwd=ROOT, check=True, capture_output=True, text=True)
    return {path for path in result.stdout.split("\0") if path}


def _ci_pytest_argument_sets() -> tuple[frozenset[str], ...]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    argument_sets = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            uncommented = "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))
            tokens = shlex.split(uncommented.replace("\\\n", " "))
            for index in range(len(tokens) - 2):
                if Path(tokens[index]).name in {"python", "python3"} and tokens[index + 1 : index + 3] == [
                    "-m",
                    "pytest",
                ]:
                    argument_sets.append(frozenset(tokens[index + 3 :]))
    return tuple(argument_sets)


def _playwright_path_literals(config: Path) -> tuple[str, set[str], set[str]]:
    """Read static path declarations without importing arbitrary JavaScript."""
    text = config.read_text(encoding="utf-8")
    test_dir = re.search(r"testDir:\s*[\"']([^\"']+)[\"']", text)
    assert test_dir, f"{config} must declare testDir"
    # globalSetup and globalTeardown are the same kind of declaration and are
    # both resolved relative to the config file, so they are checked together.
    global_setups = set(re.findall(r"global(?:Setup|Teardown):\s*require\.resolve\([\"']([^\"']+)[\"']\)", text))
    fixture_paths = {
        "/".join(parts)
        for parts in re.findall(r"path\.join\(__dirname,\s*([^)]*)\)", text)
        if "fixture" in parts
        for parts in [re.findall(r"[\"']([^\"']+)[\"']", parts)]
    }
    return test_dir.group(1), global_setups, fixture_paths


def test_suite_ownership_and_discovery_inputs_are_declared() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    markers = _toml_string_list("markers", pyproject)
    registered_markers = {entry.partition(":")[0] for entry in markers}

    assert (ROOT / "src/astinuslab/__init__.py").is_file()
    assert not (ROOT / "backend/app").exists()
    assert (ROOT / "src/astinuslab/migrations/alembic.ini").is_file()
    assert not (ROOT / "backend").exists()
    assert (ROOT / "src/astinuslab/web/templates/base.html").is_file()
    assert (ROOT / "src/astinuslab/web/static/css/tokens.css").is_file()
    assert not (ROOT / "frontend").exists()
    assert 'package-dir = {"" = "src"}' in pyproject
    assert 'include = ["astinuslab*"]' in pyproject

    for suite in SUITES.values():
        _suite_roots(suite)
        assert suite["owner"]
        assert set(suite.get("markers", ())) <= registered_markers

    app_discovery = [root.relative_to(ROOT).as_posix() for root in _suite_roots(SUITES["app_python"])]
    assert _toml_string_list("testpaths", pyproject) == app_discovery
    app_conftest = ROOT / str(SUITES["app_python"]["target"]) / "conftest.py"
    assert "def pytest_ignore_collect" in app_conftest.read_text(encoding="utf-8")
    pytest_argument_sets = _ci_pytest_argument_sets()
    for suite_name in ("app_python", "tooling_python"):
        assert all(
            any(root.relative_to(ROOT).as_posix() in arguments for arguments in pytest_argument_sets)
            for root in _suite_roots(SUITES[suite_name])
        )
    assert SUITES["host_shell"]["ci_policy"] in CI_WORKFLOW.read_text(encoding="utf-8")

    for boundary in STAGE_D_TOOL_BOUNDARIES.values():
        target = ROOT / boundary["target"]
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", str(boundary["target"])],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\0")
        relative_files = {Path(path).relative_to(boundary["target"]).as_posix() for path in tracked if path}
        if not boundary.get("recursive", True):
            relative_files = {path for path in relative_files if "/" not in path}
        assert relative_files == boundary["files"]
        assert {path for path in relative_files if (target / path).stat().st_mode & stat.S_IXUSR} == boundary[
            "executables"
        ]
        legacy_tracked = subprocess.run(
            ["git", "ls-files", "--", str(boundary["legacy_source"])],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if "legacy_files" in boundary:
            legacy_root = ROOT / boundary["legacy_source"]
            legacy_files = {Path(path).relative_to(boundary["legacy_source"]).as_posix() for path in legacy_tracked}
            assert legacy_files == boundary["legacy_files"]
            assert {path for path in legacy_files if (legacy_root / path).stat().st_mode & stat.S_IXUSR} == boundary[
                "legacy_executables"
            ]
            for path in legacy_files:
                assert f"{boundary['target']}/{path}" in (legacy_root / path).read_text(encoding="utf-8")
        else:
            assert not legacy_tracked

    shell_lint_patterns = _workflow_array("SHELL_LINT_SCRIPTS")
    assert "ops/**/*.sh" in shell_lint_patterns
    assert "tools/**/*.sh" in shell_lint_patterns


def test_app_python_discovery_preserves_the_branch_baseline() -> None:
    default_command = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    assert _collected_count(default_command) == BASELINE_APP_TEST_COUNT
    assert _collected_count(_collection_command(include_tooling=False)) == BASELINE_APP_TEST_COUNT
    assert _collected_count(_collection_command(include_tooling=True)) == BASELINE_COMBINED_TEST_COUNT


def test_no_unowned_backend_root_tests_remain() -> None:
    actual = {path.name for root in _suite_roots(SUITES["app_python"]) for path in root.glob("test_*.py")}
    assert actual == DEFERRED_BACKEND_ROOT_TESTS


def test_import_collision_guards_cover_known_duplicates_before_moves() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    app_tests = _app_python_files("test_*.py")
    tooling_tests = [path for root in _suite_roots(SUITES["tooling_python"]) for path in root.rglob("test_*.py")]
    backend_names = Counter(path.name for path in app_tests)

    assert {name for name, count in backend_names.items() if count > 1} == KNOWN_BACKEND_DUPLICATE_BASENAMES
    assert "--import-mode=importlib" in _toml_string_list("addopts", pyproject)
    assert re.search(r"^consider_namespace_packages\s*=\s*true\s*$", pyproject, re.MULTILINE)
    typing_exclusion = re.search(r"^exclude\s*=\s*\"([^\"]+)\"", pyproject, re.MULTILINE)
    assert typing_exclusion is not None, "expected an explicit typing exclusion"
    assert re.search(r"tests/python/tooling", typing_exclusion.group(1))
    assert re.search(r"^fail_under\s*=\s*86\s*$", pyproject, re.MULTILINE)
    assert any("--cov-fail-under=86" in arguments for arguments in _ci_pytest_argument_sets())
    assert not ({path.name for path in app_tests} & {path.name for path in tooling_tests})


def test_packaged_migrations_keep_the_application_coverage_boundary_stable() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    assert config["tool"]["coverage"]["run"]["omit"] == ["src/astinuslab/migrations/*"]


def test_host_real_remains_opt_in_in_conftest_and_nightly_ci() -> None:
    host_real_roots = [
        root / "host_real" for root in _suite_roots(SUITES["app_python"]) if (root / "host_real").is_dir()
    ]
    assert len(host_real_roots) == 1
    host_real = host_real_roots[0]
    conftest = (host_real / "conftest.py").read_text(encoding="utf-8")
    nightly = yaml.safe_load(HOST_NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))

    assert 'os.getenv("ASTINUSLAB_REAL_HOST"' in conftest
    assert "pytest.skip" in conftest
    job = nightly["jobs"]["host-real"]
    assert job["env"]["ASTINUSLAB_REAL_HOST"] == "1"
    run_commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert f"pytest {host_real.relative_to(ROOT).as_posix()} -m host_real" in run_commands


def test_browser_integration_is_an_unconditional_frontend_ci_gate() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["frontend"]
    assert "if" not in job
    assert job.get("continue-on-error") is not True
    steps = [step for step in job["steps"] if step.get("run", "").strip() == "npm run test:integration"]
    assert len(steps) == 1
    step = steps[0]
    assert "if" not in step
    assert step.get("continue-on-error") is not True
    assert step["env"]["ASTINUSLAB_PYTHON"] == "${{ env.pythonLocation }}/bin/python"


def test_every_tracked_shell_script_has_ci_policy_or_exact_exemption() -> None:
    linted = {
        path.relative_to(ROOT).as_posix()
        for pattern in _workflow_array("SHELL_LINT_SCRIPTS")
        for path in ROOT.glob(pattern)
        if path.is_file()
    }
    assert all((ROOT / path).is_file() for path in INTENTIONALLY_UNLINTED_SHELL_SCRIPTS)
    assert not (_tracked_shell_scripts() - linted - INTENTIONALLY_UNLINTED_SHELL_SCRIPTS)


def test_playwright_configs_resolve_transitional_test_and_fixture_paths() -> None:
    declared_projects: set[str] = set()
    for filename in (
        "playwright.config.js",
        "playwright.docs.config.js",
        "playwright.integration.config.js",
        "tests/browser/remediation.config.js",
    ):
        config_path = ROOT / filename
        test_dir, global_setups, fixture_paths = _playwright_path_literals(config_path)
        # Playwright resolves testDir and require.resolve hooks against the
        # config's own directory, not the repository root. Only root-level
        # configs existed when this contract was written, which hid the
        # difference; the remediation config lives beside its specs.
        resolved_test_dir = (config_path.parent / test_dir).resolve()
        assert resolved_test_dir.is_dir()
        assert resolved_test_dir.is_relative_to(ROOT / SUITES["browser"]["target"])
        assert all((config_path.parent / path).is_file() for path in global_setups)
        assert all((ROOT / path).is_dir() for path in fixture_paths)

        if filename == "playwright.config.js":
            assert fixture_paths == {"tests/browser/fixtures/images"}

        if filename == "playwright.docs.config.js":
            declared_projects.add("docs")
        else:
            text = (ROOT / filename).read_text(encoding="utf-8")
            declared_projects.update(
                re.findall(r"(?:^ {6}name:|\{[ \t]*name:)[ \t]*[\"']([^\"']+)[\"']", text, re.MULTILINE)
            )

    assert declared_projects == SUITES["browser"]["playwright_projects"]
    package = (ROOT / "package.json").read_text(encoding="utf-8")
    assert "tests/browser/unit/**/*.test.mjs" in package
    assert "tests/browser/helpers/preconditions.test.js" in package
    # npm runs scripts through `sh`, which has no globstar, so an *unquoted*
    # `**` expands to exactly one directory level. Every unit test happens to
    # sit one level deep today, so the unquoted form ran all of them and a file
    # added at tests/browser/unit/foo.test.mjs -- or three levels down -- would
    # have been skipped silently, with no failure and no change in the count.
    # Quoting hands the pattern to Node, which expands `**` properly.
    unit_script = json.loads(package)["scripts"]["test:js:unit"]
    assert '"tests/browser/unit/**/*.test.mjs"' in unit_script, (
        "quote the unit-test glob so Node expands it instead of sh: " + unit_script
    )
    assert not list((ROOT / "src/astinuslab/web/static").rglob("*.test.mjs"))
    assert (ROOT / "tests/browser/fixtures/images").is_dir()


def test_test_imports_use_only_the_explicit_shared_support_api() -> None:
    # Only these support modules are public; test-module imports stay forbidden.
    shared_support_modules = {
        "tests.python._support.helpers",
        "tests.python._support.lab_packages",
        "tests.python._support.lab_package_observations",
        "tests.python._support.vol3_fixtures",
        "tests.python._support.windows_images",
    }
    offenders = []
    app_files = _app_python_files("*.py")
    tooling_files = [path for root in _suite_roots(SUITES["tooling_python"]) for path in root.rglob("*.py")]
    for path in [*app_files, *tooling_files]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend.tests"):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: from {node.module}")
                elif node.module.startswith("tests.python") and node.module not in shared_support_modules:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: from {node.module}")
            elif isinstance(node, ast.Import):
                for name in node.names:
                    if name.name.startswith("backend.tests") or (
                        name.name.startswith("tests.python") and name.name not in shared_support_modules
                    ):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: import {name.name}")

    assert not offenders, "\n".join(offenders)
