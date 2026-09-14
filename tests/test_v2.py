import json
from datetime import datetime

from test_webhook import make_order, message_event, post_webhook


def test_order_profit_math():
    import app
    row = {"payment_amount": 1000, "item_cost": 400, "china_ship_fee": 100, "house_ship_fee": 50}
    p = app.order_profit(row)
    assert p["profit"] == 450
    assert round(p["margin"], 1) == 45.0


def test_order_profit_divide_by_zero_guard():
    import app
    row = {"payment_amount": 0, "item_cost": 100, "china_ship_fee": 0, "house_ship_fee": 0}
    p = app.order_profit(row)
    assert p["profit"] == -100
    assert p["margin"] is None  # rendered as "—", never a ZeroDivisionError


def test_track_page_leaks_no_internal_tracking(app_module, client):
    make_order(app_module, link_code="LINK01")  # mode รถ, lot 11278
    resp = client.get("/track/LINK01")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "11278" not in html
    assert "รถ" not in html and "เรือ" not in html


def test_track_unknown_code_404(client):
    assert client.get("/track/NOPE99").status_code == 404


def test_webhook_linked_customer_typing_code_gets_friendly_status(app_module, client, monkeypatch):
    replies = []
    monkeypatch.setattr(app_module, "line_reply", lambda token, text: replies.append(text) or True)
    make_order(app_module, link_code="LINK01")

    # First message links the LINE account; second is the code lookup.
    post_webhook(client, "test-line-channel-secret", message_event("U1", "LINK01"))
    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "LINK01"))
    assert resp.status_code == 200
    assert any("Ordered" in r for r in replies)
    # friendly label only -- never the internal mode/lot
    assert not any("รถ" in r or "11278" in r for r in replies)


def test_translate_title_parses_mocked_response(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"en": "red shoes", "th": "รองเท้าแดง"})}}]}

    monkeypatch.setattr(app_module.requests, "post", lambda *a, **k: FakeResp())
    out = app_module.translate_title("红鞋")
    assert out == {"en": "red shoes", "th": "รองเท้าแดง"}


def test_translate_title_no_key_returns_none(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "")
    assert app_module.translate_title("红鞋") is None


def test_openrouter_json_strips_markdown_code_fence(app_module, monkeypatch):
    """Real OpenRouter/Claude responses sometimes wrap JSON in ```json ... ```
    despite the prompt saying 'return ONLY compact JSON' -- caught live while
    activating the production key. Must not break parsing."""
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            fenced = "```json\n" + json.dumps({"en": "red shoes", "th": "รองเท้าแดง"}) + "\n```"
            return {"choices": [{"message": {"content": fenced}}]}

    monkeypatch.setattr(app_module.requests, "post", lambda *a, **k: FakeResp())
    out = app_module.translate_title("红鞋")
    assert out == {"en": "red shoes", "th": "รองเท้าแดง"}
