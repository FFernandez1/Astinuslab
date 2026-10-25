from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class DiskCapture:
    def __init__(self, *, run_command, mode: str = "pause_convert", libvirt_uri: str = "qemu:///system") -> None:
        if mode not in ("pause_convert", "blockcopy"):
            raise ValueError(f"unknown disk capture mode: {mode}")
        self._run = run_command
        self._mode = mode
        self._libvirt_uri = libvirt_uri

    def capture(
        self, *, domain_name: str, source_disk_path: str | None = None, source_disk: str | None = None, output_path
    ) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if self._mode == "pause_convert":
            if source_disk_path is None:
                raise ValueError("pause_convert mode requires source_disk_path")
            self._capture_pause_convert(domain_name, source_disk_path, str(output_path))
        else:
            if source_disk is None:
                source_disk = "vda"
            self._capture_blockcopy(domain_name, source_disk, str(output_path))

    def capture_clean_layer(self, *, domain_name: str, source_disk_path: str, output_path) -> Path:
        """Capture one consistent post-verification disk layer.

        This named operation keeps creator-side clean-run code on the existing
        pause/convert path while making the reproducibility boundary explicit.
        The returned path is still caller-owned; content-addressed publication
        belongs to the package export service.
        """

        self.capture(domain_name=domain_name, source_disk_path=source_disk_path, output_path=output_path)
        return Path(output_path)

    def _capture_pause_convert(self, domain_name: str, source_path: str, output_path: str) -> None:
        self._run(["virsh", "-c", self._libvirt_uri, "suspend", domain_name])
        convert_exc: Exception | None = None
        try:
            # -U (force-share): a suspended qemu still holds the qcow2 write
            # lock, so a plain open fails with "Failed to get shared write
            # lock". The domain is paused for the duration of the copy, so a
            # shared read is consistent.
            self._run(["qemu-img", "convert", "-U", "-O", "qcow2", source_path, output_path])
        except Exception as exc:
            convert_exc = exc
        try:
            self._run(["virsh", "-c", self._libvirt_uri, "resume", domain_name])
        except Exception as resume_exc:
            logger.exception("disk capture: resume failed for %s", domain_name)
            if convert_exc is None:
                raise
            raise resume_exc from convert_exc
        if convert_exc is not None:
            raise convert_exc

    def _capture_blockcopy(self, domain_name: str, source_disk: str, output_path: str) -> None:
        self._run(
            [
                "virsh",
                "-c",
                self._libvirt_uri,
                "blockcopy",
                domain_name,
                source_disk,
                output_path,
                "--wait",
                "--verbose",
                "--format",
                "qcow2",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self._run(
            ["virsh", "-c", self._libvirt_uri, "blockjob", domain_name, source_disk, "--pivot"],
            check=True,
            capture_output=True,
            text=True,
        )
