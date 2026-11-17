"""HTTP-приёмник вебхуков Prodamus (aiohttp)."""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import parse_qs

from aiohttp import web

from . import texts
from .app import App

log = logging.getLogger(__name__)


def create_webhook_app(app: App) -> web.Application:
    settings = app.settings
    notification_tasks: set[asyncio.Task] = set()

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "bot": "obshchestvo-tayn"})

    async def checkout(request: web.Request) -> web.Response:
        # Токен случаен и указывает только на уже сохранённый заказ: пользователь
        # не может подставить произвольный адрес или Telegram ID для редиректа.
        token = request.match_info["token"]
        order = await app.db.get_order_by_checkout_token(token)
        if not order or not order.get("checkout_url") or order["status"] not in {"pending", "failed"}:
            return web.Response(status=404, text="Payment link unavailable")

        try:
            notify_once = await app.db.claim_checkout_notice(token)
        except Exception:  # отсутствие сообщения не должно блокировать оплату
            log.warning("Не удалось подготовить сообщение об ожидании оплаты")
            notify_once = False
        if notify_once:
            async def notify() -> None:
                try:
                    await app.bot.send_message(chat_id=int(order["tg_id"]), text=texts.CHECKOUT_NOTICE)
                except Exception:  # уведомление не должно прервать переход к оплате
                    log.warning("Не удалось отправить сообщение об ожидании оплаты")

            task = asyncio.create_task(notify())
            notification_tasks.add(task)
            task.add_done_callback(notification_tasks.discard)
        return web.Response(
            status=302,
            headers={
                "Location": order["checkout_url"],
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    async def webhook(request: web.Request) -> web.Response:
        headers = dict(request.headers)
        content_type = (request.content_type or "").lower()

        raw = b""
        data: dict = {}
        try:
            if "multipart/form-data" in content_type:
                # request.read() consumes the multipart stream; parse it first.
                parsed_form = await request.post()
                data = {str(k): str(v) for k, v in parsed_form.items()}
            else:
                raw = await request.read()
                if "json" in content_type:
                    payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                    data = {str(k): v for k, v in payload.items()} if isinstance(payload, dict) else {}
                else:
                    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                    data = {k: v[0] for k, v in parsed.items()}
        except Exception:  # noqa: BLE001
            log.warning("Не удалось разобрать тело вебхука")
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
        if not verified:
            verified = app.prodamus.verify_raw(raw, signature, data)

        if not verified:
            # Данные без подписи недостоверны, даже предлагаемый ID получателя.
            log.error("Вебхук с неверной подписью отклонён")
            await app.access.notify_admins(
                "⚠️ <b>Уведомление Prodamus отклонено: неверная подпись.</b>\n"
                "Проверьте платёж в кабинете и настройки подписи. Доступ не выдан."
            )
            return web.Response(status=403, text="error: invalid signature")

        try:
            result = await app.payments.handle(data)
            log.info("Вебхук обработан: %s", result)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки вебхука Prodamus")
            return web.Response(status=500, text="error: processing failed")
        return web.Response(status=200, text="success")

    async def webhook_probe(request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            text="Prodamus webhook endpoint is alive. Use POST for notifications.",
        )

    async def on_startup(application: web.Application) -> None:
        log.info("Вебхук Prodamus слушает %s", settings.webhook_path)

    async def on_cleanup(application: web.Application) -> None:
        tasks = tuple(notification_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    application = web.Application()
    application.router.add_get("/health", health)
    application.router.add_get("/checkout/{token:[0-9a-f]{32}}", checkout, allow_head=False)
    application.router.add_get(settings.webhook_path, webhook_probe)
    application.router.add_post(settings.webhook_path, webhook)
    application.on_startup.append(on_startup)
    application.on_cleanup.append(on_cleanup)
    return application



__all__ = ["create_webhook_app"]
