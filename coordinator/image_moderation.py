"""Image directory resolution, the submit-time prompt denylist, and the
NudeNet output classifier (layer 2). No DB/Redis coupling, so this is a
plain import (not a ``build_router`` closure) wherever it's needed —
``coordinator/main.py``'s ``/generate``, ``/jobs/complete``, and
``/images/{name}`` handlers all import from here directly.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from coordinator.image_params import _png_dimensions

log = logging.getLogger("coordinator.image_moderation")


# Where generated images are written. Lives next to the SQLite DB so
# it shares the same volume — one mount, one backup, one rotation
# policy. Filename is {job_id}.png so the messages.image_path =
# "{job_id}.png" suffix stays portable across deploys (no absolute
# path baked into the DB).
#
# IMAGE_DIR resolves env var > sibling-of-DB_PATH > /data/images,
# in that order. Test suites that override DB_PATH to a tmp dir
# therefore get a writable images dir for free.
def _resolve_image_dir() -> Path:
    explicit = os.getenv("IMAGE_DIR")
    if explicit:
        return Path(explicit)
    from shared.config import DB_PATH as _DB_PATH
    db_parent = Path(_DB_PATH).parent
    if db_parent and str(db_parent) not in ("", "."):
        return db_parent / "images"
    return Path("/data/images")


IMAGE_DIR = _resolve_image_dir()
try:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Fall back to a tmp dir if the configured path isn't writable
    # (e.g. a developer running the coordinator outside Docker with
    # no /data volume). Image features still work; persistence is
    # lost across process restarts.
    import tempfile
    IMAGE_DIR = Path(tempfile.mkdtemp(prefix="gamerai-images-"))

# Cap on accepted PNG size. 8 MB lets a 1024×1024 image through with
# headroom; anything bigger is likely a misbehaving (or malicious)
# worker. ``image_b64`` arrives base64-encoded so the wire payload is
# ~4/3 of this.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Lightweight prompt-side denylist. NOT a content moderation system —
# this just refuses the most obviously banned categories at submit time
# so a contributor's machine never has to run them. Phase 3b+ ships a
# real classifier (image-side); for now we keep the surface small and
# explicit so we can point at it during incident review.
_IMAGE_PROMPT_DENYLIST = re.compile(
    r"\b("
    r"csam|child(?:\s+|-)porn|cp\b|"
    r"loli(?:con)?|shota|underage\b|minor\b|prepubescent|"
    r"bestiality|zoophilia|"
    r"non[- ]?consensual|rape\b"
    r")\b",
    re.IGNORECASE,
)


def _image_prompt_is_blocked(prompt: str) -> Optional[str]:
    """Return the matched denylist phrase if the prompt should be
    refused, else None. Surface the phrase so the user sees a concrete
    reason — vague 'rejected for content' is hostile and hides bugs."""
    m = _IMAGE_PROMPT_DENYLIST.search(prompt or "")
    return m.group(0) if m else None


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"


# ---------- NSFW output classifier (layer 2) ----------
# The forced negative prompt (layer 1) catches most accidental nudity
# from vague prompts, but DreamShaper can still produce explicit
# output. NudeNet runs after generation as a hard gate: any detection
# of the explicit-anatomy classes above NSFW_THRESHOLD turns the job
# into a friendly "image filtered by content policy" error bubble.
# Soft-fails when the package isn't installed (dev/test environments
# without the docker layer) so the rest of the code path is unaffected.
_NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.5"))
_NSFW_BLOCKED_CLASSES = frozenset({
    # NudeNet v3 class names. The bar here is "family-friendly" — a
    # 6-year-old should be able to look over the requester's shoulder
    # without surprises. That means we go past the just-genitalia
    # set into shirtlessness and bare-midriff territory:
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
    "MALE_BREAST_EXPOSED",   # shirtless males — not family-friendly default
    "BELLY_EXPOSED",         # bare midriff / crop-top — same standard
    # COVERED variants (FEMALE_BREAST_COVERED, BUTTOCKS_COVERED, etc.)
    # are deliberately NOT in the block set. NudeNet fires those on
    # any clothed body part visible through fabric — adding them
    # would block ~80% of normal pictures of people. Other EXPOSED
    # categories (FEET_EXPOSED, ARMPITS_EXPOSED) are normal family
    # content (sandals, sleeveless shirts) and stay permitted.
})
_nudenet_detector = None  # lazy-loaded singleton
_nudenet_load_attempted = False


def _get_nudenet():
    """Lazy-load the NudeNet detector once. Returns None if the package
    isn't installed (dev/test). Failure is logged once, then silently
    skipped on every subsequent call so a missing dep doesn't break
    image jobs — the system stays usable, just without the safety
    net, and operators see the warning."""
    global _nudenet_detector, _nudenet_load_attempted
    if _nudenet_load_attempted:
        return _nudenet_detector or None
    _nudenet_load_attempted = True
    try:
        from nudenet import NudeDetector  # type: ignore
        _nudenet_detector = NudeDetector()
        log.info(
            "NudeNet classifier loaded (threshold=%.2f)",
            _NSFW_THRESHOLD,
            extra={"event": "nudenet_loaded"},
        )
    except Exception as e:
        log.warning(
            "NudeNet unavailable — image NSFW classifier disabled: %s",
            e,
            extra={"event": "nudenet_unavailable"},
        )
        _nudenet_detector = None
    return _nudenet_detector


class _NSFWFilteredError(Exception):
    """Image was rejected by the output classifier. Raised from inside
    _save_image_or_raise so the existing /jobs/complete catch-all
    surfaces it as image_save_error → friendly error bubble + retry
    button + worker doesn't get credited (the existing flow for any
    image save failure). Distinct class so we can identify the
    refusal in logs vs. a malformed-bytes / disk-full failure."""


def _classify_image_or_raise(png_bytes: bytes, job_id: str) -> None:
    """Run NudeNet on the bytes; raise _NSFWFilteredError if any
    explicit-anatomy class scores above the threshold. No-op (soft
    success) when NudeNet isn't installed — the operator gets a
    one-time warning at startup."""
    detector = _get_nudenet()
    if detector is None:
        return
    # NudeNet's detect() expects a file path. Materialize to a temp
    # file so we don't have to monkey-patch its internals — the bytes
    # are already in memory after base64 decode, so this is one
    # additional disk round-trip per image, ~5ms.
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="nudenet-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)
        try:
            detections = detector.detect(tmp_path)
        except Exception as e:
            # Classifier failure should not block image delivery —
            # log loudly, then let the image through. An attacker
            # can't deliberately trigger this since they don't
            # control the model code path.
            log.warning(
                "NudeNet detect() failed — letting image through: %s", e,
                extra={"event": "nudenet_detect_failed", "job_id": job_id},
            )
            return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    matches = [
        d for d in (detections or [])
        if d.get("class") in _NSFW_BLOCKED_CLASSES
        and float(d.get("score") or 0) >= _NSFW_THRESHOLD
    ]
    if matches:
        log.warning(
            "image filtered by NSFW classifier",
            extra={
                "event": "nsfw_filtered",
                "job_id": job_id,
                "matches": [
                    {"class": m["class"], "score": round(float(m["score"]), 3)}
                    for m in matches
                ],
            },
        )
        raise _NSFWFilteredError(
            "Generated image was filtered by the content classifier. "
            "Please try a different prompt."
        )


def _validate_and_classify_init_image(image_b64: str, job_id: str) -> None:
    """Gate a member-supplied init image for a tool=image edit job
    before it ever reaches a queue: size cap, base64 shape, PNG/JPEG
    magic header, then the same NSFW classifier generated OUTPUT
    images get. An "edit" job hands a contributor's GPU someone
    else's uploaded picture, not just a text prompt — it gets the
    same content gate, not a weaker one. Raises HTTPException on any
    failure."""
    if len(image_b64) > int(MAX_IMAGE_BYTES * 4 / 3) + 16:
        raise HTTPException(
            status_code=413,
            detail=f"init image too large (>{MAX_IMAGE_BYTES} bytes)",
        )
    try:
        data = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"init_image_b64 not valid base64: {e}",
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"init image too large after decode (>{MAX_IMAGE_BYTES} bytes)",
        )
    if not (data.startswith(_PNG_SIGNATURE) or data.startswith(_JPEG_SIGNATURE)):
        raise HTTPException(
            status_code=400,
            detail="init image bytes are not a PNG or JPEG (missing magic header)",
        )
    try:
        _classify_image_or_raise(data, job_id)
    except _NSFWFilteredError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _save_image_or_raise(
    job_id: str, image_b64: Optional[str]
) -> tuple[str, int, int]:
    """Decode the worker-supplied PNG and persist it under IMAGE_DIR.
    Returns ``(basename, width, height)`` — basename is the
    messages.image_path the UI fetches from /images/<basename>;
    width/height come from the PNG's IHDR and are used by
    /jobs/complete to bill the right quota multiplier for the
    rendered size. Raises HTTPException on malformed payloads so
    /jobs/complete responds 400 — the worker should not retry the
    same broken bytes.

    Validates the PNG magic header so a worker can't sneak a JPEG (or
    a raw HTML page) past the route handler. Real moderation (NSFW
    classifier) is post-MVP; this is just shape validation."""
    if not image_b64:
        raise HTTPException(
            status_code=400,
            detail="image job completed without image_b64",
        )
    # Estimated decoded size — base64 overhead is 4/3. Reject before
    # decoding to avoid materializing a 1 GB string in memory.
    if len(image_b64) > int(MAX_IMAGE_BYTES * 4 / 3) + 16:
        raise HTTPException(
            status_code=413,
            detail=f"image too large (>{MAX_IMAGE_BYTES} bytes)",
        )
    try:
        data = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"image_b64 not valid base64: {e}",
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"image too large after decode (>{MAX_IMAGE_BYTES} bytes)",
        )
    if not data.startswith(_PNG_SIGNATURE):
        raise HTTPException(
            status_code=400,
            detail="image bytes are not a PNG (missing magic header)",
        )
    # NSFW classifier runs BEFORE persisting so a blocked image never
    # touches disk. Raises _NSFWFilteredError on a hit; the outer
    # /jobs/complete handler catches that as image_save_error and the
    # UI gets a friendly "filtered" bubble.
    _classify_image_or_raise(data, job_id)
    # job_id is a uuid we generated — safe as a path component, but
    # belt-and-braces against ../ traversal.
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", job_id)
    fname = f"{safe}.png"
    dest = IMAGE_DIR / fname
    tmp = dest.with_suffix(".png.tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    width, height = _png_dimensions(data)
    return fname, width, height
