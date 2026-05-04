from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from codexproxy.spend_tracker import SpendTracker, TokenUsage


class SpendTrackerTests(unittest.TestCase):
    def test_record_usage_accumulates_total_and_client_usd(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tracker = SpendTracker(Path(temp_dir))

            cost = tracker.record_usage(
                client_name="client-1",
                model_name="gpt-5.5",
                usage=TokenUsage(input_tokens=1000, cached_input_tokens=200, output_tokens=500),
            )

            self.assertEqual(str(cost), "0.019100")
            status = tracker.get_status("client-1")
            self.assertEqual(status.date_text, date.today().isoformat())
            self.assertEqual(str(status.client_usd), "0.019100")
            self.assertEqual(str(status.total_usd), "0.019100")

            payload = json.loads(
                (Path(temp_dir) / "cache" / "daily-spend.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["clients"]["client-1"], "0.019100")

    def test_unknown_model_uses_zero_pricing_from_other(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tracker = SpendTracker(Path(temp_dir))

            cost = tracker.record_usage(
                client_name="client-1",
                model_name="unknown-model",
                usage=TokenUsage(input_tokens=1000, cached_input_tokens=0, output_tokens=500),
            )

            self.assertEqual(str(cost), "0")
            status = tracker.get_status("client-1")
            self.assertEqual(str(status.client_usd), "0")
