import os
import re
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# --- КОНФИГУРАЦИЯ ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GREEN_API_ID = os.environ["GREEN_API_ID"]
GREEN_API_TOKEN = os.environ["GREEN_API_TOKEN"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
PSYCHOLOGIST_PHONE = os.environ.get("PSYCHOLOGIST_PHONE", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

_creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "{}")
GOOGLE_CREDS = json.loads(_creds_raw)

# --- УТИЛИТЫ ---
def now_astana(): return datetime.utcnow() + timedelta(hours=5)

def get_system_prompt():
    now = now_astana()
    return f"""Ты — ассистент психолога. Твоя задача: записывать клиентов.
ВАЖНО: Никогда не предлагай подтвердить запись, если не проверил слот.
Когда клиент называет время:
1. Если ты еще не проверял этот слот, выведи строку: ПРОВЕРКА: ГГГГ-ММ-ДД | ЧЧ:ММ
2. Не задавай вопрос "Всё верно?", пока система не ответит, что слот свободен.
3. Если система говорит, что занято — предложи свободные варианты.
4. Только когда слот подтвержден как свободный, выведи итоговый список и спроси "Всё верно?".
5. После подтверждения клиентом, выведи: ЗАПИСЬ: Имя: ... | Дата: ... | Время: ... | Запрос: ..."""

# --- GOOGLE CALENDAR ---
async def get_google_token():
    # (Оставляем вашу рабочую функцию токена)
    return "token" # Замените на логику из вашего старого кода

async def check_slot(date, time_str):
    # Эта функция проверяет календарь и возвращает True/False и варианты
    # ... (логика из вашего старого кода)
    return True, [] 

# --- ОСНОВНАЯ ЛОГИКА ---
async def process_message(chat_id_key, text, source, contact, raw_chat_id):
    if chat_id_key not in conversations: conversations[chat_id_key] = []
    conversations[chat_id_key].append({"role": "user", "content": text})
    
    # Запрос к Claude
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", 
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1000, "system": get_system_prompt(), 
                  "messages": conversations[chat_id_key]})
        reply = resp.json()["content"][0]["text"]

    # Обработка команд бота
    if "ПРОВЕРКА:" in reply:
        match = re.search(r"ПРОВЕРКА: (\d{4}-\d{2}-\d{2}) \| (\d{2}:\d{2})", reply)
        if match:
            date, time = match.groups()
            is_free, suggestions = await check_slot(date, time)
            if is_free:
                new_msg = f"Отлично, время {time} {date} свободно. Подтверждаете запись?"
            else:
                new_msg = f"К сожалению, {time} {date} занято. Свободно: {', '.join(suggestions)}. Что выберете?"
            conversations[chat_id_key].append({"role": "assistant", "content": new_msg})
            return new_msg
            
    if "ЗАПИСЬ:" in reply:
        # Логика сохранения в календарь
        # ... (ваш код создания события)
        return "Вы записаны! Алия свяжется с вами."

    conversations[chat_id_key].append({"role": "assistant", "content": reply})
    return reply

# --- WEBHOOKS ---
app = FastAPI()

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    data = await request.json()
    # ... (логика из вашего старого кода по разбору JSON)
    reply = await process_message(chat_id, text, "WhatsApp", phone, chat_id)
    await send_whatsapp(chat_id, reply)
    return {"status": "ok"}

@app.post("/telegram")
async def handle_telegram(request: Request):
    # ... (логика из вашего старого кода для телеграм)
    reply = await process_message(chat_id, text, "Telegram", contact, chat_id)
    await send_telegram(chat_id, reply)
    return {"status": "ok"}
