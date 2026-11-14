"""Пользовательские сценарии: /start, оплата, промокод, статус."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from .. import texts
from ..app import App
from ..keyboards import (
    cancel_confirm_kb,
    pay_url_kb,
    foreign_pay_kb,
    member_kb,
    paywall_kb,
    paywall_kb_with_promo_reset,
    promo_cancel_kb,
    quick_grant_kb,
    return_kb,
    welcome_kb,
)
from ..utils import fmt_date, fmt_price, from_iso, human_username, is_active

log = logging.getLogger(__name__)


class PromoStates(StatesGroup):
    waiting_code = State()


def build_router(app: App) -> Router:
    router = Router(name="user")
    settings = app.settings
    bot = app.bot
    app_prodamus = app.prodamus
    db = app.db
    access = app.access
    payments = app.payments
    subscriptions = app.subscriptions
    price_label = fmt_price(settings.price, settings.currency)

    # ------------------------------------------------------------------ утилиты

    async def send_paywall(message: Message, tg_id: int, promo_code: str | None = None) -> None:
        url, amount, _order_id, discount = await payments.create_payment_link(
            tg_id, promo_code=promo_code
        )
        amount_label = fmt_price(amount, settings.currency)
        if discount and promo_code:
            text = texts.BEFORE_PAYMENT_WITH_PROMO.format(
                promo=promo_code,
                discount=round(discount * 100 / settings.price),
                price=amount_label,
                base_price=price_label,
            )
            keyboard = paywall_kb_with_promo_reset(url, amount_label)
        else:
            text = texts.BEFORE_PAYMENT.format(price=price_label)
            keyboard = paywall_kb(
                url,
                amount_label,
                with_foreign=bool(settings.prodamus_foreign_url),
            )
        await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)

    def can_cancel(user: dict | None) -> bool:
        """Кнопка отмены есть у любого действующего участника.

        В режиме автосписаний она отключает списания в Prodamus, в бесплатном
        режиме — просто прекращает напоминания о продлении.
        """
        return bool(is_active(user) and int(user.get("auto_renew", 1)) == 1)

    async def send_status(message: Message, tg_id: int) -> None:
        user = await db.get_user(tg_id) or await db.upsert_user(tg_id)
        if is_active(user):
            until = from_iso(user["paid_until"])
            until_label = fmt_date(until, settings.timezone)
            if int(user.get("auto_renew", 1)) == 0:
                template = (
                    texts.RENEWAL_LINE_OFF if settings.is_recurrent
                    else texts.RENEWAL_LINE_OFF_MANUAL
                )
                renewal_line = template.format(date=until_label)
            elif settings.is_recurrent:
                renewal_line = texts.RENEWAL_LINE_AUTO.format(
                    date=until_label, price=price_label
                )
            else:
                renewal_line = texts.RENEWAL_LINE_MANUAL.format(date=until_label)
            await message.answer(
                texts.STATUS_ACTIVE.format(
                    date=until_label, price=price_label, renewal_line=renewal_line
                ),
                reply_markup=member_kb(show_cancel=can_cancel(user)),
            )
        elif user.get("status") == "expired":
            await message.answer(
                texts.STATUS_EXPIRED.format(price=price_label), reply_markup=return_kb()
            )
        else:
            await message.answer(
                texts.STATUS_NEW.format(price=price_label), reply_markup=welcome_kb()
            )

    @router.callback_query(F.data == "cur:menu")
    async def cb_currency_menu(call: CallbackQuery) -> None:
        await call.answer()
        if call.message is None or call.from_user is None:
            return
        text = texts.FOREIGN_CHOOSE.format(days=settings.subscription_days)
        try:
            # бессрочная ссылка Prodamus: валюта и сумма считаются на её стороне
            url, _amount, _order_id, _currency = await payments.create_foreign_link(
                call.from_user.id
            )
        except ValueError:
            await call.message.answer(texts.FOREIGN_UNAVAILABLE)
            return
        await call.message.answer(
            text, reply_markup=foreign_pay_kb(url), disable_web_page_preview=True
        )

    async def ensure_user(message: Message) -> dict:
        assert message.from_user is not None
        return await db.upsert_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )

    # ---------------------------------------------------------------- команды

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = await ensure_user(message)
        if is_active(user):
            until = from_iso(user["paid_until"])
            await message.answer(
                texts.ALREADY_MEMBER.format(date=fmt_date(until, settings.timezone)),
                reply_markup=member_kb(show_cancel=can_cancel(user)),
            )
        else:
            await message.answer(
                texts.WELCOME.format(price=price_label), reply_markup=welcome_kb()
            )

    @router.message(Command("status", "subscription", "podpiska"))
    async def cmd_status(message: Message, state: FSMContext) -> None:
        await state.clear()
        assert message.from_user is not None
        await ensure_user(message)
        await send_status(message, message.from_user.id)

    @router.message(Command("pay1"))
    async def cmd_pay1(message: Message, state: FSMContext) -> None:
        """Тестовая оплата за 1 ₽ — доступна всем, пока включена в настройках.

        Пока идёт проверка оплаты, Марина может заплатить с любого аккаунта.
        Когда тесты закончатся, выключите PAY1_ENABLED — команда замолчит.
        """
        if message.from_user is None:
            return
        await state.clear()
        if not settings.pay1_enabled:
            await message.answer(texts.PAY1_OFF)
            return
        allowed, used = await db.bump_counter("pay1", settings.pay1_limit)
        if not allowed:
            await message.answer(texts.PAY1_LIMIT.format(limit=settings.pay1_limit))
            return

        tg_id = message.from_user.id
        await db.upsert_user(tg_id, message.from_user.username, message.from_user.first_name)
        discount = max(1, settings.price - 1)
        order_id = payments.new_order_id(tg_id)
        await db.create_order(
            order_id=order_id,
            tg_id=tg_id,
            amount=settings.price,
            is_recurring=settings.is_recurrent,
        )
        url = app_prodamus.payment_url(tg_id, order_id, settings.price, discount)
        await message.answer(
            texts.PAY1_LINK.format(
                price=price_label,
                discount=fmt_price(discount, settings.currency),
                one=fmt_price(1, settings.currency),
                left=max(0, settings.pay1_limit - used),
            ),
            reply_markup=pay_url_kb(url, "Оплатить 1 ₽"),
        )
        await access.notify_admins(
            "🧪 <b>Тестовая оплата за 1 ₽</b>\n\n"
            f"Пользователь: {human_username(await db.get_user(tg_id))}\n"
            f"ID: <code>{tg_id}</code>\n"
            f"Осталось тестовых оплат: {max(0, settings.pay1_limit - used)}"
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            texts.HELP.format(support=settings.support_contact or "администратора клуба")
        )

    # ---------------------------------------------------------- оплата / вступление

    @router.callback_query(F.data == "join")
    async def cb_join(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer()
        if call.message is None or call.from_user is None:
            return
        await db.upsert_user(
            call.from_user.id, call.from_user.username, call.from_user.first_name
        )
        await send_paywall(call.message, call.from_user.id)

    @router.callback_query(F.data == "promo:enter")
    async def cb_promo_enter(call: CallbackQuery, state: FSMContext) -> None:
        await call.answer()
        if call.message is None:
            return
        await state.set_state(PromoStates.waiting_code)
        await call.message.answer(texts.PROMO_ASK, reply_markup=promo_cancel_kb())

    @router.message(PromoStates.waiting_code)
    async def promo_code_received(message: Message, state: FSMContext) -> None:
        code = (message.text or "").strip()
        await state.clear()
        if not code:
            await message.answer(texts.PROMO_BAD.format(reason="Пустой промокод."))
            return
        ok, reason = await db.promo_is_valid(code)
        if not ok:
            await message.answer(
                texts.PROMO_BAD.format(reason=reason), reply_markup=welcome_kb()
            )
            return
        promo = await db.get_promo(code)
        assert promo is not None and message.from_user is not None
        await message.answer(texts.PROMO_OK.format(discount=promo["discount_percent"]))
        await db.upsert_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name
        )
        await send_paywall(message, message.from_user.id, promo_code=promo["code"])

    @router.callback_query(F.data == "promo:cancel")
    async def cb_promo_cancel(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.answer(texts.PROMO_CANCELLED, show_alert=False)
        if call.message is not None:
            await call.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data == "promo:reset")
    async def cb_promo_reset(call: CallbackQuery) -> None:
        await call.answer()
        if call.message is not None and call.from_user is not None:
            await send_paywall(call.message, call.from_user.id)

    @router.callback_query(F.data == "retry")
    async def cb_retry(call: CallbackQuery) -> None:
        await call.answer()
        if call.message is not None and call.from_user is not None:
            await send_paywall(call.message, call.from_user.id)

    # ------------------------------------------------------------------- статус

    @router.callback_query(F.data == "status")
    async def cb_status(call: CallbackQuery) -> None:
        await call.answer()
        if call.message is not None and call.from_user is not None:
            await send_status(call.message, call.from_user.id)

    @router.callback_query(F.data == "links:resend")
    async def cb_resend_links(call: CallbackQuery) -> None:
        await call.answer()
        if call.message is None or call.from_user is None:
            return
        user = await db.get_user(call.from_user.id)
        if not is_active(user):
            await send_paywall(call.message, call.from_user.id)
            return
        ok, error = await access.send_links_again(
            call.from_user.id, show_cancel=can_cancel(user)
        )
        if not ok:
            await call.message.answer(
                texts.LINKS_ERROR.format(support=settings.support_contact or "администратора")
            )
            log.warning("Не удалось переотправить ссылки %s: %s", call.from_user.id, error)

    # ------------------------------------------------- «Я оплатил» — сигнал админу

    @router.callback_query(F.data == "paid:notify")
    async def cb_paid_notify(call: CallbackQuery) -> None:
        if call.from_user is None:
            return
        await call.answer("Передали администратору ✅", show_alert=False)
        tg_id = call.from_user.id
        user = await db.get_user(tg_id) or await db.upsert_user(
            tg_id, call.from_user.username, call.from_user.first_name
        )
        text = texts.PAID_CLAIM_ADMIN.format(
            user=human_username(user), tg_id=tg_id, days=settings.subscription_days
        )
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text, reply_markup=quick_grant_kb(tg_id))
            except TelegramAPIError:
                log.warning("Не удалось уведомить админа %s о заявке на оплату", admin_id)
        if call.message is not None:
            await call.message.answer(
                texts.PAID_CLAIM_USER.format(
                    support=settings.support_contact or "администратора клуба"
                )
            )

    # -------------------------------------------------— отмена подписки

    @router.callback_query(F.data == "sub:cancel")
    async def cb_cancel(call: CallbackQuery) -> None:
        if call.message is None or call.from_user is None:
            return
        user = await db.get_user(call.from_user.id)
        if not is_active(user):
            await call.answer(texts.CANCEL_UNAVAILABLE.replace("<b>", "").replace("</b>", ""),
                              show_alert=True)
            return
        if not can_cancel(user):
            await call.answer("Автосписание уже отключено", show_alert=True)
            await call.message.answer(
                texts.CANCEL_ALREADY, reply_markup=member_kb(show_cancel=False)
            )
            return
        await call.answer()
        until = from_iso(user["paid_until"])
        ask = texts.CANCEL_ASK if settings.is_recurrent else texts.CANCEL_ASK_MANUAL
        await call.message.answer(
            ask.format(date=fmt_date(until, settings.timezone)),
            reply_markup=cancel_confirm_kb(),
        )

    @router.callback_query(F.data == "sub:cancel_yes")
    async def cb_cancel_yes(call: CallbackQuery) -> None:
        if call.message is None or call.from_user is None:
            return
        await call.answer()
        outcome = await subscriptions.cancel(call.from_user.id)
        if not outcome.local_ok:
            await call.message.answer(
                texts.CANCEL_UNAVAILABLE, reply_markup=member_kb()
            )
            return
        until_label = fmt_date(outcome.paid_until, settings.timezone)
        if not settings.is_recurrent:
            text = texts.CANCEL_DONE_MANUAL.format(date=until_label)
        elif outcome.remote_ok:
            text = texts.CANCEL_DONE.format(date=until_label)
        else:
            text = texts.CANCEL_DONE_WARN.format(
                date=until_label,
                support=settings.support_contact or "администратора клуба",
            )
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 — сообщение могло устареть
            pass
        await call.message.answer(
            text, reply_markup=member_kb(show_cancel=False), disable_web_page_preview=True
        )

    @router.callback_query(F.data == "sub:cancel_no")
    async def cb_cancel_no(call: CallbackQuery) -> None:
        await call.answer(texts.CANCEL_KEPT)
        if call.message is not None:
            await call.message.edit_reply_markup(
                reply_markup=member_kb(show_cancel=True)
            )

    # ------------------------------------------------------------- прочий текст

    @router.message(F.text)
    async def fallback(message: Message, state: FSMContext) -> None:
        """Любое сообщение без активного сценария — показываем меню."""
        current = await state.get_state()
        if current is not None:
            return
        user = await ensure_user(message)
        if is_active(user):
            until = from_iso(user["paid_until"])
            await message.answer(
                texts.ALREADY_MEMBER.format(date=fmt_date(until, settings.timezone)),
                reply_markup=member_kb(show_cancel=can_cancel(user)),
            )
        else:
            await message.answer(
                texts.WELCOME.format(price=price_label), reply_markup=welcome_kb()
            )

    return router


__all__ = ["build_router", "PromoStates"]
