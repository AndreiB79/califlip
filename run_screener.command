#!/bin/bash
# CaliFlip Screener — двойной клик в Finder запускает приложение.
# Откроет браузер на http://localhost:8501 автоматически.

cd "$(dirname "$0")"

# Открываем браузер на streamlit URL через 3 секунды (когда сервер поднимется)
( sleep 3 && open "http://localhost:8501" ) &

# Запуск streamlit (foreground — окно Terminal остаётся открытым)
exec python3 -m streamlit run califlip_app.py \
    --browser.gatherUsageStats=false \
    --server.headless=true
