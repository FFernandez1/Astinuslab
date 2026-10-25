from __future__ import annotations

from .base import KernelMetadata
from .providers import get_provider, is_supported


def describe_fetch(metadata: KernelMetadata) -> dict:
    """Return a dict describing how this kernel would be fetched (for logging/debugging)."""
    provider = get_provider(metadata)
    return {
        "distro": metadata.distro,
        "kernel_release": metadata.kernel_release,
        "package_name": provider.package_name(metadata),
        "fetch_url": provider.fetch_url(metadata),
        "supported": is_supported(metadata.distro),
    }
