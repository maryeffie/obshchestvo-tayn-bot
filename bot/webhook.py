"""HTTP-приёмник вебхуков Prodamus (aiohttp)."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .app import App

log = logging.getLogger(__name__)


def create_webhook_app(app: App) -> web.Application:
    settings = app.settings

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "bot": "obshchestvo-tayn"})

    async def webhook(request: web.Request) -> web.Response:
        headers = dict(request.headers)
        content_type = (request.content_type or "").lower()

        try:
            if "json" in content_type:
                payload = await request.json()
                data = {str(k): v for k, v in payload.items()} if isinstance(payload, dict) else {}
            else:
                post = await request.post()
                data = {str(k): v for k, v in post.items() if isinstance(v, str)}
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось разобрать тело вебхука: %s", exc)
            return web.Response(status=400, text="error: bad request body")

        if not data:
            log.warning("Вебхук пришёл с пустым телом (content-type=%s)", content_type)
            return web.Response(status=400, text="error: empty body")

        if not app.prodamus.verify_request(data, headers):
            # 400 — важный сигнал: Prodamus повторит отправку. Если подпись
            # неверна по сути (не тот секрет / демо-режим), разбираемся вручную.
            log.error("Вебхук с неверной подписью отклонён. Данные: %s", _short(data))
            return web.Response(status=400, text="error: signature incorrect")

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
