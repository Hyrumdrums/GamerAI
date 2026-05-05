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


def validate_or_raise(name: str | None, *, strict: bool | None = None) -> None:
    """Raise ValueError if *name* is unknown and strict mode is on.

    Strict mode defaults to the ``STRICT_MODELS`` env var. Callers can
    override with an explicit ``strict=`` argument (used in tests).
    """
    enforce = _strict_default() if strict is None else bool(strict)
    if enforce and not is_known(name):
        raise ValueError(f"unknown model: {name!r}")
