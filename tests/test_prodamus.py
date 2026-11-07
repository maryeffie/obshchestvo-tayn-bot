"""Подпись, платёжная ссылка и разбор уведомлений Prodamus."""

from __future__ import annotations

from bot.prodamus import Notification, ProdamusClient, sign_payload, verify_signature

from .helpers import make_settings


def test_signature_roundtrip() -> None:
    payload = {"order_id": "tg1-abc", "sum": "790", "products": [{"name": "Клуб", "price": "790"}]}
    signature = sign_payload(payload, "secret")
    assert len(signature) == 64
    assert verify_signature(payload, "secret", signature)
    assert verify_signature(payload, "secret", signature.upper())
    assert not verify_signature(payload, "other-secret", signature)
    assert not verify_signature({**payload, "sum": "1"}, "secret", signature)
    assert not verify_signature(payload, "secret", None)


def test_signature_uses_utf8_and_escaped_slashes() -> None:
    """Проверено на живой форме Prodamus: кириллица остаётся UTF-8, «/» экранируется.

    Если экранировать кириллицу как \\uXXXX (поведение PHP json_encode по умолчанию),
    Prodamus считает подпись неверной и отбрасывает параметры ссылки.
    """
    import hashlib
    import hmac

    payload = {"b": "2", "a": "https://example.com/x", "c": {"y": "1", "x": "кириллица"}}
    body = '{"a":"https:\\/\\/example.com\\/x","b":"2","c":{"x":"кириллица","y":"1"}}'
    expected = hmac.new(b"secret", body.encode(), hashlib.sha256).hexdigest()
    assert sign_payload(payload, "secret") == expected
    assert "\\u" not in body


def test_signature_legacy_ascii_still_verifies() -> None:
    """Входящие уведомления со «старой» (PHP-подобной) подписью тоже принимаем."""
    import hashlib
    import hmac
    import json

    payload = {"order_id": "tg1-x", "sum": "790", "name": "Клуб"}
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(b"secret", body.encode(), hashlib.sha256).hexdigest()
    assert verify_signature(payload, "secret", signature)


def test_verify_accepts_python_style_json() -> None:
    """Вторая реализация (без экранирования) тоже должна проходить проверку."""
    import hashlib
    import hmac
    import json

    payload = {"order_id": "tg1-x", "sum": "790"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    signature = hmac.new(b"secret", body.encode(), hashlib.sha256).hexdigest()
    assert verify_signature(payload, "secret", signature)


def test_payment_url_recurrent() -> None:
    client = ProdamusClient(make_settings())
    url = client.payment_url(tg_id=555, order_id="tg555-abc", base_amount=790, discount=158)
    assert url.startswith("https://demo.payform.ru/?")
    assert "subscription=4242" in url
    assert "discount_value=158" in url
    assert "customer_extra=tg%3A555" in url
    assert "order_id=tg555-abc" in url
    assert "signature=" in url
    assert "products%5B0%5D%5Bname%5D" in url


def test_payment_url_one_time_discount() -> None:
    client = ProdamusClient(make_settings(prodamus_subscription_id=None))
    url = client.payment_url(tg_id=1, order_id="tg1-abc", base_amount=790, discount=79)
    assert "subscription=" not in url
    assert "products%5B0%5D%5Bprice%5D=711" in url


def test_parse_success_notification() -> None:
    client = ProdamusClient(make_settings())
    note = client.parse(
        {
            "order_id": "tg777-abc",
            "payment_status": "success",
            "sum": "790.00",
            "customer_extra": "tg:777",
            "customer_phone": "+79990001122",
        }
    )
    assert isinstance(note, Notification)
    assert note.kind == "paid"
    assert note.tg_id == 777
    assert note.amount == 790
    assert note.phone == "+79990001122"


def test_parse_failed_notification() -> None:
    client = ProdamusClient(make_settings())
    note = client.parse(
        {
            "order_id": "tg777-abc",
            "payment_status": "fail",
            "payment_status_description": "Недостаточно средств",
        }
    )
    assert note.kind == "failed"


def test_parse_info_notification() -> None:
    client = ProdamusClient(make_settings())
    note = client.parse({"subscription": "4242", "subscription_status": "upcoming"})
    assert note.kind == "info"


def test_verify_request_with_header() -> None:
    client = ProdamusClient(make_settings())
    form = {"order_id": "tg5-abc", "payment_status": "success", "sum": "790"}
    signature = sign_payload(form, "secret-key-for-tests")
    assert client.verify_request(form, {"Sign": signature})
    assert not client.verify_request(form, {"Sign": "deadbeef"})


def test_demo_signature_rejected() -> None:
    """Демо-подпись (секрет + 'demo') не должна считаться боевой."""
    client = ProdamusClient(make_settings())
    form = {"order_id": "tg5-abc", "payment_status": "success", "sum": "790"}
    demo_signature = sign_payload(form, "secret-key-for-testsdemo")
    assert not client.verify_request(form, {"Sign": demo_signature})
