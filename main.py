#!/usr/bin/env python3
"""Запуск бота. Дублирует run.py — на случай, если хостинг ищет main.py."""

from __future__ import annotations

import asyncio
import logging

from run import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("club").info("Остановлено вручную")
