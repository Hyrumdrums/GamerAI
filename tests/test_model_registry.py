"""Tests for ``coordinator.model_registry``.

Covers the catalog shape, the strict-mode validation gate, and the MoE
flag derivation. Pure, no Redis or HTTP.
"""
from __future__ import annotations

import unittest

from coordinator import model_registry


class CatalogShapeTests(unittest.TestCase):
    def test_catalog_is_non_empty(self):
        self.assertGreater(len(model_registry.list_all()), 0)

    def test_each_entry_has_required_fields(self):
        for m in model_registry.list_all():
            self.assertTrue(m.name)
            self.assertTrue(m.family)
            self.assertGreaterEqual(m.params_b, 0)
            self.assertGreaterEqual(m.active_b, 0)
            self.assertLessEqual(m.active_b, m.params_b)
            self.assertGreaterEqual(m.min_vram_gb, 0)
            self.assertTrue(m.license)

    def test_to_dict_includes_is_moe(self):
        deepseek = model_registry.get("deepseek-v3")
        self.assertIsNotNone(deepseek)
        d = deepseek.to_dict()
        self.assertIn("is_moe", d)
        self.assertTrue(d["is_moe"])

    def test_dense_model_is_not_moe(self):
        m = model_registry.get("llama3.1:8b")
        self.assertIsNotNone(m)
        self.assertFalse(m.is_moe)


class IsKnownTests(unittest.TestCase):
    def test_known_returns_true(self):
        self.assertTrue(model_registry.is_known("llama3.2:1b"))
        self.assertTrue(model_registry.is_known("mock"))

    def test_unknown_returns_false(self):
        self.assertFalse(model_registry.is_known("totally-fake-model"))

    def test_none_or_empty_treated_as_known(self):
        self.assertTrue(model_registry.is_known(None))
        self.assertTrue(model_registry.is_known(""))


class ValidateOrRaiseTests(unittest.TestCase):
    def test_strict_off_accepts_unknown(self):
        # No exception raised.
        model_registry.validate_or_raise("totally-fake", strict=False)

    def test_strict_on_rejects_unknown(self):
        with self.assertRaises(ValueError):
            model_registry.validate_or_raise("totally-fake", strict=True)

    def test_strict_on_accepts_known(self):
        model_registry.validate_or_raise("llama3.2:1b", strict=True)

    def test_strict_on_accepts_none(self):
        # An unspecified model is allowed even in strict mode — caller
        # opts in by passing a name.
        model_registry.validate_or_raise(None, strict=True)


if __name__ == "__main__":
    unittest.main()
