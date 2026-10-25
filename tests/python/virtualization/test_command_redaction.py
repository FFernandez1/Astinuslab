"""Command lines reach logs, exception messages and HostAction.error_summary.

Redaction is deliberately narrow. Over-redacting destroys the diagnostic value
that made the line worth logging in the first place: an operator reading
`command failed` needs to see which helper ran against which guest.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from astinuslab.virtualization.process_support import redact_argv, redacted_command

pytestmark = pytest.mark.unit


@contextmanager
def _null_scope():
    """A session scope for runs that touch no job row."""
    yield None


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSecretFlags:
    @pytest.mark.parametrize(
        "flag",
        ["--password", "--token", "--secret", "--api-key", "--passphrase"],
    )
    def test_the_value_after_a_secret_flag_is_replaced(self, flag):
        assert redact_argv(["helper", flag, "hunter2"]) == ["helper", flag, "<redacted>"]

    @pytest.mark.parametrize(
        "flag",
        ["--password", "--token", "--secret", "--api-key", "--passphrase"],
    )
    def test_the_inline_form_is_replaced(self, flag):
        assert redact_argv(["helper", f"{flag}=hunter2"]) == ["helper", f"{flag}=<redacted>"]

    def test_matching_is_case_insensitive(self):
        assert redact_argv(["helper", "--PASSWORD", "hunter2"]) == ["helper", "--PASSWORD", "<redacted>"]

    def test_a_trailing_secret_flag_with_no_value_is_left_alone(self):
        assert redact_argv(["helper", "--token"]) == ["helper", "--token"]

    def test_an_unrelated_flag_keeps_its_value(self):
        assert redact_argv(["helper", "--timeout", "30"]) == ["helper", "--timeout", "30"]


class TestSshKeyPaths:
    def test_the_key_directory_is_abbreviated_and_the_name_kept(self):
        """Which key was used is diagnostic; where the operator keeps it is not."""
        argv = ["ssh", "-i", "/srv/astinuslab/storage/ssh_keys/astinuslab_ed25519", "user@10.0.0.5", "true"]

        assert redact_argv(argv) == [
            "ssh",
            "-i",
            "<ssh_keys>/astinuslab_ed25519",
            "user@10.0.0.5",
            "true",
        ]

    def test_a_key_outside_the_keystore_is_still_abbreviated(self):
        assert redact_argv(["scp", "-i", "/home/someone/.ssh/id_rsa", "a", "b"]) == [
            "scp",
            "-i",
            "<key>/id_rsa",
            "a",
            "b",
        ]

    def test_an_unrelated_dash_i_value_is_not_treated_as_a_key(self):
        """`-i` is a key only for ssh-family commands; qemu-img uses it for a format."""
        assert redact_argv(["qemu-img", "convert", "-i", "2", "src", "dst"]) == [
            "qemu-img",
            "convert",
            "-i",
            "2",
            "src",
            "dst",
        ]


class TestPreservedDiagnostics:
    def test_an_ordinary_helper_call_is_unchanged(self):
        argv = ["sudo", "-n", "/opt/astinuslab/ops/host/check_job_vm.sh", "astinuslab-job-abc"]

        assert redact_argv(argv) == argv

    def test_the_guest_address_and_script_name_survive(self):
        argv = ["ssh", "-i", "/s/ssh_keys/k", "-o", "BatchMode=yes", "ubuntu@192.168.122.31", "true"]

        assert "ubuntu@192.168.122.31" in redact_argv(argv)
        assert "BatchMode=yes" in redact_argv(argv)

    def test_redaction_does_not_mutate_the_caller_list(self):
        argv = ["helper", "--token", "hunter2"]
        redact_argv(argv)

        assert argv == ["helper", "--token", "hunter2"]

    def test_an_empty_command_is_handled(self):
        assert redact_argv([]) == []
        assert redacted_command([]) == ""


class TestRenderedForm:
    def test_it_renders_the_redacted_argv_as_one_line(self):
        assert redacted_command(["helper", "--token", "hunter2"]) == "helper --token <redacted>"


class TestCallSites:
    def test_timeout_classification_reads_the_unredacted_command(self):
        """Redaction must not change which timeout class a command lands in."""
        from astinuslab.virtualization.process_support import timeout_for

        argv = ["ssh", "-i", "/s/ssh_keys/k", "--token", "abc", "host", "true"]

        assert timeout_for(argv) == 3600.0

    def test_a_failure_message_carries_the_redacted_command(self, monkeypatch, tmp_path):
        """The message becomes HostAction.error_summary and is shown in the UI."""
        import subprocess as subprocess_module

        from astinuslab.virtualization import process_support

        script = tmp_path / "slow.sh"
        script.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
        script.chmod(0o755)

        class _Logger:
            def __init__(self):
                self.lines: list[str] = []

            def _record(self, message, *args, **kwargs):
                self.lines.append(message % args if args else message)

            debug = info = warning = error = exception = _record

        logger = _Logger()
        with pytest.raises((RuntimeError, subprocess_module.CalledProcessError)) as excinfo:
            process_support.run_command(
                logger,
                _null_scope,
                None,
                {},
                {},
                _NullLock(),
                [str(script), "--token", "hunter2"],
                timeout=0.5,
                storage_dir=tmp_path,
            )

        assert "hunter2" not in str(excinfo.value)
        assert "<redacted>" in str(excinfo.value)
        assert not [line for line in logger.lines if "hunter2" in line]
