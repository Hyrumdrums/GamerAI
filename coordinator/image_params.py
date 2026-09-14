"""Pure image-parameter helpers used by ``/generate`` and ``/jobs/complete``
— negative-prompt combination, dimension clamping, PNG IHDR parsing, and
the pixel-area quota multiplier. No DB/Redis coupling, so this is a plain
import (not a ``build_router`` closure) wherever it's needed.
"""
from __future__ import annotations

import os
from typing import Optional

# sd.cpp requires image dims to be multiples of 64. We clamp into
# [256, 1536] so a client can't ask the worker to render a 16K canvas.
_IMAGE_DIM_MIN = 256
_IMAGE_DIM_MAX = 1536


# Always-on negative prompt forced onto every image job. Layer-1
# mitigation for the case the v1.1.24 incident report flagged:
# "obese cow" → topless woman wearing cow ears. DreamShaper-class
# SD1.5 models are happy to drift toward nudity when the prompt is
# vague; biasing the sampler away with explicit negatives catches
# ~90% of the accidental-nudity case at zero infrastructure cost.
# Layer-2 is NudeNet on the output; see _classify_image_or_filter.
# Env-tunable so production can swap phrasing without a redeploy.
_FORCED_NEGATIVE_PROMPT = os.getenv(
    "FORCED_NEGATIVE_PROMPT",
    "nsfw, nude, naked, topless, partially nude, bare skin, "
    "sexual, sexually suggestive, explicit, lingerie, underwear",
)


def _combine_negative_prompt(user_negative: Optional[str]) -> str:
    """Layer the forced SFW phrases in FRONT of whatever the user
    asked to negate so the sampler sees them with full weight. Empty
    user input degenerates to just the forced prefix; missing forced
    prefix (env override to '') degenerates to user input unchanged."""
    user_negative = (user_negative or "").strip()
    forced = (_FORCED_NEGATIVE_PROMPT or "").strip()
    if not forced:
        return user_negative
    if not user_negative:
        return forced
    return f"{forced}, {user_negative}"


def _clamp_image_dim(value: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 512
    v = max(_IMAGE_DIM_MIN, min(_IMAGE_DIM_MAX, v))
    return (v // 64) * 64 or _IMAGE_DIM_MIN


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Parse (width, height) from a PNG IHDR. Layout: 8-byte signature
    + 4-byte chunk length + 4-byte 'IHDR' + 4-byte width BE + 4-byte
    height BE. Returns (0, 0) on a buffer too short to hold the chunk
    — callers treat that as 'unknown' and fall back to the smallest
    cost bucket so an oddball worker output never auto-bills 4×."""
    if len(data) < 24:
        return (0, 0)
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def image_cost_multiplier(width: int, height: int) -> float:
    """Quota multiplier for an image of (*width*, *height*). Three
    buckets aligned with the composer's small/medium/large radios:

    - small  (≤ 512²)             → 1×
    - medium (≤ 768²)             → 2×
    - large  (anything bigger,
      including SDXL-native 1024²) → 4×

    Compute is roughly proportional to pixel area on diffusion models,
    so the factors approximate GPU work while keeping the displayed
    cost an integer the UI can render as 'costs N image-credits'.
    Unknown / zero dims fall through to 1× — fair-by-default for
    edge cases like a sub-256 canary."""
    area = max(int(width), 0) * max(int(height), 0)
    if area <= 0 or area <= 512 * 512:
        return 1.0
    if area <= 768 * 768:
        return 2.0
    return 4.0


def _default_image_params():
    """Wraps ImageParams() so the import lives at the top of the file
    only — keeps the /generate body short."""
    from shared.models import ImageParams
    return ImageParams()
