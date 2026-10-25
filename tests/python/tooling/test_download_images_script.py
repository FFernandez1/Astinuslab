import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [pytest.mark.unit, pytest.mark.host]

SCRIPT = ROOT / "ops/images/download_images.sh"
METADATA = ROOT / "ops/images/image_download_metadata.sh"
CATALOG = ROOT / "config/catalog/targets.example.yaml"


def test_download_size_metadata_is_shared_and_canonical():
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(METADATA))}; printf '%s %s\n' "
            '"$ASTINUSLAB_IMAGE_DOWNLOAD_APPROX_GB" "$ASTINUSLAB_IMAGE_DOWNLOAD_MIN_FREE_GB"',
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3 10"
    downloader = SCRIPT.read_text(encoding="utf-8")
    installer = (ROOT / "ops/deploy/install.sh").read_text(encoding="utf-8")
    assert "image_download_metadata.sh" in downloader
    assert "image_download_metadata.sh" in installer
    assert "ASTINUSLAB_IMAGE_DOWNLOAD_MIN_FREE_GB" in downloader


def _fetched_urls() -> list[str]:
    return re.findall(r'^fetch\s+"[^"]*"\s+"(https://[^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)


def _catalog_image_refs() -> set[str]:
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return {target["image"]["image_ref"] for target in catalog["targets"]}


def test_download_script_fetches_every_url_it_declares():
    urls = _fetched_urls()

    assert len(urls) == 4, f"expected four image downloads, parsed {len(urls)}: {urls}"


def test_downloaded_filenames_match_catalog_image_refs():
    """wget -N -P saves under the *remote* filename and targets resolve images by
    filename, so an upstream rename silently leaves the catalog unsatisfiable —
    the download succeeds and every job still fails with a missing image."""
    image_refs = _catalog_image_refs()

    for url in _fetched_urls():
        filename = url.rsplit("/", 1)[-1]
        assert filename in image_refs, (
            f"{filename} (from {url}) matches no image_ref in {CATALOG.name}; known refs: {sorted(image_refs)}"
        )


def test_download_script_saves_under_the_remote_filename():
    """The filename invariant above only holds while fetch() lets wget name the
    file. Switching to -O would decouple them and must update that test too."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'wget -N --progress=bar:force:noscroll -P "$DEST_DIR" "$url"' in script
    assert "wget -O" not in script


def _resolved_destination(script_dir: Path, *, env=None) -> str:
    """Run the script with fetch() stubbed out and report where it would save."""
    program = f"""
    set -uo pipefail
    wget() {{ :; }}
    source {shlex.quote(str(script_dir / "download_images.sh"))} >/dev/null 2>&1
    printf '%s\\n' "$DEST_DIR"
    """
    merged = os.environ.copy()
    merged.pop("ASTINUSLAB_DATA_DIR", None)
    if env:
        merged.update(env)
    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, env=merged)
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr


def _installed_layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "opt"
    release = root / "releases" / "1.0.0-abcdef012345"
    image_ops = release / "ops" / "images"
    image_ops.mkdir(parents=True)
    shutil.copy(SCRIPT, image_ops / SCRIPT.name)
    if METADATA.exists():
        shutil.copy(METADATA, image_ops / METADATA.name)
    (root / "current").symlink_to(release)
    (root / "images" / "definitions").mkdir(parents=True)
    return root, release


def test_downloads_land_in_the_install_roots_image_tree_not_the_release(tmp_path):
    """Images are mutable state owned by the install root, while this script
    ships inside the immutable release. Saving beside the script fills a tree no
    service reads and the next upgrade discards."""
    root, _release = _installed_layout(tmp_path)

    destination = _resolved_destination(root / "current" / "ops" / "images")

    assert destination == str(root / "images" / "definitions"), destination


def test_a_development_checkout_still_downloads_beside_itself(tmp_path):
    tree = tmp_path / "checkout"
    image_ops = tree / "ops" / "images"
    image_ops.mkdir(parents=True)
    shutil.copy(SCRIPT, image_ops / SCRIPT.name)
    if METADATA.exists():
        shutil.copy(METADATA, image_ops / METADATA.name)

    destination = _resolved_destination(image_ops)

    assert destination == str(tree / "images" / "definitions"), destination


def test_an_explicit_destination_still_wins(tmp_path):
    tree = tmp_path / "checkout"
    image_ops = tree / "ops" / "images"
    image_ops.mkdir(parents=True)
    shutil.copy(SCRIPT, image_ops / SCRIPT.name)
    if METADATA.exists():
        shutil.copy(METADATA, image_ops / METADATA.name)
    chosen = tmp_path / "elsewhere"

    program = f"""
    set -uo pipefail
    wget() {{ :; }}
    source {shlex.quote(str(image_ops / SCRIPT.name))} {shlex.quote(str(chosen))} >/dev/null 2>&1
    printf '%s\\n' "$DEST_DIR"
    """
    result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)

    assert result.stdout.strip().splitlines()[-1] == str(chosen)


@pytest.mark.parametrize("suffix", ["", ".ca", ".es", ".fr"])
def test_localized_runbooks_use_the_installed_release_downloader(suffix):
    runbook = ROOT / "docs" / "usage" / f"running-locally{suffix}.md"
    text = runbook.read_text(encoding="utf-8")

    assert "/opt/astinuslab/current/ops/images/download_images.sh" in text
    assert "cd /opt/astinuslab/current" in text
    assert 'sudo -u "$USER" ./ops/images/download_images.sh' in text
    assert "sudo -u astinuslab-runner ./ops/images/download_images.sh" in text
    assert "--split-users" in text
    assert "/opt/astinuslab/ops/images/download_images.sh" not in text
