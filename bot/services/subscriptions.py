"""Управление подпиской участника: отмена автосписания (кнопка в боте и админка).

Логика отмены:
  1. вызываем Prodamus Rest API setActivity и выключаем автосписание;
  2. в нашей базе ставим auto_renew = 0 — доступ остаётся до конца оплаченного периода,
     напоминания о продлении больше не приходят;
  3. в день окончания периода бот исключает участника (без дня отсрочки — деньги
     больше не ждём);
  4. Марине приходит уведомление. Если Prodamus не ответил — в уведомлении написано,
     что автосписание нужно выключить вручную в кабинете.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..db import Database
from ..prodamus import ProdamusClient, SubscriptionResult
from ..utils import fmt_date, from_iso, human_username, utcnow
from .access import AccessService

log = logging.getLogger(__name__)


@dataclass
class CancelOutcome:
    local_ok: bool                 # отметили отмену в нашей базе
    remote_ok: bool                # Prodamus подтвердил отключение автосписания
    paid_until: datetime | None    # до какой даты сохраняется доступ
    error: str | None = None


class SubscriptionService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        prodamus: ProdamusClient,
        access: AccessService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.prodamus = prodamus
        self.access = access

    # ------------------------------------------------------------------ отмена

    async def cancel(
        self,
        tg_id: int,
        *,
        as_manager: bool = False,
        notify: bool = True,
    ) -> CancelOutcome:
        """Отключает автосписание. Доступ сохраняется до конца оплаченного периода."""
        user = await self.db.get_user(tg_id)
        if user is None:
            return CancelOutcome(local_ok=False, remote_ok=False, paid_until=None,
                                 error="пользователь не найден")

        # Бесплатный режим: автосписаний нет, отключать в Prodamus нечего —
        # просто перестаём напоминать о продлении.
        manual = not self.settings.is_recurrent
        if manual:
            result = SubscriptionResult(ok=True, message="автосписания не подключены",
                                        tried="none", remote=False)
        else:
            result = await self.prodamus.set_subscription_activity(
                active=False,
                as_manager=as_manager,
                tg_user_id=tg_id,
                phone=user.get("phone"),
                email=user.get("email"),
            )

        await self.db.set_auto_renew(tg_id, False, cancelled=True)
        paid_until = from_iso(user.get("paid_until"))

        if notify:
            await self.access.notify_admins(
                self._admin_notice(
                    user, result.ok, result.message, as_manager, paid_until, manual=manual
                )
            )

        log.info(
            "Подписка отменена: tg_id=%s, режим=%s, Prodamus=%s (%s)",
            tg_id,
            "бесплатный (ручное продление)" if manual
            else ("менеджер" if as_manager else "пользователь"),
            "ok" if result.ok else "не подтверждено",
            result.message,
        )
        return CancelOutcome(
            local_ok=True,
            remote_ok=result.ok,
            paid_until=paid_until,
            error=None if result.ok else result.message,
        )

    # ------------------------------------------------------- вспомогательное

    def _admin_notice(
        self,
        user: dict,
        remote_ok: bool,
        message: str,
        as_manager: bool,
        paid_until: datetime | None,
        manual: bool = False,
    ) -> str:
        who = human_username(user)
        until = fmt_date(paid_until, self.settings.timezone) if paid_until else "—"
        if manual:
            header = "🚪 <b>Участник не будет продлевать подписку</b>"
        elif as_manager:
            header = "🔁 <b>Автосписание отключено администратором</b>"
        else:
            header = "🚪 <b>Участник отменил подписку</b>"
        text = (
            f"{header}\n\n"
            f"Пользователь: {who}\n"
            f"ID: <code>{user['tg_id']}</code>\n"
            f"Доступ сохраняется до: <b>{until}</b>\n\n"
        )
        if manual:
            text += (
                "Клуб работает в бесплатном режиме (ручное продление), поэтому в Prodamus "
                "ничего выключать не нужно. Напоминания о продлении этому человеку больше "
                "не приходят, доступ закроется ровно в дату окончания."
            )
        elif remote_ok:
            text += "В Prodamus автосписание выключено — деньги больше списываться не будут."
        else:
            text += (
                "⚠️ <b>Prodamus не подтвердил отключение.</b>\n"
                f"Ответ: <code>{message}</code>\n\n"
                "Выключите автосписание вручную: Prodamus → «Подписчики» → найти по "
                f"телефону/ID <code>{user['tg_id']}</code> → переключатель «Статус». "
                "В базе бота отмена уже отмечена, напоминания и авто-кик сработают по расписанию."
            )
        if paid_until and paid_until > utcnow():
            text += "\n\nДоступ закроется автоматически в день окончания периода."
        return text


__all__ = ["SubscriptionService", "CancelOutcome"]
