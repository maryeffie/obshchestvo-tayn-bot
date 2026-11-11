"""Клавиатуры и кнопки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


# ------------------------------------------------------------------ пользователь

def welcome_kb() -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text="✨ Вступить в Общество тайн", callback_data="join")]
    )


# ----------------------------------------------------------- страница оплаты

def paywall_kb(
    pay_url: str, price_label: str, *, with_foreign: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"💳 Оплатить в рублях {price_label}", url=pay_url)]
    ]
    if with_foreign:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🌍 Оплатить в другой валюте", callback_data="cur:menu"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✅ Я оплатил — проверить", callback_data="paid:notify")]
    )
    return _kb(*rows)




def foreign_pay_kb(pay_url: str) -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],
        [
            InlineKeyboardButton(text="↩️ Назад", callback_data="join"),
            InlineKeyboardButton(text="📅 Моя подписка", callback_data="status"),
        ],
    )


def quick_grant_kb(tg_id: int) -> InlineKeyboardMarkup:
    """Кнопка админу: выдать доступ человеку, который сообщил об оплате."""
    return _kb(
        [InlineKeyboardButton(text="🎁 Выдать доступ на 30 дней", callback_data=f"adm:quickgrant:{tg_id}")],
        [InlineKeyboardButton(text="📅 Моя подписка", callback_data="status")],
    )


def paywall_kb_with_promo_reset(pay_url: str, price_label: str) -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text=f"💳 Оплатить в рублях {price_label}", url=pay_url)],
        [InlineKeyboardButton(text="↩️ Убрать промокод", callback_data="promo:reset")],
    )


def access_kb(
    channel_url: str, chat_url: str, show_cancel: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📖 Перейти в закрытый канал", url=channel_url)],
        [InlineKeyboardButton(text="💬 Перейти в чат обсуждений", url=chat_url)],
    ]
    if show_cancel:
        rows.append(
            [InlineKeyboardButton(text="Отменить подписку", callback_data="sub:cancel")]
        )
    return _kb(*rows)


def member_kb(show_cancel: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔗 Получить новые ссылки", callback_data="links:resend")],
        [InlineKeyboardButton(text="📅 Моя подписка", callback_data="status")],
    ]
    if show_cancel:
        rows.append(
            [InlineKeyboardButton(text="Отменить подписку", callback_data="sub:cancel")]
        )
    return _kb(*rows)


def cancel_confirm_kb() -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text="✅ Да, отменить подписку", callback_data="sub:cancel_yes")],
        [InlineKeyboardButton(text="↩️ Оставить как есть", callback_data="sub:cancel_no")],
        [InlineKeyboardButton(text="📅 Моя подписка", callback_data="status")],
    )


def retry_kb() -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="🔄 Повторить оплату", callback_data="join")])


def renew_url_kb(pay_url: str, price_label: str) -> InlineKeyboardMarkup:
    """Продление в один клик: кнопка сразу ведёт на страницу оплаты."""
    return _kb(
        [InlineKeyboardButton(text="💳 Продлить подписку", url=pay_url)],
        [InlineKeyboardButton(text="📅 Моя подписка", callback_data="status")],
    )


def renew_kb(price_label: str) -> InlineKeyboardMarkup:
    """Кнопка продления в напоминаниях (бесплатный режим — продление вручную)."""
    return _kb(
        [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="join")]
    )


def return_kb() -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="🔑 Вернуться в клуб", callback_data="join")])


def promo_cancel_kb() -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="Отмена", callback_data="promo:cancel")])


# ------------------------------------------------------------------- админ-панель

def admin_kb() -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [
            InlineKeyboardButton(text="🎁 Выдать доступ", callback_data="adm:grant"),
            InlineKeyboardButton(text="🚫 Забрать доступ", callback_data="adm:revoke"),
        ],
        [InlineKeyboardButton(text="👤 Найти участника", callback_data="adm:find")],
        [
            InlineKeyboardButton(
                text="🔁 Отменить автосписание", callback_data="adm:cancel_sub"
            )
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:panel")],
    )


def promos_kb(promos: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать промокод", callback_data="adm:promo_new")
    for promo in promos:
        if promo["active"]:
            builder.button(
                text=f"⛔ Отключить {promo['code']}",
                callback_data=f"adm:promo_off:{promo['code']}",
            )
    builder.button(text="⬅️ Назад", callback_data="adm:panel")
    builder.adjust(1)
    return builder.as_markup()


def back_to_panel_kb() -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="adm:panel")])


def cancel_kb() -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="Отмена", callback_data="adm:panel")])
