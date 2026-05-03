import json
import unittest
from pathlib import Path

from codexproxy.debug_record import format_debug_record_summary


class DebugRecordTests(unittest.TestCase):
    def test_terminal_summary_truncates_each_string_field_to_500_words(self) -> None:
        long_text = " ".join(f"word{i}" for i in range(600))
        record = {
            "downstream_request": {
                "headers": {"X-Long": long_text},
                "body": long_text,
            },
            "upstream_request": {
                "headers": {"X-Long": long_text},
                "body": long_text,
            },
        }

        summary = format_debug_record_summary(record, file_path=Path("/tmp/test.json"))
        payload = json.loads(summary[len("RECORD "):])
        truncated_header = payload["record"]["downstream_request"]["headers"]["X-Long"]
        truncated_body = payload["record"]["downstream_request"]["body"]

        self.assertGreater(len(truncated_header.split()), 500)
        self.assertGreater(len(truncated_body.split()), 500)
        self.assertIn("...(truncated 100 words)", truncated_header)
        self.assertIn("...(truncated 100 words)", truncated_body)
