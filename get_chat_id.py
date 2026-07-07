#!/usr/bin/env python3
"""Одноразовый скрипт: узнать свой chat_id.

1. Напиши своему боту /start в Telegram.
2. Запусти: TELEGRAM_BOT_TOKEN=xxx python get_chat_id.py
"""
import os

import httpx

token = os.environ["TELEGRAM_BOT_TOKEN"]
r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()

if not r.get("ok"):
    print("Ошибка:", r)
elif not r["result"]:
    print("Обновлений нет. Сначала напиши боту /start и запусти снова.")
else:
    for upd in r["result"]:
        msg = upd.get("message") or upd.get("my_chat_member") or {}
        chat = msg.get("chat", {})
        if chat:
            print(f"chat_id: {chat['id']}  ({chat.get('first_name', '')} {chat.get('username', '')})")
