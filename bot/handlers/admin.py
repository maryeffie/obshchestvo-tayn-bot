"""Админ-панель: статистика, ручная выдача/отзыв доступа, промокоды."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html import escape

import aiohttp

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import texts
from ..app import App
from ..filters import IsAdmin
from ..keyboards import (
    admin_kb,
    admins_kb,
    revoke_confirm_kb,
    date_confirm_kb,
    user_card_kb,
    back_to_panel_kb,
    cancel_kb,
    promos_kb,
)
from ..prodamus import sign_payload
from ..services.access import INSIDE_STATUSES, status_ru
from ..utils import (
    fmt_date,
    from_iso,
    fmt_datetime,
    fmt_price,
    human_username,
    is_active,
    to_iso,
    utcnow,
)

log = logging.getLogger(__name__)

PROMO_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")
STATUS_LABELS = {"new": "новичок", "active": "активен", "expired": "доступ закрыт"}


class AdminStates(StatesGroup):
    grant_user = State()
    grant_days = State()
    date_user = State()
    date_value = State()
    date_confirm = State()
    revoke_user = State()
    find_user = State()
    cancel_user = State()
    promo_code = State()
    promo_percent = State()
    promo_uses = State()
    promo_days = State()
    add_admin = State()


def _forwarded_user(message: Message):
    """Достаёт автора пересланного сообщения (для выдачи доступа по пересылке)."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender is not None:
            return sender
    return getattr(message, "forward_from", None)


def build_router(app: App) -> Router:
    router = Router(name="admin")
    settings = app.settings
    db = app.db
    access = app.access
    payments = app.payments
    subscriptions = app.subscriptions
    is_admin = IsAdmin(settings)

    async def resolve_target(message: Message) -> tuple[int | None, dict | None]:
        forwarded = _forwarded_user(message)
        if forwarded is not None:
            return forwarded.id, await db.get_user(forwarded.id)
        text = (message.text or "").strip()
        if not text:
            return None, None
        user = await db.find_user(text)
        if user:
            return int(user["tg_id"]), user
        if text.lstrip("-").isdigit():
            return int(text), None
        return None, None

    # ------------------------------------------------------------ команды

    @router.message(Command("admin"), is_admin)
    async def cmd_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(texts.ADMIN_PANEL, reply_markup=admin_kb())

    @router.message(Command("chatid"), is_admin)
    async def cmd_chatid(message: Message) -> None:
        await message.answer(
            f"chat_id: <code>{message.chat.id}</code>\nтип чата: {message.chat.type}"
        )

    @router.message(Command("fakepay"), is_admin)
    async def cmd_fakepay(message: Message) -> None:
        """Полная проверка оплаты без денег: шлём сами себе подписанный вебхук.

        Формат: /fakepay — на себе, /fakepay 123456789 — на участнике,
        /fakepay 123456789 100 — своя сумма.
        """
        if message.from_user is None:
            return
        parts = (message.text or "").split()[1:]
        if parts and not parts[0].isdigit():
            await message.answer(
                "Формат: <code>/fakepay</code> — на себе,\n"
                "<code>/fakepay 123456789</code> — на участнике,\n"
                "<code>/fakepay 123456789 100</code> — своя сумма."
            )
            return
        tg_id = int(parts[0]) if parts else message.from_user.id
        amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else settings.price
        await db.upsert_user(tg_id)
        order_id = payments.new_order_id(tg_id)
        await db.create_order(
            order_id=order_id,
            tg_id=tg_id,
            amount=amount,
            is_recurring=settings.is_recurrent,
        )
        data = {
            "order_id": order_id,
            "order_num": order_id,
            "payment_status": "success",
            "payment_status_description": "Тестовая оплата из бота",
            "sum": str(amount),
            "customer_extra": f"tg:{tg_id}",
            "products": [
                {
                    "name": settings.product_name,
                    "price": str(amount),
                    "quantity": "1",
                    "sum": str(amount),
                }
            ],
        }
        signature = sign_payload(data, settings.prodamus_secret)

        delivered = False
        detail = ""
        if settings.public_base_url:
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        settings.webhook_url, json=data, headers={"Sign": signature}
                    ) as response:
                        delivered = response.status == 200
                        detail = f"HTTP {response.status}"
            except Exception as exc:  # noqa: BLE001
                detail = "не удалось отправить запрос на вебхук"

        if delivered:
            await message.answer(
                "🧪 <b>Тестовый платёж отправлен на вебхук</b>\n\n"
                f"Заказ: <code>{order_id}</code>\n"
                f"ID участника: <code>{tg_id}</code>\n"
                f"Сумма: {fmt_price(amount, settings.currency)}\n\n"
                "Проверьте логи: должна быть строка «Вебхук обработан: paid». "
                "Участнику придут ссылки в канал и чат.",
                reply_markup=back_to_panel_kb(),
            )
            return

        # если свой же вебхук недоступен — прогоняем обработку напрямую
        result = await payments.handle(data)
        await message.answer(
            "🧪 <b>Тестовый платёж прогнан напрямую</b>\n\n"
            f"Результат: <code>{result}</code>\n"
            f"Заказ: <code>{order_id}</code>\n"
            f"ID участника: <code>{tg_id}</code>\n"
            f"Сумма: {fmt_price(amount, settings.currency)}\n\n"
            f"Вебхук проверить не удалось: {detail or 'адрес не задан'}.\n"
            "Цепочка выдачи доступа при этом проверена.",
            reply_markup=back_to_panel_kb(),
        )

    @router.message(Command("links"), is_admin)
    async def cmd_links(message: Message) -> None:
        """Переотправить участнику свежие ссылки в канал и чат: /links 123456789."""
        if message.from_user is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target.isdigit():
            await message.answer(
                "Формат: <code>/links 123456789</code> — ID участника.\n"
                "Свежие ссылки живут 24 часа и открываются несколько раз."
            )
            return
        tg_id = int(target)
        ok, error = await access.send_links_again(tg_id)
        if ok:
            await message.answer(
                f"✅ Новые ссылки отправлены участнику <code>{tg_id}</code>.",
                reply_markup=back_to_panel_kb(),
            )
        else:
            await message.answer(
                f"⚠️ Не удалось отправить ссылки: <code>{error}</code>\n\n"
                "Проверьте, что бот — администратор канала и чата с правом "
                "«Приглашать пользователей».",
                reply_markup=back_to_panel_kb(),
            )

    async def run_check(tg_id: int) -> str:
        """Статусы в канале/чате + разбан + свежие ссылки. Общая для /check и кнопки."""
        statuses = await access.lift_ban(tg_id)  # заодно снимает бан, если он есть
        ok, error = await access.send_links_again(tg_id)
        if any(s in INSIDE_STATUSES for s in statuses.values()):
            note = texts.CHECK_NOTE_INSIDE
        elif any(s.startswith("kicked") for s in statuses.values()):
            note = texts.CHECK_NOTE_BANNED
        else:
            note = ""
        return texts.CHECK_RESULT.format(
            tg_id=tg_id,
            channel=status_ru(statuses.get("channel", "—")),
            chat=status_ru(statuses.get("chat", "—")),
            links=texts.CHECK_LINKS_OK
            if ok
            else texts.CHECK_LINKS_FAIL.format(error=error or "неизвестно"),
            note=note,
        )

    @router.callback_query(F.data.startswith("adm:check:"), is_admin)
    async def cb_check(call: CallbackQuery) -> None:
        raw = (call.data or "").split(":")[-1]
        if not raw.lstrip("-").isdigit():
            await call.answer("Не понял ID", show_alert=True)
            return
        await call.answer("Проверяю…")
        if call.message is not None:
            await call.message.answer(
                await run_check(int(raw)), reply_markup=back_to_panel_kb()
            )

    @router.message(Command("check"), is_admin)
    async def cmd_check(message: Message) -> None:
        """Почему человек не может войти: /check 2138023308."""
        parts = (message.text or "").split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target.lstrip("-").isdigit():
            await message.answer(texts.CHECK_USAGE)
            return
        await message.answer(await run_check(int(target)), reply_markup=back_to_panel_kb())

    @router.message(Command("testpay"), is_admin)
    async def cmd_testpay(message: Message) -> None:
        """Прогон цепочки «оплата → доступ» без Prodamus. Только для админа.

        Нужен, чтобы проверить выдачу ссылок и уведомления, когда касса молчит.
        """
        if message.from_user is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""
        if target and not target.isdigit():
            await message.answer(
                "Формат: <code>/testpay</code> — проверить на себе,\n"
                "или <code>/testpay 123456789</code> — на конкретном ID."
            )
            return
        tg_id = int(target) if target else message.from_user.id
        await db.upsert_user(tg_id)
        order_id = payments.new_order_id(tg_id)
        await db.create_order(order_id=order_id, tg_id=tg_id, amount=settings.price,
                              is_recurring=False)
        result = await payments.handle(
            {
                "order_id": "test-" + order_id,
                "order_num": order_id,
                "payment_status": "success",
                "payment_status_description": "Тестовая оплата из бота",
                "sum": str(settings.price),
                "customer_extra": str(tg_id),
                "notification_code": "test",
            }
        )
        user = await db.get_user(tg_id)
        paid_until = from_iso(user.get("paid_until")) if user else None
        await message.answer(
            "🧪 <b>Тестовая оплата прогнана</b>\n\n"
            f"Результат: <code>{result}</code>\n"
            f"ID участника: <code>{tg_id}</code>\n"
            f"Сумма: {fmt_price(settings.price, settings.currency)}\n"
            f"Доступ до: {fmt_date(paid_until, settings.timezone)}\n\n"
            "Если участник получил сообщение со ссылками — цепочка выдачи работает. "
            "Значит, дело только в уведомлениях Prodamus.",
            reply_markup=back_to_panel_kb(),
        )

    @router.message(Command("stats"), is_admin)
    async def cmd_stats(message: Message) -> None:
        await message.answer(await _stats_text(), reply_markup=back_to_panel_kb())

    async def _stats_text() -> str:
        stats = await db.stats()
        return texts.ADMIN_STATS.format(
            users_total=stats["users_total"],
            active=stats["active"],
            expiring_3d=stats["expiring_3d"],
            autorenew_off=stats["autorenew_off"],
            users_new_month=stats["users_new_month"],
            payments_month=stats["payments_month"],
            revenue_month=fmt_price(stats["revenue_month"], settings.currency),
            payments_total=stats["payments_total"],
            revenue_total=fmt_price(stats["revenue_total"], settings.currency),
        )

    # ----------------------------------------------------------- панель

    # ------------------------------------------------------- администраторы

    async def admins_screen():
        extra = [a for a in await db.list_admins() if int(a["tg_id"]) not in settings.admin_ids]
        owners_text = "\n".join(f"• <code>{tg_id}</code>" for tg_id in settings.admin_ids) or "—"
        if extra:
            extra_text = "\n".join(
                "• {} — <code>{}</code>".format(a.get("title") or "без имени", a["tg_id"])
                for a in extra
            )
        else:
            extra_text = texts.ADMIN_ADMINS_EMPTY
        text = texts.ADMIN_ADMINS.format(owners=owners_text, extra=extra_text)
        return text, admins_kb(extra, settings.admin_ids)

    @router.callback_query(F.data == "adm:admins", is_admin)
    async def cb_admins(call: CallbackQuery) -> None:
        text, keyboard = await admins_screen()
        if call.message is not None:
            await call.message.answer(text, reply_markup=keyboard)
        await call.answer()

    @router.callback_query(F.data == "adm:admin_add", is_admin)
    async def cb_admin_add(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.add_admin)
        if call.message is not None:
            await call.message.answer(texts.ADMIN_ADD_PROMPT, reply_markup=cancel_kb())
        await call.answer()

    @router.message(AdminStates.add_admin, is_admin, F.text)
    async def msg_admin_add(message: Message, state: FSMContext) -> None:
        tg_id, user = await resolve_target(message)
        if tg_id is None:
            await message.answer(texts.ADMIN_ADD_NOT_FOUND, reply_markup=cancel_kb())
            return
        user = user or await db.get_user(tg_id)
        title = None
        if user:
            title = user.get("first_name") or user.get("username")
        elif message.text and not message.text.strip().lstrip("-").isdigit():
            title = message.text.strip()[:64]
        await db.add_admin(tg_id, title, message.from_user.id if message.from_user else None)
        settings.extra_admin_ids.add(tg_id)
        await state.clear()
        await message.answer(
            texts.ADMIN_ADDED.format(
                who=human_username(user or {"tg_id": tg_id, "username": None})
            ),
            reply_markup=admin_kb(),
        )

    @router.callback_query(F.data.startswith("adm:admin_del:"), is_admin)
    async def cb_admin_del(call: CallbackQuery) -> None:
        raw = (call.data or "").split(":")[-1]
        if not raw.lstrip("-").isdigit():
            await call.answer("Не понял ID", show_alert=True)
            return
        tg_id = int(raw)
        if tg_id in settings.admin_ids:
            await call.answer(texts.ADMIN_OWNER_LOCKED, show_alert=True)
            return
        await db.remove_admin(tg_id)
        settings.extra_admin_ids.discard(tg_id)
        user = await db.get_user(tg_id)
        await call.answer("Убрали ✅", show_alert=False)
        if call.message is not None:
            await call.message.answer(
                texts.ADMIN_DELETED.format(
                    who=human_username(user or {"tg_id": tg_id, "username": None})
                ),
                reply_markup=back_to_panel_kb(),
            )

    @router.callback_query(F.data == "adm:panel", is_admin)
    async def cb_panel(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer()
        if call.message is not None:
            await call.message.edit_text(texts.ADMIN_PANEL, reply_markup=admin_kb())

    @router.callback_query(F.data == "adm:stats", is_admin)
    async def cb_stats(call: CallbackQuery) -> None:
        await call.answer()
        if call.message is not None:
            await call.message.edit_text(await _stats_text(), reply_markup=back_to_panel_kb())

    # ------------------------------------------------ ручная выдача доступа

    @router.callback_query(F.data == "adm:grant", is_admin)
    async def cb_grant(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.grant_user)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_ASK_USER_GRANT, reply_markup=cancel_kb())

    @router.message(AdminStates.grant_user, is_admin, F.text)
    async def grant_user(message: Message, state: FSMContext) -> None:
        tg_id, user = await resolve_target(message)
        if tg_id is None:
            await message.answer(texts.ADMIN_USER_NOT_FOUND, reply_markup=cancel_kb())
            return
        await state.update_data(tg_id=tg_id)
        await state.set_state(AdminStates.grant_days)
        await message.answer(
            f"Участник: {escape(human_username(user or {'tg_id': tg_id}))} (ID <code>{tg_id}</code>). "
            "Сверьте ID с участником до выдачи доступа.\n\n"
            + texts.ADMIN_ASK_DAYS.format(days=settings.subscription_days), reply_markup=cancel_kb()
        )

    @router.message(AdminStates.grant_days, is_admin, F.text)
    async def grant_days(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text.isdigit():
            await message.answer(texts.ADMIN_BAD_NUMBER, reply_markup=cancel_kb())
            return
        days = int(text) or settings.subscription_days
        data = await state.get_data()
        await state.clear()
        tg_id = data.get("tg_id")
        if tg_id is None:
            await message.answer(texts.ADMIN_CANCELLED, reply_markup=back_to_panel_kb())
            return
        await db.upsert_user(int(tg_id))
        ok, paid_until, error = await payments.grant_manually(int(tg_id), days)
        user = await db.get_user(int(tg_id))
        if ok:
            await message.answer(
                texts.ADMIN_GRANTED.format(
                    user=human_username(user) if user else tg_id,
                    days=days,
                    date=fmt_date(paid_until, settings.timezone),
                ),
                reply_markup=back_to_panel_kb(),
            )
        else:
            await message.answer(
                texts.ADMIN_GRANT_FAILED.format(
                    user=human_username(user) if user else tg_id,
                    date=fmt_date(paid_until, settings.timezone),
                    error=error,
                ),
                reply_markup=back_to_panel_kb(),
            )

    @router.callback_query(F.data.startswith("adm:quickgrant:"), is_admin)
    async def cb_quick_grant(call: CallbackQuery) -> None:
        """Выдача доступа по кнопке из уведомления «Я оплатил»."""
        if call.data is None:
            return
        try:
            tg_id = int(call.data.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            await call.answer("Не понял, кому выдавать доступ", show_alert=True)
            return
        await call.answer("Выдаю доступ…")
        await db.upsert_user(tg_id)
        ok, paid_until, error = await payments.grant_manually(
            tg_id, settings.subscription_days
        )
        user = await db.get_user(tg_id)
        name = human_username(user) if user else tg_id
        text = (
            texts.PAID_CLAIM_GRANTED.format(
                user=name, date=fmt_date(paid_until, settings.timezone)
            )
            if ok
            else texts.PAID_CLAIM_GRANT_FAILED.format(user=name, error=error)
        )
        if call.message is not None:
            await call.message.answer(text, reply_markup=back_to_panel_kb())

    # ------------------------------------------- точная дата доступа

    async def ask_access_date(message: Message, state: FSMContext, tg_id: int) -> None:
        user = await db.get_user(tg_id)
        await state.update_data(date_tg_id=tg_id)
        await state.set_state(AdminStates.date_value)
        await message.answer(
            texts.ADMIN_DATE_ASK.format(
                user=escape(human_username(user or {"tg_id": tg_id})),
                tg_id=tg_id,
                timezone=escape(str(settings.timezone)),
            ),
            reply_markup=cancel_kb(),
        )

    @router.callback_query(F.data == "adm:date", is_admin)
    async def cb_date_start(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AdminStates.date_user)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_DATE_ASK_USER, reply_markup=cancel_kb())

    @router.callback_query(F.data.startswith("adm:date:"), is_admin)
    async def cb_date_from_card(call: CallbackQuery, state: FSMContext) -> None:
        raw = (call.data or "").rsplit(":", 1)[-1]
        if not raw.isdigit() or len(raw) > 19 or not 0 < int(raw) < 2**63:
            await call.answer("Не удалось определить пользователя", show_alert=True)
            return
        await state.clear()
        await call.answer()
        if call.message is not None:
            await ask_access_date(call.message, state, int(raw))

    @router.message(AdminStates.date_user, is_admin, F.text)
    async def date_user(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.isdigit() and (len(raw) > 19 or not 0 < int(raw) < 2**63):
            await message.answer(texts.ADMIN_USER_NOT_FOUND, reply_markup=cancel_kb())
            return
        tg_id, _user = await resolve_target(message)
        if tg_id is None or not 0 < tg_id < 2**63:
            await message.answer(texts.ADMIN_USER_NOT_FOUND, reply_markup=cancel_kb())
            return
        await ask_access_date(message, state, tg_id)

    @router.message(AdminStates.date_value, is_admin, F.text)
    async def date_value(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        try:
            if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
                raise ValueError("date format")
            # Включительно весь указанный день в настройках часового пояса бота.
            until = datetime.strptime(value, "%d.%m.%Y").replace(
                hour=23, minute=59, second=59, tzinfo=settings.timezone
            )
            if until <= utcnow():
                raise ValueError("past date")
        except ValueError:
            await message.answer(texts.ADMIN_DATE_INVALID, reply_markup=cancel_kb())
            return
        data = await state.get_data()
        tg_id = data.get("date_tg_id")
        if not isinstance(tg_id, int) or tg_id <= 0:
            await state.clear()
            await message.answer(texts.ADMIN_CANCELLED, reply_markup=back_to_panel_kb())
            return
        user = await db.get_user(tg_id)
        keys = ("status", "paid_until", "exact_access_end", "last_payment_at", "auto_renew", "cancelled_at", "updated_at")
        snapshot = {key: user.get(key) for key in keys} if user else None
        old_until = from_iso(user.get("paid_until")) if user else None
        warning = "⚠️ <b>Новый срок короче текущего.</b>\n\n" if old_until and until < old_until else ""
        await state.update_data(date_until=to_iso(until), date_expected=snapshot)
        await state.set_state(AdminStates.date_confirm)
        await message.answer(
            texts.ADMIN_DATE_CONFIRM.format(
                user=escape(human_username(user or {"tg_id": tg_id})), tg_id=tg_id,
                old=fmt_datetime(old_until, settings.timezone),
                status=STATUS_LABELS.get(user.get("status"), "неизвестен") if user else "нет в базе",
                new=fmt_datetime(until, settings.timezone), timezone=escape(str(settings.timezone)),
                warning=warning,
            ),
            reply_markup=date_confirm_kb(),
        )

    @router.callback_query(AdminStates.date_confirm, F.data == "adm:date_confirm", is_admin)
    async def cb_date_confirm(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        await call.answer()
        tg_id = data.get("date_tg_id")
        until = from_iso(data.get("date_until"))
        if (
            call.from_user is None or not isinstance(tg_id, int) or tg_id <= 0
            or until is None or until <= utcnow()
        ):
            if call.message is not None:
                await call.message.answer(texts.ADMIN_DATE_INVALID, reply_markup=back_to_panel_kb())
            return
        expected = data.get("date_expected")
        if not await db.set_access_until_exact(tg_id, until, expected, call.from_user.id):
            if call.message is not None:
                await call.message.answer(texts.ADMIN_DATE_CONFLICT, reply_markup=back_to_panel_kb())
            return
        user = await db.get_user(tg_id)
        name = escape(human_username(user or {"tg_id": tg_id}))
        result = texts.ADMIN_DATE_CHANGED.format(
            user=name, date=fmt_date(until, settings.timezone), timezone=escape(str(settings.timezone))
        )
        # При исправлении действующего срока не выдаём повторно приглашения и не
        # сообщаем об оплате. Для нового или неактивного участника отправляем ссылки.
        if not is_active(expected):
            ok, _error = await access.send_access(
                tg_id, paid_until=until,
                text=texts.ACCESS_UNTIL_MANUAL.format(date=fmt_date(until, settings.timezone)),
            )
            result += texts.ADMIN_DATE_CHANGED_INVITES if ok else texts.ADMIN_DATE_CHANGED_FAILED
        if call.message is not None:
            await call.message.answer(result, reply_markup=back_to_panel_kb())

    @router.callback_query(F.data == "adm:date_confirm", is_admin)
    async def cb_date_expired(call: CallbackQuery) -> None:
        await call.answer("Подтверждение устарело. Начните изменение даты заново.", show_alert=True)

    # ---------------------------------------------------------- отзыв доступа

    @router.callback_query(F.data == "adm:revoke", is_admin)
    async def cb_revoke(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.revoke_user)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_ASK_USER_REVOKE, reply_markup=cancel_kb())

    async def revoke_now(tg_id: int) -> str:
        """Закрывает доступ и исключает человека из канала и чата."""
        await db.upsert_user(tg_id)
        await db.set_status(tg_id, "expired")
        await access.kick(tg_id)
        user = await db.get_user(tg_id)
        return texts.ADMIN_REVOKED.format(user=human_username(user))

    @router.message(AdminStates.revoke_user, is_admin, F.text)
    async def revoke_user(message: Message, state: FSMContext) -> None:
        tg_id, user = await resolve_target(message)
        await state.clear()
        if tg_id is None:
            await message.answer(texts.ADMIN_REVOKE_NOT_FOUND, reply_markup=back_to_panel_kb())
            return
        await db.upsert_user(int(tg_id))
        user = user or await db.get_user(int(tg_id))
        assert user is not None
        await message.answer(
            texts.ADMIN_REVOKE_CONFIRM.format(
                user=human_username(user), tg_id=user["tg_id"]
            ),
            reply_markup=revoke_confirm_kb(int(user["tg_id"])),
        )

    @router.callback_query(F.data.startswith("adm:revoke_ask:"), is_admin)
    async def cb_revoke_ask(call: CallbackQuery) -> None:
        """Кнопка «Забрать доступ» из карточки участника — с тем же подтверждением."""
        raw = (call.data or "").split(":")[-1]
        if not raw.lstrip("-").isdigit():
            await call.answer("Не понял ID", show_alert=True)
            return
        await call.answer()
        tg_id = int(raw)
        await db.upsert_user(tg_id)
        user = await db.get_user(tg_id)
        if call.message is not None:
            await call.message.answer(
                texts.ADMIN_REVOKE_CONFIRM.format(
                    user=human_username(user), tg_id=tg_id
                ),
                reply_markup=revoke_confirm_kb(tg_id),
            )

    @router.callback_query(F.data.startswith("adm:revoke_yes:"), is_admin)
    async def cb_revoke_yes(call: CallbackQuery) -> None:
        raw = (call.data or "").split(":")[-1]
        if not raw.lstrip("-").isdigit():
            await call.answer("Не понял ID", show_alert=True)
            return
        await call.answer("Забираю доступ…")
        text = await revoke_now(int(raw))
        if call.message is not None:
            await call.message.answer(text, reply_markup=back_to_panel_kb())

    # --------------------------------------------- отмена автосписания

    @router.callback_query(F.data == "adm:cancel_sub", is_admin)
    async def cb_cancel_sub(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.cancel_user)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_CANCEL_ASK, reply_markup=cancel_kb())

    @router.message(AdminStates.cancel_user, is_admin, F.text)
    async def cancel_sub_user(message: Message, state: FSMContext) -> None:
        tg_id, _user = await resolve_target(message)
        await state.clear()
        if tg_id is None:
            await message.answer(texts.ADMIN_USER_NOT_FOUND, reply_markup=back_to_panel_kb())
            return
        await db.upsert_user(int(tg_id))
        outcome = await subscriptions.cancel(int(tg_id), as_manager=True)
        user = await db.get_user(int(tg_id))
        remote = (
            "автосписание отключено ✅"
            if outcome.remote_ok
            else f"не подтверждено, отключите вручную в кабинете ({outcome.error})"
        )
        await message.answer(
            texts.ADMIN_CANCEL_DONE.format(
                user=human_username(user),
                date=fmt_date(outcome.paid_until, settings.timezone),
                remote=remote,
            ),
            reply_markup=back_to_panel_kb(),
        )

    # ------------------------------------------------------------- поиск

    @router.callback_query(F.data == "adm:find", is_admin)
    async def cb_find(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.find_user)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_ASK_USER_FIND, reply_markup=cancel_kb())

    @router.message(AdminStates.find_user, is_admin, F.text)
    async def find_user(message: Message, state: FSMContext) -> None:
        tg_id, user = await resolve_target(message)
        await state.clear()
        if tg_id is None:
            await message.answer(texts.ADMIN_USER_NOT_FOUND, reply_markup=back_to_panel_kb())
            return
        user = user or await db.get_user(int(tg_id))
        if user is None:
            await db.upsert_user(int(tg_id))
            user = await db.get_user(int(tg_id))
        assert user is not None
        last_payment = await db.last_successful_payment(int(tg_id))
        await message.answer(
            texts.ADMIN_USER_CARD.format(
                username=human_username(user),
                tg_id=user["tg_id"],
                first_name=user.get("first_name") or "—",
                status=STATUS_LABELS.get(user["status"], user["status"]),
                paid_until=fmt_datetime(user.get("paid_until"), settings.timezone),
                last_payment=(
                    f"{fmt_price(last_payment['amount'], settings.currency)}, "
                    f"{fmt_datetime(last_payment['created_at'], settings.timezone)}"
                    if last_payment
                    else "—"
                ),
                autorenew="включено" if int(user.get("auto_renew", 1)) else "отключено",
                source=user.get("access_source") or "—",
                phone=user.get("phone") or "—",
                created=fmt_date(user.get("created_at"), settings.timezone),
            ),
            reply_markup=user_card_kb(int(user["tg_id"])),
        )

    # ---------------------------------------------------------- промокоды

    @router.callback_query(F.data == "adm:promos", is_admin)
    async def cb_promos(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer()
        promos = await db.list_promos()
        if not promos:
            text = texts.ADMIN_PROMO_LIST_EMPTY
        else:
            lines = []
            for promo in promos:
                expires = promo["expires_at"]
                lines.append(
                    f"• <code>{promo['code']}</code> — скидка {promo['discount_percent']}%, "
                    f"использован {promo['used_count']}/"
                    f"{promo['max_uses'] or '∞'}, "
                    f"{'активен' if promo['active'] else 'выключен'}"
                    + (f", до {fmt_date(expires, settings.timezone)}" if expires else "")
                )
            text = texts.ADMIN_PROMO_LIST_HEADER + "\n".join(lines)
        if call.message is not None:
            await call.message.edit_text(text, reply_markup=promos_kb(promos))

    @router.callback_query(F.data.startswith("adm:promo_off:"), is_admin)
    async def cb_promo_off(call: CallbackQuery) -> None:
        code = (call.data or "").split(":", 2)[-1]
        ok = await db.deactivate_promo(code)
        await call.answer("Промокод выключен" if ok else "Не найден")
        if call.message is not None:
            promos = await db.list_promos()
            await call.message.edit_reply_markup(reply_markup=promos_kb(promos))

    @router.callback_query(F.data == "adm:promo_new", is_admin)
    async def cb_promo_new(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.promo_code)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_PROMO_ASK_CODE, reply_markup=cancel_kb())

    @router.message(AdminStates.promo_code, is_admin, F.text)
    async def promo_code(message: Message, state: FSMContext) -> None:
        code = (message.text or "").strip().upper().replace(" ", "")
        if not PROMO_CODE_RE.match(code):
            await message.answer(
                "Код должен быть от 3 до 32 символов: латиница, цифры, «-» и «_». Попробуйте снова.",
                reply_markup=cancel_kb(),
            )
            return
        await state.update_data(code=code)
        await state.set_state(AdminStates.promo_percent)
        await message.answer(texts.ADMIN_PROMO_ASK_PERCENT, reply_markup=cancel_kb())

    @router.message(AdminStates.promo_percent, is_admin, F.text)
    async def promo_percent(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip().replace("%", "")
        if not text.isdigit() or not 1 <= int(text) <= 100:
            await message.answer(texts.ADMIN_BAD_NUMBER, reply_markup=cancel_kb())
            return
        await state.update_data(discount=int(text))
        await state.set_state(AdminStates.promo_uses)
        await message.answer(texts.ADMIN_PROMO_ASK_USES, reply_markup=cancel_kb())

    @router.message(AdminStates.promo_uses, is_admin, F.text)
    async def promo_uses(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text.isdigit():
            await message.answer(texts.ADMIN_BAD_NUMBER, reply_markup=cancel_kb())
            return
        await state.update_data(uses=int(text))
        await state.set_state(AdminStates.promo_days)
        await message.answer(texts.ADMIN_PROMO_ASK_DAYS, reply_markup=cancel_kb())

    @router.message(AdminStates.promo_days, is_admin, F.text)
    async def promo_days(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text.isdigit():
            await message.answer(texts.ADMIN_BAD_NUMBER, reply_markup=cancel_kb())
            return
        days = int(text)
        data = await state.get_data()
        await state.clear()
        code = data.get("code")
        discount = int(data.get("discount", 0))
        uses = int(data.get("uses", 0))
        expires_at = to_iso(utcnow() + timedelta(days=days)) if days else None
        if not code or not discount:
            await message.answer(texts.ADMIN_CANCELLED, reply_markup=back_to_panel_kb())
            return
        await db.create_promo(code, discount, uses, expires_at)
        promo_price = settings.price - round(settings.price * discount / 100)
        await message.answer(
            texts.ADMIN_PROMO_CREATED.format(
                code=code,
                discount=discount,
                uses=uses or "∞",
                expires=fmt_date(expires_at, settings.timezone) if expires_at else "бессрочно",
                price=fmt_price(promo_price, settings.currency),
            ),
            reply_markup=back_to_panel_kb(),
        )
        log.info("Создан промокод %s (%s%%, лимит %s)", code, discount, uses)

    return router


__all__ = ["build_router", "AdminStates"]
