#!/usr/bin/env bash
# ============================================================================
#  «Общество тайн» — установка на сервер (Ubuntu 22.04 / 24.04, Debian 12)
#
#  Два режима:
#    1) .env уже готов (лежит рядом с проектом):
#          bash deploy/install.sh
#       → спросит ТОЛЬКО домен (или Enter для бесплатного IP.nip.io),
#         пропишет PUBLIC_BASE_URL, настроит HTTPS и запустит бота.
#       Именно этот режим нужен, если вы уже заполнили .env на своём компьютере.
#
#    2) .env нет — установщик спросит токен, ID, ключи Prodamus и создаст .env:
#          bash deploy/install.sh --ask
#
#  Перезаписать существующий .env: --force
# ============================================================================
set -euo pipefail

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; OFF=$'\e[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s✘ %s%s\n' "$RED" "$*" "$OFF" >&2; exit 1; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ASK=0; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --ask)   ASK=1 ;;
    --force) FORCE=1; ASK=1 ;;
    *) die "Неизвестный параметр: $arg (есть только --ask и --force)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Запустите от root:  sudo bash deploy/install.sh"
[[ -f requirements.txt && -f run.py ]] || die "Скрипт должен лежать в папке проекта (deploy/install.sh)"
command -v curl >/dev/null || die "Нет curl: apt update && apt install -y curl"

if [[ -f .env && $FORCE -eq 0 ]]; then
  ASK=0
elif [[ ! -f .env ]]; then
  ASK=1
fi

ask() {           # ask "Вопрос" "переменная" "по умолчанию" "обязательно(1/0)"
  local question="$1" var="$2" default="${3:-}" required="${4:-0}" answer=""
  while true; do
    if [[ -n "$default" ]]; then
      read -r -p "$question [$default]: " answer || true
      answer="${answer:-$default}"
    else
      read -r -p "$question: " answer || true
    fi
    answer="$(printf '%s' "$answer" | tr -d '[:space:]')"
    [[ -n "$answer" ]] && break
    [[ "$required" == "1" ]] || break
    warn "Это поле обязательное."
  done
  printf -v "$var" '%s' "$answer"
}

get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

# ---------------------------------------------------------------- .env
if [[ $ASK -eq 1 ]]; then
  say ""
  say "${BOLD}Заполняем .env${OFF}"
  say "1/2. Telegram"
  ask "Токен бота от @BotFather" BOT_TOKEN "" 1
  ask "Ваш Telegram ID (для уведомлений)" ADMIN_IDS "" 1
  ask "ID закрытого канала (@username или -100…)" CHANNEL_ID "" 1
  ask "ID чата обсуждений (-100…)" CHAT_ID "" 1
  say ""
  say "2/2. Prodamus"
  ask "Адрес платёжной формы" PRODAMUS_FORM_URL "https://demo.payform.ru/" 1
  ask "Секретный ключ платёжной страницы" PRODAMUS_SECRET "" 1
  ask "ID подписного товара (пусто = разовые платежи)" PRODAMUS_SUBSCRIPTION_ID "" 0
  ask "Код интеграции SYS (можно пусто)" PRODAMUS_SYS "" 0

  cat > .env <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
CHANNEL_ID=$CHANNEL_ID
CHAT_ID=$CHAT_ID

PRICE=790
CURRENCY=rub
PRODUCT_NAME=Подписка «Общество тайн» (1 месяц)
SUBSCRIPTION_DAYS=30
GRACE_DAYS=1
REMINDER_DAYS_BEFORE=3
INVITE_LINK_TTL_HOURS=24

PRODAMUS_FORM_URL=$PRODAMUS_FORM_URL
PRODAMUS_SECRET=$PRODAMUS_SECRET
PRODAMUS_SUBSCRIPTION_ID=$PRODAMUS_SUBSCRIPTION_ID
PRODAMUS_SYS=$PRODAMUS_SYS
PRODAMUS_SIGN_LINKS=true
PRODAMUS_SEND_TG_USER_ID=true

WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8080
WEBHOOK_PATH=/prodamus/webhook

DB_PATH=data/club.db
TZ=Europe/Moscow
SUPPORT_CONTACT=
LOG_LEVEL=INFO
EOF
  ok ".env создан"
else
  ok "Использую готовый .env"
  for key in BOT_TOKEN ADMIN_IDS CHANNEL_ID CHAT_ID PRODAMUS_SECRET PRODAMUS_FORM_URL; do
    [[ -n "$(get_env "$key")" ]] || die "В .env пустое поле $key — заполните его и запустите снова"
  done
fi
chmod 600 .env
mkdir -p data

# ---------------------------------------------------------------- домен
IP="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
DEFAULT_DOMAIN="${IP:+$IP.nip.io}"
say ""
say "${BOLD}Адрес для вебхука Prodamus${OFF}"
say "Если своего домена нет — просто нажмите Enter: получится бесплатный адрес"
say "вида ${DEFAULT_DOMAIN:-IP.nip.io}, HTTPS настроится сам (сертификат Let's Encrypt)."
ask "Домен (без https://)" DOMAIN "$DEFAULT_DOMAIN" 1

# ---------------------------------------------------------------- Docker
if ! command -v docker >/dev/null 2>&1; then
  say "Ставлю Docker (2–3 минуты)…"
  curl -fsSL https://get.docker.com | sh >/dev/null
  ok "Docker установлен"
else
  ok "Docker уже есть"
fi
docker compose version >/dev/null 2>&1 || die "Нет плагина docker compose (docker-compose-plugin)."

# ------------------------------------------------------- .env + Caddyfile
if grep -q '^PUBLIC_BASE_URL=' .env; then
  sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$DOMAIN|" .env
else
  printf '\nPUBLIC_BASE_URL=https://%s\n' "$DOMAIN" >> .env
fi
ok "PUBLIC_BASE_URL = https://$DOMAIN"

cat > deploy/Caddyfile <<EOF
$DOMAIN {
	encode zstd gzip
	reverse_proxy bot:8080
}
EOF
ok "Caddyfile настроен на $DOMAIN"

# ---------------------------------------------------------------- запуск
say "Собираю и запускаю бота…"
docker compose up -d --build

say ""
say "Жду, пока поднимется HTTPS (до 60 секунд)…"
HTTPS_OK=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "https://$DOMAIN/health" >/dev/null 2>&1; then
    HTTPS_OK=1; ok "HTTPS работает"; break
  fi
  sleep 2
done
[[ $HTTPS_OK -eq 1 ]] || warn "HTTPS пока не ответил — подождите минуту и проверьте: docker compose logs caddy"

WEBHOOK_URL="https://$DOMAIN/prodamus/webhook"
say ""
say "${BOLD}${GREEN}Готово!${OFF}"
say ""
say "Адрес для Prodamus (вписать в ДВА поля уведомлений):"
say "   ${BOLD}$WEBHOOK_URL${OFF}"
say ""
say "Проверка настроек внутри контейнера:"
say "   docker compose exec bot python scripts/check_setup.py"
say "Логи:"
say "   docker compose logs -f bot"
say ""
say "Дальше в Telegram: написать боту /start и /admin."
say ""
if [[ -z "$(get_env PRODAMUS_SUBSCRIPTION_ID)" ]]; then
  warn "ID подписного товара пустой: пока работает разовая оплата 790 ₽/мес."
fi
