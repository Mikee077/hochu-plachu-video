#!/usr/bin/env bash
# Установка сервиса монтажа на чистый VPS (Ubuntu 22.04/24.04).
# Запуск:  sudo bash install.sh render.ваш-домен.com
# Перед запуском: A-запись домена должна указывать на IP сервера.
set -euo pipefail
DOMAIN="${1:?Укажите домен: sudo bash install.sh render.example.com}"
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  KEY=$(openssl rand -hex 24)
  printf 'DOMAIN=%s\nRENDER_KEY=%s\nKEEP_DAYS=14\nDEFAULT_DUR=30\n' "$DOMAIN" "$KEY" > .env
  chmod 600 .env
  echo "Ключ сервиса сохранён в $(pwd)/.env (строка RENDER_KEY)."
  echo "Скопируйте его в n8n: Credentials → Header Auth «Видео — сервис монтажа», имя X-API-Key."
fi

mkdir -p data/files assets
[ -f assets/banner.png ] || echo "Внимание: нет assets/banner.png — запустите make_banner.py или положите свой PNG."

docker compose up -d --build
sleep 5
curl -fsS "https://$DOMAIN/health" && echo && echo "Готово: https://$DOMAIN/health"
