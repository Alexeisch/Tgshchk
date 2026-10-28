#!/usr/bin/env bash
# Установка бота на сервер (Ubuntu / Debian). Запускается на сервере:
#   bash /opt/tgbot/deploy/install.sh
# Повторный запуск безопасен: обновит библиотеки и перезапустит бота.
set -e
cd "$(dirname "$0")/.."
BOT_DIR="$(pwd)"

echo "→ Устанавливаю Python…"
apt-get update -q
apt-get install -y -q python3 python3-venv

# Если памяти мало и нет файла подкачки — создаём его (1 ГБ), чтобы бот не падал при рисовании отчётов
if [ "$(swapon --show | wc -l)" -eq 0 ] && [ ! -f /swapfile ]; then
    echo "→ Создаю файл подкачки 1 ГБ…"
    if fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile; then
        echo "/swapfile none swap sw 0 0" >> /etc/fstab
    else
        rm -f /swapfile
        echo "  Файл подкачки создать не удалось — это не страшно, если у сервера 1 ГБ памяти или больше."
    fi
fi

echo "→ Устанавливаю библиотеки бота…"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "→ Настраиваю автозапуск…"
sed "s#/opt/tgbot#${BOT_DIR}#g" deploy/tgbot.service > /etc/systemd/system/tgbot.service
systemctl daemon-reload
systemctl enable tgbot
systemctl restart tgbot

sleep 3
systemctl --no-pager --lines=5 status tgbot || true
echo
echo "✓ Готово. Бот работает на сервере и сам запустится после перезагрузки."
echo "  Журнал бота: journalctl -u tgbot -f   (выход — Control+C)"
