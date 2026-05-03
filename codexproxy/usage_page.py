from __future__ import annotations

from html import escape

from codexproxy.expiry_manager import ExpiryStatus
from codexproxy.state import ClientBinding


def render_usage_page(binding: ClientBinding, expiry_status: ExpiryStatus | None = None) -> str:
    remaining = max(binding.limit - binding.count, 0)
    usage_percent = (
        0 if binding.limit == 0 else round((binding.count / binding.limit) * 100, 2)
    )
    client_name = escape(binding.name)
    expire_time_text = escape(expiry_status.expire_time_text or "Not set") if expiry_status else "Not set"
    auto_update_enabled = "enabled" if expiry_status and expiry_status.auto_update_enabled else "disabled"
    notice_html = ""
    if expiry_status and expiry_status.notice:
        notice_html = (
            '<div class="notice">'
            f"{escape(expiry_status.notice)}"
            "</div>"
        )

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
    .notice {{ margin-top: 20px; padding: 14px 16px; border-radius: 12px; background: #fff7ed; color: #9a3412; border: 1px solid #fdba74; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Client Usage</h1>
    <div class=\"meta\">
      <div><strong>client</strong>: {client_name}</div>
      <div><strong>expire_time</strong>: {expire_time_text}</div>
      <div><strong>auto_update</strong>: {auto_update_enabled}</div>
    </div>
    {notice_html}
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
