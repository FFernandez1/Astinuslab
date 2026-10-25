import json

from astinuslab.forensics.isf_diagnostics import write_diagnostic_bundle


def test_bundle_writes_all_files(tmp_path):
    serial_log = tmp_path / "serial.log"
    serial_log.write_text("\n".join(f"line {i}" for i in range(500)))

    def fake_dumpxml(domain_name):
        return f"<domain name='{domain_name}'/>"

    bundle_dir = write_diagnostic_bundle(
        run_dir=tmp_path / "run",
        target="ubuntu-20.04",
        domain_name="astinuslab-job-7",
        error=RuntimeError("guest IP unresolved"),
        serial_log_path=serial_log,
        preflight_info={"rpm2cpio": "/usr/bin/rpm2cpio"},
        dumpxml=fake_dumpxml,
    )

    assert bundle_dir.is_dir()
    serial_text = (bundle_dir / "serial.log").read_text()
    assert serial_text.count("\n") < 500  # truncated
    assert "line 499" in serial_text  # tail kept
    assert "<domain" in (bundle_dir / "libvirt-domain.xml").read_text()
    info = json.loads((bundle_dir / "preflight.json").read_text())
    assert info["rpm2cpio"] == "/usr/bin/rpm2cpio"
    error_text = (bundle_dir / "error.txt").read_text()
    assert "guest IP unresolved" in error_text


def test_bundle_handles_missing_serial_log(tmp_path):
    bundle_dir = write_diagnostic_bundle(
        run_dir=tmp_path / "run",
        target="fedora-30",
        domain_name=None,
        error=RuntimeError("mount failed"),
        serial_log_path=tmp_path / "nonexistent.log",
        preflight_info={},
        dumpxml=lambda _: "",
    )
    assert (bundle_dir / "serial.log").read_text() == "(no serial log captured)\n"
    assert (bundle_dir / "libvirt-domain.xml").read_text() == "(no domain available)\n"
