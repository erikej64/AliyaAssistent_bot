import os
import re
import json
import asyncio
import httpx
import google.generativeai as genai
from datetime import datetime, timedelta
from fastapi import FastAPI, Request

# Инициализация Gemini
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-1.5-flash')

# Константы
SESSION_BLOCK = 120  # Всегда 2 часа
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
conversations = {}

def now_astana() -> datetime:
    return datetime.utcnow() + timedelta(hours=5)

def get_system_prompt() -> str:
    now = now_astana()
    return f"""Ты — ассистент психолога Алии. 
    ТЕКУЩАЯ ДАТА: {now.strftime("%Y-%m-%d")}
    
    ПРАВИЛА ЗАПИСИ:
    1. Длительность сессии ВСЕГДА 2 часа (120 минут).
    2. При создании новой записи используй формат:
       ЗАПИСЬ: Имя: {{имя}} | Город: {{город}} | Дата: {{ГГГГ-ММ-ДД}} | Время: {{ЧЧ:ММ}} | Запрос: {{запрос}}
    
    3. ПРАВИЛА ПЕРЕНОСА (ИЗМЕНЕНИЯ) ЗАПИСИ:
       Если клиент просит перенести встречу:
       - Уточни у клиента дату старой записи.
       - В ответе обязательно добавь параметр 'изменить_с: ГГГГ-ММ-ДД'.
       Пример: ЗАПИСЬ: Имя: {{имя}} | Дата: {{новая_дата}} | Время: {{новое_время}} | изменить_с: {{старая_дата}} | Запрос: {{запрос}}
    """

async def ask_gemini(chat_id: str, user_message: str) -> str:
    if chat_id not in conversations:
        conversations[chat_id] = []
    chat = model.start_chat(history=[])
    response = await chat.send_message(user_message)
    return response.text

async def find_and_delete_event(name: str, date: str) -> bool:
    """Ищет и удаляет старую запись в календаре."""
    try:
        token = await get_google_token()
        # Предполагаем, что get_events_for_date уже есть в вашем проекте
        events = await get_events_for_date(date, GOOGLE_CALENDAR_ID) 
        for event in events:
            if name.lower() in event.get("summary", "").lower():
                async with httpx.AsyncClient() as client:
                    await client.delete(
                        f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events/{event.get('id')}",
                        headers={"Authorization": f"Bearer {token}"}
                    )
                return True
        return False
    except Exception as e:
        print(f"Ошибка удаления: {e}")
        return False

async def create_calendar_event(name: str, date: str, time_str: str, request_text: str, city: str, client_phone: str = "", client_tg: str = "") -> bool:
    try:
        start_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=SESSION_BLOCK) # Фиксированные 2 часа
        
        event = {
            "summary": f"Консультация: {name} | {city}",
            "description": f"Клиент: {name}\nЗапрос: {request_text}\nКонтакт: {client_phone or client_tg}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Almaty"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Almaty"},
        }
        token = await get_google_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events",
                headers={"Authorization": f"Bearer {token}"},
                json=event
            )
            return resp.status_code in (200, 201)
    except Exception as e:
        print(f"Ошибка создания: {e}")
        return False

async def process_reply(reply: str, source: str, contact: str, raw_chat_id: str = "") -> str:
    booking = await extract_booking(reply)
    if not booking:
        return reply

    # Логика переноса
    if "изменить_с" in booking:
        await find_and_delete_event(booking["имя"], booking["изменить_с"])

    # Проверка слота (всегда 120 мин)
    is_free, suggestions = await check_slot(booking["дата"], booking["время"])
    
    if not is_free:
        return f"Время занято. Свободные варианты: {', '.join(suggestions)}"

    # Создание новой записи
    success = await create_calendar_event(
        name=booking["имя"],
        date=booking["дата"],
        time_str=booking["время"],
        request_text=booking["запрос"],
        city=booking["город"],
        client_phone=raw_chat_id if source == "WhatsApp" else "",
        client_tg=contact if source == "Telegram" else ""
    )
    
    return "Запись успешно подтверждена." if success else "Ошибка при записи."

# Оставшаяся часть вашего кода (get_google_token, extract_booking, check_slot и т.д.) 
# должна идти здесь без изменений.
