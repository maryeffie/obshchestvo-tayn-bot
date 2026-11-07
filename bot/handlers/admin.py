"""Админ-панель: статистика, ручная выдача/отзыв доступа, промокоды."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

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
    back_to_panel_kb,
    cancel_kb,
    promos_kb,
)
from ..utils import (
    fmt_date,
    fmt_datetime,
    fmt_price,
    human_username,
    to_iso,
    utcnow,
)

log = logging.getLogger(__name__)

PROMO_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")
STATUS_LABELS = {"new": "новичок", "active": "активен", "expired": "доступ закрыт"}


class AdminStates(StatesGroup):
    grant_user = State()
    grant_days = State()
    revoke_user = State()
    find_user = State()
    cancel_user = State()
    promo_code = State()
    promo_percent = State()
    promo_uses = State()
    promo_days = State()


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

    @router.message(Command("stats"), is_admin)
    async def cmd_stats(message: Message) -> None:
        await message.answer(await _stats_text(), reply_markup=back_to_panel_kb())

    async def _stats_text() -> str:
        stats = await db.stats()
        return texts.ADMIN_STATS.format(
            users_total=stats["users_total"],
            active=stats["active"],
            expiring_3d=stats["expiring_3d"],
            users_new_month=stats["users_new_month"],
            payments_month=stats["payments_month"],
            revenue_month=fmt_price(stats["revenue_month"], settings.currency),
            payments_total=stats["payments_total"],
            revenue_total=fmt_price(stats["revenue_total"], settings.currency),
        )

    # ----------------------------------------------------------- панель

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
            await call.message.answer(texts.ADMIN_ASK_USER, reply_markup=cancel_kb())

    @router.message(AdminStates.grant_user, is_admin, F.text)
    async def grant_user(message: Message, state: FSMContext) -> None:
        tg_id, user = await resolve_target(message)
        if tg_id is None:
            await message.answer(texts.ADMIN_USER_NOT_FOUND, reply_markup=cancel_kb())
            return
        await state.update_data(tg_id=tg_id)
        await state.set_state(AdminStates.grant_days)
        await message.answer(
            texts.ADMIN_ASK_DAYS.format(days=settings.subscription_days), reply_markup=cancel_kb()
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

    # ---------------------------------------------------------- отзыв доступа

    @router.callback_query(F.data == "adm:revoke", is_admin)
    async def cb_revoke(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdminStates.revoke_user)
        await call.answer()
        if call.message is not None:
            await call.message.answer(texts.ADMIN_ASK_USER, reply_markup=cancel_kb())

    @router.message(AdminStates.revoke_user, is_admin, F.text)
    async def revoke_user(message: Message, state: FSMContext) -> None:
        tg_id, user = await resolve_target(message)
        await state.clear()
        if tg_id is None:
            await message.answer(texts.ADMIN_REVOKE_NOT_FOUND, reply_markup=back_to_panel_kb())
            return
        await db.upsert_user(int(tg_id))
        await db.set_status(int(tg_id), "expired")
        await access.kick(int(tg_id))
        user = user or await db.get_user(int(tg_id))
        await message.answer(
            texts.ADMIN_REVOKED.format(user=human_username(user)), reply_markup=back_to_panel_kb()
        )

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
            await call.message.answer(texts.ADMIN_ASK_USER, reply_markup=cancel_kb())

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
            reply_markup=back_to_panel_kb(),
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
