import unittest

from codexproxy.state import ClientBinding
from codexproxy.usage_page import render_usage_page


class UsagePageTests(unittest.TestCase):
    def test_usage_page_rounds_decimal_counts_for_display(self) -> None:
        binding = ClientBinding(
            name="client-a",
            base_url="https://example.invalid/v1",
            upstream_api_key="shared-upstream-key",
            limit=300,
            count=1.6,
        )

        html = render_usage_page(binding)

        self.assertIn("298 / 300", html)
        self.assertIn("2 / 300", html)
        self.assertIn('data-progress="0.53"', html)
