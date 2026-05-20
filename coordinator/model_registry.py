"""Catalog of models the coordinator knows about, plus validation helpers.

This is intentionally a static, in-process registry — small, easy to
read, easy to test. It lets the coordinator:

* Tell customers which models exist and roughly what they cost
  (``/models`` endpoint).
* Surface what each worker has advertised running (in ``/workers``).
* Optionally reject ``/generate`` calls for models we don't know about
  (``STRICT_MODELS=true`` env flag, default off so tests and local dev
  keep working with arbitrary model names).

When this graduates to a real database-backed registry it can keep the
same public surface — ``is_known``, ``get``, ``list_all`` — without any
caller changes.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Model:
    name: str
    family: str
    params_b: float       # total parameters in billions
    active_b: float       # active per-token parameters (= params_b for dense)
    min_vram_gb: float    # rough minimum VRAM at INT4 quantization
    license: str
    notes: str = ""
    # "chat" (LLM) or "image" (diffusion). Most callers don't care, but
    # routing + tool-aware validation does — an image model on a /generate
    # call with tool="chat" should be rejected by STRICT_MODELS=true.
    kind: str = "chat"

    @property
    def is_moe(self) -> bool:
        return self.active_b < self.params_b

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_moe"] = self.is_moe
        return d


# Curated catalog. Add models as we add support for them. Numbers are
# pragmatic estimates, not contractual.
_CATALOG: dict[str, Model] = {
    m.name: m
    for m in (
        Model("mock",         "mock",     0.0,   0.0,   0.0,  "Apache-2.0", "test/dev only"),
        Model("llama3.2:1b",  "llama-3",  1.2,   1.2,   2.0,  "Llama-3-Community"),
        Model("llama3.2:3b",  "llama-3",  3.2,   3.2,   3.0,  "Llama-3-Community"),
        Model("llama3.1:8b",  "llama-3",  8.0,   8.0,   6.0,  "Llama-3-Community"),
        Model("llama3.1:70b", "llama-3",  70.0,  70.0,  40.0, "Llama-3-Community"),
        Model("mistral:7b",   "mistral",  7.3,   7.3,   6.0,  "Apache-2.0"),
        Model("mixtral:8x7b", "mixtral",  46.7,  12.9,  24.0, "Apache-2.0"),
        Model("deepseek-v3",  "deepseek", 671.0, 37.0,  20.0, "MIT-style", "MoE; 8 of 256 experts active"),
        Model("qwen2.5:7b",   "qwen",     7.6,   7.6,   6.0,  "Apache-2.0"),
        Model("phi3:14b",     "phi",      14.0,  14.0,  10.0, "MIT"),
        # Image-generation models. params_b is the unet+text-encoder
        # parameter count (estimate); min_vram_gb is the rough Q4 GGUF
        # requirement for 512×512 generation. Defaults shipped with the
        # MVP — the small one. SDXL is registered but the mirror only
        # serves it once we promote it (see infra/setup-image-mirror.sh).
        Model("sd1.5",  "stable-diffusion-1.5", 0.86, 0.86, 4.0,
              "CreativeML Open RAIL-M",
              "Q4_0 GGUF; mirror default; 512x512 in ~5-15s on a recent GPU",
              kind="image"),
        Model("sdxl",   "stable-diffusion-xl",  3.5,  3.5,  8.0,
              "CreativeML Open RAIL++-M",
              "1024x1024; high-end contributors only; not yet on the mirror",
              kind="image"),
    )
}


def _strict_default() -> bool:
    return os.getenv("STRICT_MODELS", "").lower() in ("1", "true", "yes", "on")


def is_known(name: str | None) -> bool:
    """True if the model name is in the catalog. None / empty is also
    'known' so callers that don't care about the model still pass."""
    if not name:
        return True
    return name in _CATALOG


def get(name: str) -> Model | None:
    return _CATALOG.get(name)


def list_all() -> list[Model]:
    return list(_CATALOG.values())


# Default image model — what /generate falls back to when tool="image"
# arrives with no `model` field. Single source of truth so the agent
# bootstrap and the coordinator stay in sync.
DEFAULT_IMAGE_MODEL = "sd1.5"


def is_image_model(name: str | None) -> bool:
    """True if *name* is registered as an image model. Unknown names
    return False (so an undeclared name on a chat job doesn't get
    accidentally treated as an image)."""
    if not name:
        return False
    m = _CATALOG.get(name)
    return m is not None and m.kind == "image"


def validate_or_raise(name: str | None, *, strict: bool | None = None) -> None:
    """Raise ValueError if *name* is unknown and strict mode is on.

    Strict mode defaults to the ``STRICT_MODELS`` env var. Callers can
    override with an explicit ``strict=`` argument (used in tests).
    """
    enforce = _strict_default() if strict is None else bool(strict)
    if enforce and not is_known(name):
        raise ValueError(f"unknown model: {name!r}")
