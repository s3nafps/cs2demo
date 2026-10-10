"""Recording backends. Each one turns a plan into per-clip recordings."""

from .base import CaptureBackend, CaptureCancelled, CaptureResult, CaptureSettings
from .hlae import HlaeBackend, HlaeConfig

__all__ = [
    "CaptureBackend",
    "CaptureCancelled",
    "CaptureResult",
    "CaptureSettings",
    "HlaeBackend",
    "HlaeConfig",
]
