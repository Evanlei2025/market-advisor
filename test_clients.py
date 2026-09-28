"""Synthetic offline compatibility tests for the retired portfolio schema."""
import json
import unittest
from pathlib import Path

import main


class ClientCompatibilityTests(unittest.TestCase):
    def test_empty_legacy_storage_schemas(self):
        root = Path(__file__).resolve().parent / "knowledge_base"
        for name, expected in (("traces.json", []), ("state_history.json", []),
                               ("recommendations.json", []), ("manager_snapshots.json", {})):
            with self.subTest(name=name):
                self.assertEqual(json.loads((root / name).read_text(encoding="utf-8")), expected)

    def test_synthetic_legacy_clients_remain_isolated(self):
        config = {"target": {"equity": {"base": 0.4}}, "products": [], "holdings": [],
                  "clients": {"sample_a": {"display_name": "Sample A", "products": [{"code": "000001"}]},
                              "sample_b": {"display_name": "Sample B", "products": []}}}
        clients = main.get_clients(config)
        self.assertEqual([item["id"] for item in clients], ["sample_a", "sample_b"])
        self.assertEqual(clients[0]["cfg"]["products"], [{"code": "000001"}])
        self.assertEqual(clients[1]["cfg"]["products"], [])

    def test_deep_merge_does_not_mutate_inputs(self):
        base = {"a": {"x": 1, "y": 2}, "list": [1]}
        actual = main.deep_merge(base, {"a": {"y": 3}, "list": [2]})
        self.assertEqual(actual, {"a": {"x": 1, "y": 3}, "list": [2]})
        self.assertEqual(base, {"a": {"x": 1, "y": 2}, "list": [1]})


if __name__ == "__main__":
    unittest.main()
