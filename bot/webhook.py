"""HTTP-приёмник вебхуков Prodamus (aiohttp)."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qs

from aiohttp import web

from .app import App
from .keyboards import quick_grant_kb

log = logging.getLogger(__name__)


def create_webhook_app(app: App) -> web.Application:
    settings = app.settings

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "bot": "obshchestvo-tayn"})

    async def webhook(request: web.Request) -> web.Response:
        headers = dict(request.headers)
        content_type = (request.content_type or "").lower()

        try:
            raw = await request.read()
        except Exception:  # noqa: BLE001
            raw = b""

        data: dict = {}
        try:
            if "json" in content_type:
                payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                data = (
                    {str(k): v for k, v in payload.items()}
                    if isinstance(payload, dict)
                    else {}
                )
            else:
                parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                data = {k: v[0] for k, v in parsed.items()}
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось разобрать тело вебхука: %s", exc)
            data = {}

        if not data:
            log.warning("Вебхук пришёл с пустым телом (content-type=%s)", content_type)
            return web.Response(status=400, text="error: empty body")

        # необязательный общий секрет в адресе: ?token=... (см. WEBHOOK_TOKEN)
        expected_token = app.settings.webhook_token
        token_ok = True
        if expected_token:
            token_ok = request.query.get("token") == expected_token
        if not token_ok:
            log.error("Вебхук без верного token — отклонён")
            return web.Response(status=403, text="error: forbidden")

        signature = next(
            (value for key, value in headers.items() if key.lower() == "sign"), None
        )
        signature = signature or data.get("signature") or data.get("sign")

        # 1) штатная проверка (нормализованный JSON), 2) по сырому телу запроса
        verified = app.prodamus.verify_request(data, headers, raw)
        if not verified and raw:
            verified = app.prodamus.verify_raw(raw, signature, data)

        if not verified and not (expected_token and token_ok):
            # Подпись не сошлась, а секретного token нет: автоматически засчитывать
            # нельзя — такую ссылку легко подделать. Отдаём админу кнопку в один клик.
            order_id = str(data.get("order_num") or data.get("order_id") or "")
            order = await app.db.get_order(order_id) if order_id else None
            tg_id = int(order["tg_id"]) if order else None
            if tg_id is None:
                digits = "".join(ch for ch in str(data.get("customer_extra") or "") if ch.isdigit())
                if digits:
                    tg_id = int(digits)
            log.error(
                "Вебхук с неверной подписью. content-type=%s, заголовки=%s, Sign=%s, тело=%s",
                content_type,
                sorted(headers.keys()),
                (str(signature)[:8] + "…") if signature else "нет",
                raw[:1200].decode("utf-8", "replace"),
            )
            text = (
                "⚠️ <b>Платёж пришёл, но подпись не сошлась</b>\n\n"
                f"Заказ: <code>{order_id or '—'}</code>\n"
                f"ID участника: <code>{tg_id or '—'}</code>\n"
                f"Сумма: {data.get('sum') or '—'} {data.get('currency') or ''}\n"
                f"Телефон: {data.get('customer_phone') or '—'}\n\n"
                "Проверьте платёж в кабинете Prodamus. Если деньги пришли — нажмите "
                "кнопку, доступ откроется сам."
            )
            for admin_id in app.settings.admin_ids:
                try:
                    await app.bot.send_message(
                        admin_id,
                        text,
                        reply_markup=quick_grant_kb(tg_id) if tg_id else None,
                    )
                except Exception:  # noqa: BLE001
                    log.warning("Не удалось предупредить админа %s", admin_id)
            return web.Response(status=200, text="success")

        if not verified:
            log.warning(
                "Подпись не сошлась, но адрес содержит верный token — платёж засчитан"
            )

        asyncio.create_task(_process(app, data))
        return web.Response(status=200, text="success")

    async def webhook_probe(request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            text="Prodamus webhook endpoint is alive. Use POST for notifications.",
        )

    async def on_startup(application: web.Application) -> None:
        log.info("Вебхук Prodamus слушает %s", settings.webhook_path)

    application = web.Application()
    application.router.add_get("/health", health)
    application.router.add_get(settings.webhook_path, webhook_probe)
    application.router.add_post(settings.webhook_path, webhook)
    application.on_startup.append(on_startup)
    return application


async def _process(app: App, data: dict) -> None:
    """Обрабатываем уведомление в отдельной задаче, чтобы быстро ответить 200."""
    try:
        result = await app.payments.handle(data)
        log.info("Вебхук обработан: %s (order_id=%s)", result, data.get("order_id"))
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки вебхука Prodamus")
        await app.access.notify_admins(
            "⚠️ <b>Ошибка обработки платежа.</b>\n\n"
            f"order_id: <code>{data.get('order_id')}</code>\n"
            "Подробности — в логах. Проверьте, выдан ли доступ."
        )


def _short(data: dict) -> dict:
    keep = ("order_id", "payment_status", "sum", "subscription", "customer_extra")
    return {k: v for k, v in data.items() if k in keep} or {"keys": list(data)[:10]}


__all__ = ["create_webhook_app"]
