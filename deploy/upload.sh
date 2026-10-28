#!/usr/bin/env bash
# Загрузка бота с Mac на сервер. Запускать в Терминале из папки бота:
#   bash deploy/upload.sh IP-СЕРВЕРА --first   — самый первый раз: код + токен (.env) + задачи (tasks.db)
#   bash deploy/upload.sh IP-СЕРВЕРА           — обновления: только код (задачи и токен на сервере не трогает)
set -e
if [ -z "$1" ]; then
    echo "Укажите IP-адрес сервера, например: bash deploy/upload.sh 185.10.20.30"
    exit 1
fi
SERVER="root@$1"
EXCLUDE=(--exclude .git --exclude '.venv*' --exclude __pycache__ --exclude .vscode --exclude .idea)
if [ "$2" != "--first" ]; then
    EXCLUDE+=(--exclude .env --exclude tasks.db)
fi

echo "→ Копирую файлы на сервер $1…"
rsync -av "${EXCLUDE[@]}" ./ "$SERVER:/opt/tgbot/"

if [ "$2" = "--first" ]; then
    ssh "$SERVER" "bash /opt/tgbot/deploy/install.sh"
else
    ssh "$SERVER" "cd /opt/tgbot && .venv/bin/pip install -q -r requirements.txt && systemctl restart tgbot"
    echo "✓ Бот на сервере обновлён и перезапущен."
fi
