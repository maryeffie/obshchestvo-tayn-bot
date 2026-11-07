"""Тесты самих хендлеров: регистрация роутеров и ключевые ответы бота."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.handlers import admin as admin_handlers
from bot.handlers import user as user_handlers

from .helpers import make_app


class FakeMessage:
    def __init__(self, text: str = "/start", user_id: int = 555) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, username=f"user{user_id}", first_name="Тест")
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.forward_origin = None
        self.answers: list[dict] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append({"text": text, "keyboard": reply_markup})
        return self

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.answers.append({"text": "", "keyboard": reply_markup})
        return self

    @property
    def last_text(self) -> str:
        return self.answers[-1]["text"]


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage, user_id: int = 555) -> None:
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id, username=f"user{user_id}", first_name="Тест")
        self.answered: list[str] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answered.append(text or "")


class FakeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def clear(self) -> None:
        self.data.clear()
        self.state = None

    async def set_state(self, state) -> None:
        self.state = state

    async def get_state(self):
        return self.state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


def _handler(router, event: str, name: str):
    observer = getattr(router, event)
    for item in observer.handlers:
        if item.callback.__name__ == name:
            return item.callback
    raise AssertionError(f"Хендлер {name} не зарегистрирован в {event}")


def _buttons(keyboard) -> list[str]:
    if keyboard is None:
        return []
    return [button.text for row in keyboard.inline_keyboard for button in row]


def test_routers_register_handlers() -> None:
    async def scenario() -> None:
        app = await make_app()
        user_router = user_handlers.build_router(app)  # type: ignore[arg-type]
        admin_router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        assert len(user_router.message.handlers) >= 5
        assert len(user_router.callback_query.handlers) >= 7
        assert len(admin_router.message.handlers) >= 10
        assert len(admin_router.callback_query.handlers) >= 6
        await app.db.close()

    asyncio.run(scenario())


def test_start_and_join_flow() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        state = FakeState()

        message = FakeMessage("/start", user_id=555)
        await _handler(router, "message", "cmd_start")(message, state)
        assert "Добро пожаловать в «Общество тайн»" in message.last_text
        assert _buttons(message.answers[-1]["keyboard"]) == ["Вступить в Общество тайн"]

        call = FakeCallback("join", FakeMessage(user_id=555))
        await _handler(router, "callback_query", "cb_join")(call, state)
        paywall = call.message.answers[-1]
        assert "Остался всего один шаг!" in paywall["text"]
        assert "Оплатить 790 ₽" in _buttons(paywall["keyboard"])
        url_button = paywall["keyboard"].inline_keyboard[0][0]
        assert url_button.url.startswith("https://demo.payform.ru/?")
        assert "subscription=4242" in url_button.url

        await app.db.close()

    asyncio.run(scenario())


def test_promo_entry_recalculates_price() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        state = FakeState()
        await app.db.create_promo("TAJNA20", 20, max_uses=10)

        message = FakeMessage("tajna20", user_id=556)
        await _handler(router, "message", "promo_code_received")(message, state)

        assert "Промокод принят" in message.answers[0]["text"]
        paywall = message.answers[-1]
        assert "скидка 20%" in paywall["text"]
        assert "Оплатить 632 ₽" in _buttons(paywall["keyboard"])
        assert "Убрать промокод" in _buttons(paywall["keyboard"])

        await app.db.close()

    asyncio.run(scenario())


def test_wrong_promo_does_not_open_paywall() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = user_handlers.build_router(app)  # type: ignore[arg-type]
        message = FakeMessage("НЕТТАКОГО", user_id=557)
        await _handler(router, "message", "promo_code_received")(message, FakeState())
        assert "Промокод не подошёл" in message.last_text
        assert len(message.answers) == 1
        await app.db.close()

    asyncio.run(scenario())


def test_admin_panel_available_for_admin() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        message = FakeMessage("/admin", user_id=111)
        await _handler(router, "message", "cmd_admin")(message, FakeState())
        assert "Админ-панель" in message.last_text
        buttons = _buttons(message.answers[-1]["keyboard"])
        assert "🎁 Выдать доступ" in buttons
        assert "🏷 Промокоды" in buttons
        await app.db.close()

    asyncio.run(scenario())


def test_admin_manual_grant_flow() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        state = FakeState()

        await _handler(router, "callback_query", "cb_grant")(
            FakeCallback("adm:grant", FakeMessage(user_id=111), user_id=111), state
        )
        assert state.state is not None

        await _handler(router, "message", "grant_user")(FakeMessage("900", user_id=111), state)
        assert state.data["tg_id"] == 900

        message = FakeMessage("7", user_id=111)
        await _handler(router, "message", "grant_days")(message, state)
        assert "Доступ выдан" in message.last_text

        user = await app.db.get_user(900)
        assert user["status"] == "active"
        assert any("пропуск" in text for text in app.bot.texts_to(900))

        await app.db.close()

    asyncio.run(scenario())


def test_admin_promo_creation_flow() -> None:
    async def scenario() -> None:
        app = await make_app()
        router = admin_handlers.build_router(app)  # type: ignore[arg-type]
        state = FakeState()
        await state.update_data(code="SALE30")

        await _handler(router, "message", "promo_percent")(
            FakeMessage("30", user_id=111), state
        )
        await _handler(router, "message", "promo_uses")(FakeMessage("5", user_id=111), state)
        message = FakeMessage("10", user_id=111)
        await _handler(router, "message", "promo_days")(message, state)

        assert "Промокод создан" in message.last_text
        promo = await app.db.get_promo("SALE30")
        assert promo is not None
        assert promo["discount_percent"] == 30
        assert promo["max_uses"] == 5
        assert promo["expires_at"] is not None

        await app.db.close()

    asyncio.run(scenario())
