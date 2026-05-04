from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from codexproxy.model_pricing import get_model_pricing, load_model_pricing


class ModelPricingTests(unittest.TestCase):
    def test_load_model_pricing_prefers_config_directory_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            pricing_path = Path(temp_dir) / "model-pricing.json"
            pricing_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "gpt-5.5": {
                                "input_per_million_usd": 9,
                                "cached_input_per_million_usd": 1,
                                "output_per_million_usd": 99,
                            },
                            "other": {
                                "input_per_million_usd": 0,
                                "cached_input_per_million_usd": 0,
                                "output_per_million_usd": 0,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            pricing_map = load_model_pricing(Path(temp_dir))

            self.assertEqual(str(pricing_map["gpt-5.5"].input_per_million_usd), "9")
            self.assertEqual(str(pricing_map["gpt-5.5"].output_per_million_usd), "99")

    def test_get_model_pricing_uses_other_fallback(self) -> None:
        pricing_map = load_model_pricing()

        self.assertEqual(str(get_model_pricing("gpt-5.5", pricing_map).input_per_million_usd), "5")
        self.assertEqual(str(get_model_pricing("unknown-model", pricing_map).input_per_million_usd), "0")
