import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops" / "host" / "render_virtiofs_xml.sh"


def test_render_emits_both_virtiofs_and_9p(tmp_path):
    job_id = "test-job-render-xml"
    staging = REPO_ROOT / "storage" / "tmp" / job_id / "virtiofs-staging"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), job_id],
            capture_output=True,
            text=True,
            check=True,
        )
        xml_path_line = next(line for line in result.stdout.splitlines() if line.startswith("xml_path="))
        xml_path = Path(xml_path_line.split("=", 1)[1])
        xml = xml_path.read_text()

        assert "<driver type='virtiofs'/>" in xml, xml
        assert xml.count("<filesystem") >= 2, f"expected two filesystems, got: {xml}"
        # Both must point at the same staging dir
        assert xml.count("astinuslab_out") >= 2, xml
        # 9p tag uses the _9p suffix to disambiguate from virtiofs tag
        assert "astinuslab_out_9p" in xml, xml
    finally:
        # Clean up the staging dir + parent + rendered xml
        rendered = staging.parent / f"virtiofs-{job_id}.xml"
        if rendered.exists():
            rendered.unlink()
        shutil.rmtree(staging.parent, ignore_errors=True)
