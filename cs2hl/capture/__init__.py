"""Recording backends. Each one turns a plan into per-segment recordings."""

from .base import (
    CaptureBackend,
    CaptureCancelled,
    CaptureError,
    CaptureResult,
    CaptureSettings,
    clip_segments,
    segment_file_name,
)
from .hlae import HlaeBackend, HlaeConfig

__all__ = [
    "CaptureBackend",
    "CaptureCancelled",
    "CaptureError",
    "CaptureResult",
    "CaptureSettings",
    "HlaeBackend",
    "HlaeConfig",
    "clip_segments",
    "segment_file_name",
]
