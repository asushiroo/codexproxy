from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from codexproxy.model_costs import get_model_cost, load_model_costs


class ModelCostsTests(unittest.TestCase):
    def test_load_model_costs_prefers_config_directory_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            model_costs_path = Path(temp_dir) / "model-costs.json"
            model_costs_path.write_text(
                json.dumps({"gpt-5.5": 5, "other": 2}),
                encoding="utf-8",
            )

            model_costs = load_model_costs(Path(temp_dir))

            self.assertEqual(model_costs["gpt-5.5"], 5)
            self.assertEqual(model_costs["other"], 2)

    def test_get_model_cost_uses_other_fallback(self) -> None:
        model_costs = {"gpt-5.5": 3, "other": 1}

        self.assertEqual(get_model_cost("gpt-5.5", model_costs), 3)
        self.assertEqual(get_model_cost("gpt-4.1", model_costs), 1)
        self.assertEqual(get_model_cost(None, model_costs), 1)
