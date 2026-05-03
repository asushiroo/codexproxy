from __future__ import annotations

from html import escape

from codexproxy.state import ClientBinding


def render_usage_page(binding: ClientBinding) -> str:
    remaining = max(binding.limit - binding.count, 0)
    usage_percent = (
        0 if binding.limit == 0 else round((binding.count / binding.limit) * 100, 2)
    )
    client_name = escape(binding.name)
    client_api_key = escape(_mask_api_key(binding.client_api_key or ""))

    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{client_name} usage</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 40px; background: #f7f7f8; color: #111827; }}
    .card {{ max-width: 720px; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.05); }}
    h1 {{ margin-top: 0; font-size: 28px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 24px; }}
    .item {{ background: #f9fafb; border-radius: 12px; padding: 16px; border: 1px solid #e5e7eb; }}
    .label {{ color: #6b7280; font-size: 14px; margin-bottom: 8px; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    .meta {{ margin-top: 24px; color: #4b5563; line-height: 1.7; word-break: break-all; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Client Usage</h1>
    <div class=\"meta\">
      <div><strong>client</strong>: {client_name}</div>
      <div><strong>client_api_key</strong>: {client_api_key}</div>
    </div>
    <div class=\"grid\">
      <div class=\"item\">
        <div class=\"label\">Remaining</div>
        <div class=\"value\">{remaining} / {binding.limit}</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Used</div>
        <div class=\"value\">{binding.count} / {binding.limit}</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Usage Percent</div>
        <div class=\"value\">{usage_percent}%</div>
      </div>
      <div class=\"item\">
        <div class=\"label\">Limit</div>
        <div class=\"value\">{binding.limit}</div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def _mask_api_key(client_api_key: str) -> str:
    if len(client_api_key) <= 10:
        return client_api_key
    return f"{client_api_key[:6]}...{client_api_key[-4:]}"
