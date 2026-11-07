#!/usr/bin/env python3
"""Отправляет подписанный вебхук Prodamus на наш сервер — проверка сценария без денег.

Примеры:
    python scripts/simulate_payment.py --tg-id 123456789
    python scripts/simulate_payment.py --tg-id 123456789 --status fail
    python scripts/simulate_payment.py --tg-id 123456789 --url https://bot.example.com/prodamus/webhook
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from bot.prodamus import sign_payload  # noqa: E402

load_dotenv(ROOT / ".env")


def build_payload(args: argparse.Namespace) -> dict:
    status = "success" if args.status == "success" else "fail"
    return {
        "order_id": args.order_id or f"tg{args.tg_id}-sim{os.getpid()}",
        "payment_status": status,
        "payment_status_description": (
            "Оплачено" if status == "success" else "Недостаточно средств"
        ),
        "sum": f"{args.amount}.00",
        "customer_extra": f"tg:{args.tg_id}",
        "customer_phone": args.phone,
        "subscription": args.subscription or "",
        "callbackType": "json",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Симуляция вебхука Prodamus")
    parser.add_argument("--tg-id", type=int, required=True, help="Telegram ID получателя")
    parser.add_argument("--amount", type=int, default=790, help="Сумма платежа")
    parser.add_argument("--status", choices=["success", "fail"], default="success")
    parser.add_argument("--order-id", default=None, help="order_id существующего заказа")
    parser.add_argument(
        "--url",
        default=os.getenv("SIMULATE_URL")
        or f"http://127.0.0.1:{os.getenv('WEBHOOK_PORT', '8080')}"
        + (os.getenv("WEBHOOK_PATH") or "/prodamus/webhook"),
    )
    parser.add_argument("--secret", default=os.getenv("PRODAMUS_SECRET"))
    parser.add_argument("--phone", default="+79990001122")
    parser.add_argument("--subscription", default=os.getenv("PRODAMUS_SUBSCRIPTION_ID", ""))
    args = parser.parse_args()

    if not args.secret:
        print("Нужен PRODAMUS_SECRET (в .env или через --secret)")
        return 2

    payload = build_payload(args)
    signature = sign_payload(payload, args.secret)

    print(f"POST {args.url}")
    print(f"payload: {payload}")

    async with aiohttp.ClientSession() as session:
        async with session.post(args.url, data=payload, headers={"Sign": signature}) as response:
            body = await response.text()
            print(f"\nHTTP {response.status}: {body}")
            if response.status != 200:
                print(
                    "\nПодсказка: 400 = не совпала подпись (проверьте PRODAMUS_SECRET "
                    "и что сервер запущен)."
                )
                return 1

    print(
        "\nГотово. Проверьте Telegram: пользователю должно прийти сообщение со ссылками "
        "(для success) или кнопкой «Повторить оплату» (для fail)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
