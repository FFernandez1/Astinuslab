"""Secret gate contract; runnable with unittest alone in the scanner CI job."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/quality/secret_scan.py"
# Row CL26 of the documentation/frontend claims ledger, verbatim.
CL26_LEDGER_ROW = (
    "| CL26 | `docs/api/authentication.md:5-7,60-67` says enabled auth gates every non-public "
    "UI/API/download route using bearer or independent browser session, with a precise public "
    "allowlist and a single admin role. | `src/astinuslab/core/security.py:21-28,112-129` "
    "implements the allowlist and bearer/session gate. "
    "`src/astinuslab/core/browser_sessions.py` implements opaque hashed session credentials, "
    "expiry/revocation and admin-hash binding. | **E1**, source contract agrees; session logic "
    "was inspected in the operator review. Narrower README/API summaries omit details and "
    "should defer to the dedicated guide. No credential values were read. | **Confirmed at E1** "
    "for the dedicated guide's boundary. This is not a broad security certification or "
    "reverse-proxy/network deployment acceptance; installed network configuration may require "
    "**E5**. Preserve public readiness/login/static exceptions in summaries. |"
)


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="astinuslab-secret-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        spec = importlib.util.spec_from_file_location("secret_scan_test", SCRIPT)
        self.scan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.scan)
        self.scan.REPO_ROOT = self.root
        self.scan.CONFIG_PATH = self.root / ".gitleaks.toml"
        self.scan.CONFIG_PATH.write_text("[extend]\nuseDefault = true\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Scanner fixture")
        self.git("config", "user.email", "scanner@example.invalid")
        (self.root / "safe.txt").write_text("ordinary source\n")
        self.git("add", "safe.txt")
        self.git("commit", "-qm", "safe fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

    def fake_scanner(self, *, version="8.30.1", status=0, payload="[]", report=True):
        binary = Path(self.tmp.name) / "gitleaks"
        binary.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            f"if sys.argv[1] == 'version': print({version!r}); sys.exit(0)\n"
            f"if {report!r}: pathlib.Path(sys.argv[sys.argv.index('--report-path')+1]).write_text({payload!r})\n"
            f"sys.exit({status})\n"
        )
        binary.chmod(0o755)
        return binary

    def run_scan(self, binary, scope="tracked"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = self.scan.main(["--gitleaks", str(binary), "--scope", scope])
        return code, output.getvalue()

    def real_scanner(self):
        binary = os.environ.get("ASTINUSLAB_TEST_GITLEAKS_BIN") or shutil.which("gitleaks")
        if not binary:
            self.skipTest("pinned Gitleaks unavailable; secret-scan CI runs these with the real binary")
        return Path(binary).resolve()

    def put_secret(self, *, inline=False):
        self.secret = "gh" + "p_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
        suffix = " # gitleaks:allow" if inline else ""
        (self.root / "fixture.py").write_text(f'token = "{self.secret}"{suffix}\n')
        self.git("add", "fixture.py")

    def put_skill(self, name="astinuslab-development"):
        canonical = self.root / ".claude/skills" / name
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("Maintained portable skill\n")
        link = self.root / ".agents/skills" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(f"../../.claude/skills/{name}", target_is_directory=True)
        self.git("add", ".claude", ".agents")
        return canonical, link

    def test_canonical_skill_links_are_scanned_as_blobs_without_following_them(self):
        for name in ("astinuslab-development", "release-check", "astinuslab-quality-review"):
            canonical, link = self.put_skill(name)
        for builder in (self.scan._build_index_snapshot, self.scan._build_tracked_snapshot):
            snapshot = Path(self.tmp.name) / builder.__name__
            snapshot.mkdir()
            self.assertEqual(builder(snapshot), 7)
            for name in ("astinuslab-development", "release-check", "astinuslab-quality-review"):
                copied_link = snapshot / ".agents/skills" / name
                self.assertFalse(copied_link.is_symlink())
                self.assertEqual(copied_link.read_text(), f"../../.claude/skills/{name}")
                self.assertIn("Maintained", (snapshot / ".claude/skills" / name / "SKILL.md").read_text())
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 0)

    def test_skill_target_secret_is_detected_and_redacted(self):
        canonical, _ = self.put_skill()
        self.put_secret()
        (canonical / "SKILL.md").write_bytes((self.root / "fixture.py").read_bytes())
        self.git("add", ".claude")
        self.git("rm", "--cached", "fixture.py")
        (self.root / "fixture.py").unlink()
        code, output = self.run_scan(self.real_scanner())
        self.assertEqual(code, 1)
        self.assertIn("SKILL.md", output)
        self.assertNotIn(self.secret, output)

    def test_unknown_skill_link_and_changed_index_target_fail_closed(self):
        canonical, link = self.put_skill("unapproved")
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)
        self.git("rm", "-r", "--cached", ".agents", ".claude")
        canonical, link = self.put_skill()
        for value in (
            "/tmp/operator",
            "../../operator",
            "../../.claude/skills/release-check",
            "../../.claude/skills/astinuslab-development/",
        ):
            with self.subTest(target=value):
                link.unlink()
                link.symlink_to(value)
                self.git("add", ".agents")
                self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)

    def test_skill_worktree_target_cannot_change_after_staging(self):
        _, link = self.put_skill()
        link.unlink()
        link.symlink_to("../../operator")
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)
        link.unlink()
        link.write_text("ordinary file replacing tracked link")
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)

    def test_skill_requires_tracked_canonical_source(self):
        canonical, _ = self.put_skill()
        self.git("rm", "--cached", ".claude/skills/astinuslab-development/SKILL.md")
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)
        self.assertTrue((canonical / "SKILL.md").is_file())

    def test_canonical_skill_parent_must_not_redirect_to_operator_state(self):
        canonical, _ = self.put_skill()
        outside = Path(self.tmp.name) / "private-skill"
        shutil.move(str(canonical), outside)
        canonical.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)

    def test_missing_and_wrong_scanner_fail_closed(self):
        self.assertEqual(self.run_scan(self.root / "missing")[0], 2)
        self.assertEqual(self.run_scan(self.fake_scanner(version="0.0.0"))[0], 2)

    def test_crash_missing_and_malformed_reports_fail_closed(self):
        for kwargs in ({"status": 3}, {"report": False}, {"payload": "{}"}, {"payload": "[1]"}, {"payload": ""}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.run_scan(self.fake_scanner(**kwargs))[0], 2)

    def test_findings_fail_even_if_scanner_returns_zero_and_never_print_values(self):
        payload = json.dumps(
            [
                {
                    "RuleID": "fixture",
                    "File": "fixture.py",
                    "StartLine": 1,
                    "Secret": "do-not-print",
                    "Match": "do-not-print",
                }
            ]
        )
        code, output = self.run_scan(self.fake_scanner(payload=payload))
        self.assertEqual(code, 1)
        self.assertIn("fixture.py", output)
        self.assertNotIn("do-not-print", output)

    def test_weakened_configuration_fails_closed(self):
        configurations = (
            "[extend]\nuseDefault = false\n",
            '[extend]\nuseDefault = true\ndisabledRules = ["github-pat"]\n',
            '[extend]\nuseDefault = true\n[allowlist]\npaths = [".*"]\n',
            "invalid toml",
        )
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                self.scan.CONFIG_PATH.write_text(configuration)
                self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)

    def test_empty_tracked_snapshot_fails_closed(self):
        self.git("rm", "safe.txt")
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)

    def test_symlink_parent_cannot_read_untracked_operator_files(self):
        parent = self.root / "tracked"
        parent.mkdir()
        (parent / "private.txt").write_text("original source")
        self.git("add", "tracked/private.txt")
        shutil.rmtree(parent)
        outside = Path(self.tmp.name) / "operator-state"
        outside.mkdir()
        (outside / "private.txt").write_text("private runtime value")
        parent.symlink_to(outside, target_is_directory=True)
        snapshot = Path(self.tmp.name) / "snapshot"
        snapshot.mkdir()
        with self.assertRaises(self.scan.ScanError):
            self.scan._build_tracked_snapshot(snapshot)
        self.assertFalse((snapshot / "tracked/private.txt").exists())

    def test_inline_comment_cannot_hide_a_real_detection(self):
        binary = self.real_scanner()
        self.put_secret(inline=True)
        code, output = self.run_scan(binary)
        self.assertEqual(code, 1)
        self.assertNotIn(self.secret, output)

    def test_ignored_runtime_secret_is_not_scanned(self):
        binary = self.real_scanner()
        self.put_secret()
        self.git("reset", "-q", "HEAD", "fixture.py")
        (self.root / ".gitignore").write_text("fixture.py\n")
        self.assertEqual(self.run_scan(binary)[0], 0)

    def test_deleted_secret_is_detected_in_linked_worktree_history(self):
        binary = self.real_scanner()
        self.put_secret()
        self.git("commit", "-qm", "synthetic detection fixture")
        self.git("rm", "fixture.py")
        self.git("commit", "-qm", "remove synthetic fixture")
        worktree = Path(self.tmp.name) / "linked"
        self.git("worktree", "add", "--detach", str(worktree), "HEAD")
        self.scan.REPO_ROOT = worktree
        self.assertEqual(self.run_scan(binary, "tracked")[0], 0)
        code, output = self.run_scan(binary, "history")
        self.assertEqual(code, 1)
        self.assertNotIn(self.secret, output)

    def test_secret_introduced_only_by_merge_resolution_is_detected(self):
        binary = self.real_scanner()
        main_branch = self.git("branch", "--show-current").strip()
        self.git("checkout", "-qb", "side")
        (self.root / "side.txt").write_text("side branch change")
        self.git("add", "side.txt")
        self.git("commit", "-qm", "side change")
        self.git("checkout", "-q", main_branch)
        (self.root / "main.txt").write_text("main branch change")
        self.git("add", "main.txt")
        self.git("commit", "-qm", "main change")
        self.git("merge", "--no-commit", "--no-ff", "side")
        self.put_secret()
        self.git("commit", "-qm", "merge with synthetic resolution fixture")
        self.git("rm", "fixture.py")
        self.git("commit", "-qm", "remove synthetic fixture")
        code, output = self.run_scan(binary, "history")
        self.assertEqual(code, 1)
        self.assertNotIn(self.secret, output)

    def test_staged_secret_is_found_after_working_copy_is_cleaned_or_deleted(self):
        binary = self.real_scanner()
        self.put_secret()
        for delete in (False, True):
            with self.subTest(delete=delete):
                if delete:
                    (self.root / "fixture.py").unlink()
                else:
                    (self.root / "fixture.py").write_text("clean working copy")
                code, output = self.run_scan(binary)
                self.assertEqual(code, 1)
                self.assertNotIn(self.secret, output)

    def test_git_environment_cannot_redirect_scan_to_clean_index(self):
        binary = self.real_scanner()
        clean_index = Path(self.tmp.name) / "clean-index"
        shutil.copy2(self.root / ".git/index", clean_index)
        self.put_secret()
        with mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(clean_index)}):
            self.assertEqual(self.run_scan(binary)[0], 1)

    def test_uninitialized_submodule_is_not_silently_skipped(self):
        commit = self.git("rev-parse", "HEAD").strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{commit},uninitialized-module")
        self.assertEqual(self.run_scan(self.fake_scanner())[0], 2)

    def test_force_tracked_ignored_secret_is_still_scanned(self):
        binary = self.real_scanner()
        self.put_secret()
        (self.root / ".gitignore").write_text("fixture.py\n")
        self.git("add", ".gitignore")
        self.assertEqual(self.run_scan(binary)[0], 1)

    def test_reviewed_checksum_is_allowed_in_current_tree_but_changed_value_is_detected(self):
        binary = self.real_scanner()
        self.scan.CONFIG_PATH.write_bytes((ROOT / ".gitleaks.toml").read_bytes())
        evidence = self.root / "docs/remediation/evidence/merge-to-main/source-sha256.json"
        evidence.parent.mkdir(parents=True)
        field = "src/astinuslab/forensics/api.py"
        digest = "ac33cfac8898eb7bb38246f4648565903c48a19c122f21365be8ad593088b785"
        evidence.write_text(json.dumps({field: digest}, indent=2) + "\n")
        self.git("add", str(evidence))
        self.assertEqual(self.run_scan(binary)[0], 0)
        evidence.write_text(json.dumps({field: secrets.token_hex(32)}, indent=2) + "\n")
        self.git("add", str(evidence))
        self.assertEqual(self.run_scan(binary)[0], 1)

    def test_historical_exceptions_do_not_suppress_new_credentials_in_evidence_paths(self):
        binary = self.real_scanner()
        self.scan.CONFIG_PATH.write_bytes((ROOT / ".gitleaks.toml").read_bytes())
        self.put_secret()
        evidence = self.root / "docs/remediation/evidence/merge-to-main/source-sha256.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps({"api_key": self.secret}))
        (self.root / "fixture.py").unlink()
        self.git("reset", "-q", "HEAD", "fixture.py")
        self.git("add", str(evidence))
        self.assertEqual(self.run_scan(binary)[0], 1)

    def test_reviewed_auth_prose_exception_is_exact_and_preserves_secret_detection(self):
        binary = self.real_scanner()
        self.scan.CONFIG_PATH.write_bytes((ROOT / ".gitleaks.toml").read_bytes())
        # The ledger is an internal record kept outside the published tree, but it
        # stays in history, so the exception must still hold for its CL26 row.
        relative = "docs/plans/2026-10-03-documentation-frontend-review/claims-ledger.md"
        prose = CL26_LEDGER_ROW
        evidence = self.root / relative
        evidence.parent.mkdir(parents=True)
        evidence.write_text(prose + "\n")
        self.git("add", str(evidence))
        self.assertEqual(self.run_scan(binary)[0], 0)
        credential = f'api_key = "{secrets.token_hex(32)}"'
        evidence.write_text(prose + " " + credential + "\n")
        self.git("add", str(evidence))
        self.assertEqual(self.run_scan(binary)[0], 1)
        evidence.write_text(prose + "\n" + credential + "\n")
        self.git("add", str(evidence))
        self.assertEqual(self.run_scan(binary)[0], 1)
        evidence.write_text(prose + "\n")
        other_report = self.root / "other-report.md"
        other_report.write_text(prose + "\n")
        self.git("add", str(evidence), str(other_report))
        self.assertEqual(self.run_scan(binary)[0], 1)

    def test_shallow_history_is_not_reported_as_complete(self):
        shallow = Path(self.tmp.name) / "shallow"
        self.git("clone", "-q", "--depth=1", self.root.as_uri(), str(shallow))
        self.scan.REPO_ROOT = shallow
        self.assertEqual(self.run_scan(self.fake_scanner(), "history")[0], 2)


if __name__ == "__main__":
    unittest.main()
