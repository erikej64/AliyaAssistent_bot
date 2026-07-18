import asyncio
import logging
import os
import json
import httpx
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, Response
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Aliya Psychology Bot Backend")

# --- КОНФИГУРАЦИЯ И ОКРУЖЕНИЕ ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "your-gemini-api-key-here")
MODEL_NAME = "gemini-3-flash-preview"

# Ключи и токены мессенджеров для реальной отправки ответов
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_secret_token_123")

genai.configure(api_key=GEMINI_API_KEY)

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")

# Параметры психологического центра Алии (Астана, UTC+5)
ASTANA_TZ = timezone(timedelta(hours=5))
WORK_START = 600   # 10:00 в минутах
WORK_END = 1260    # 21:00 в минутах
SESSION_BLOCK = 90 # 1.5 часа (90 минут)

# --- IN-MEMORY ХРАНИЛИЩЕ СОСТОЯНИЯ ---
conversations: dict[str, list] = {}
reminders: dict[str, dict] = {}
sent_reminders: set = set()

def get_calendar_service():
    if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        logger.warning(f"Файл {GOOGLE_SERVICE_ACCOUNT_FILE} не найден. Работа в режиме имитации.")
        return None
    try:
        scopes = ['https://www.googleapis.com/auth/calendar']
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Ошибка авторизации Google Календаря: {e}")
        return None

calendar_service = get_calendar_service()

# --- ФУНКЦИИ ОТПРАВКИ В МЕССЕНДЖЕРЫ ---

async def send_telegram_message(chat_id: str, text: str):
    """Отправка сообщения пользователю в Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не настроен. Отправка отменена.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload)
            if res.status_code != 200:
                logger.error(f"Ошибка отправки в TG: {res.text}")
        except Exception as e:
            logger.error(f"Ошибка сети при отправке в TG: {e}")

async def send_whatsapp_message(to_number: str, text: str):
    """Отправка сообщения пользователю в WhatsApp Cloud API."""
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("Настройки WhatsApp Cloud API отсутствуют. Отправка отменена.")
        return
    url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, headers=headers)
            if res.status_code not in [200, 201]:
                logger.error(f"Ошибка отправки в WA: {res.text}")
        except Exception as e:
            logger.error(f"Ошибка сети при отправке в WA: {e}")

async def send_reply(source: str, chat_id: str, text: str):
    """Универсальный роутер ответов."""
    if source == "telegram":
        await send_telegram_message(chat_id, text)
    elif source == "whatsapp":
        await send_whatsapp_message(chat_id, text)
    else:
        logger.info(f"Ответ для кастомного источника [{chat_id}]: {text}")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ЛОГИКА ---

def parse_iso_time(iso_str: str) -> datetime:
    try:
        if iso_str.endswith('Z'):
            iso_str = iso_str[:-1] + '+00:00'
        return datetime.fromisoformat(iso_str)
    except Exception as e:
        logger.error(f"Ошибка парсинга ISO времени '{iso_str}': {e}")
        return datetime.now(timezone.utc)

def notify_aliya(message: str):
    """Уведомление Алии об изменениях или переносах записей."""
    logger.info(f"[УВЕДОМЛЕНИЕ ДЛЯ АЛИИ]: {message}")

def find_and_delete_event(client_info: str) -> bool:
    """Поиск и удаление старого будущего события для реализации чистых переносов."""
    if not calendar_service:
        logger.info(f"Имитация удаления старой записи для {client_info}")
        return True
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        events_result = calendar_service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=now_iso,
            q=client_info,
            singleEvents=True
        ).execute()
        
        events = events_result.get('items', [])
        if not events:
            return False
            
        for event in events:
            calendar_service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event['id']).execute()
            logger.info(f"Удалено старое событие {event['id']} для {client_info}")
        return True
    except Exception as e:
        logger.error(f"Ошибка при удалении события: {e}")
        return False

def get_busy_slots(date_str: str) -> list[tuple[int, int]]:
    if not calendar_service:
        return []
    try:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=ASTANA_TZ)
        end_dt = start_dt + timedelta(days=1)

        events_result = calendar_service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        busy_slots = []
        for event in events:
            start_raw = event['start'].get('dateTime', event['start'].get('date'))
            end_raw = event['end'].get('dateTime', event['end'].get('date'))
            
            if len(start_raw) == 10:
                busy_slots.append((WORK_START, WORK_END))
                continue

            start_dt_event = parse_iso_time(start_raw).astimezone(ASTANA_TZ)
            end_dt_event = parse_iso_time(end_raw).astimezone(ASTANA_TZ)
            s_mins = max(WORK_START, min(start_dt_event.hour * 60 + start_dt_event.minute, WORK_END))
            e_mins = max(WORK_START, min(end_dt_event.hour * 60 + end_dt_event.minute, WORK_END))
            if s_mins < e_mins:
                busy_slots.append((s_mins, e_mins))
        return busy_slots
    except Exception as e:
        logger.error(f"Ошибка при получении занятых слотов: {e}")
        return []

def check_slot(date_str: str, time_str: str) -> dict:
    try:
        requested_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        req_start = requested_dt.hour * 60 + requested_dt.minute
        req_end = req_start + SESSION_BLOCK
    except ValueError:
        return {"status": "error", "message": "Неверный формат даты/времени. Нужно YYYY-MM-DD и HH:MM."}

    if req_start < WORK_START or req_end > WORK_END:
        return {
            "status": "busy",
            "message": "Время вне рабочих часов Алии. Мы принимаем с 10:00 до 21:00.",
            "alternatives": generate_alternatives(date_str)
        }

    busy_slots = get_busy_slots(date_str)
    for b_start, b_end in busy_slots:
        if not (req_end <= b_start or req_start >= b_end):
            return {
                "status": "busy",
                "message": "Выбранное время уже занято в календаре.",
                "alternatives": generate_alternatives(date_str)
            }
    return {"status": "free", "message": "Время свободно."}

def generate_alternatives(date_str: str) -> list[str]:
    busy_slots = get_busy_slots(date_str)
    alternatives = []
    for mins in range(WORK_START, WORK_END - SESSION_BLOCK + 1, 30):
        is_free = True
        for b_start, b_end in busy_slots:
            if not (mins + SESSION_BLOCK <= b_start or mins >= b_end):
                is_free = False
                break
        if is_free:
            alternatives.append(f"{mins // 60:02d}:{mins % 60:02d}")
            if len(alternatives) >= 3:
                break
    return alternatives

def book_google_event(date_str: str, time_str: str, client_info: str) -> bool:
    if not calendar_service:
        logger.info(f"Имитация записи для {client_info} на {date_str} {time_str}")
        return True
    try:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=ASTANA_TZ)
        end_dt = start_dt + timedelta(minutes=SESSION_BLOCK)
        event = {
            'summary': f'Запись: {client_info}',
            'description': 'Онлайн-консультация психолога Алии (Gemini 3)',
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Asia/Almaty'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Asia/Almaty'},
        }
        calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return True
    except Exception as e:
        logger.error(f"Ошибка при создании события: {e}")
        return False

# --- ФОНОВЫЕ ЗАДАЧИ НАПОМИНАНИЙ ---

async def remind_after(source: str, chat_id: str, delay: int, text: str):
    await asyncio.sleep(delay)
    await send_reply(source, chat_id, text)

async def calendar_checker_loop():
    while True:
        try:
            now = datetime.now(timezone.utc).astimezone(ASTANA_TZ)
            for chat_id, data in list(reminders.items()):
                event_time = data.get("time")
                source = data.get("source", "custom")
                if not event_time: continue
                diff = event_time - now
                
                # Напоминание за 24 часа
                if timedelta(hours=23, minutes=30) <= diff <= timedelta(hours=24, minutes=30):
                    rem_id = f"{chat_id}_24h_{event_time.strftime('%Y%m%d%H%M')}"
                    if rem_id not in sent_reminders:
                        await send_reply(source, chat_id, "Напоминание: у Вас запланирована сессия с Алией завтра.")
                        sent_reminders.add(rem_id)
                        
                # Напоминание за 1 час
                elif timedelta(minutes=45) <= diff <= timedelta(hours=1, minutes=15):
                    rem_id = f"{chat_id}_1h_{event_time.strftime('%Y%m%d%H%M')}"
                    if rem_id not in sent_reminders:
                        await send_reply(source, chat_id, "Напоминание: сессия с Алией начинается через 1 час. Подготовьтесь к созвону.")
                        sent_reminders.add(rem_id)
        except Exception as e:
            logger.error(f"Ошибка в фоновом цикле: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(calendar_checker_loop())

# --- ПОЛНЫЙ ДЕТАЛИЗИРОВАННЫЙ СИСТЕМНЫЙ ПРОМПТ ---

SYSTEM_PROMPT = """Вы — вежливый, эмпатичный и теплый ассистент по автоматической записи клиентов на онлайн-консультации к профессиональному психологу Алие. Ваша главная задача — помочь клиенту выбрать удобное время, зафиксировать запись в календаре или корректно изменить существующую бронь.

ИНФОРМАЦИЯ О ПРИЕМЕ И УСЛОВИЯХ:
- Психолог: Алия.
- Формат проведения: Исключительно онлайн по видеосвязи (ссылка на комнату созвона генерируется автоматически и отправляется клиенту незадолго до начала сессии).
- Длительность одной сессии: Строго 1,5 часа (90 минут). Обратите внимание, что шаг сетки в календаре составляет 30 минут, но сама консультация занимает полноценный полуторачасовой слот.
- Стоимость: 25 000 тенге за одну сессию. Напоминайте цену вежливо при первичном обсуждении условий.
- График работы: Прием ведется с 10:00 до 21:00 по времени Астаны (UTC+5). Записи вне этого диапазона невозможны.

ПРАВИЛА И ЛОГИКА ВЕДЕНИЯ ДИАЛОГА:
1. Обработка приветствий и знакомство: 
   Если пользователь просто здоровается (например: "Привет", "Здравствуйте", "Добрый день"), вы ОБЯЗАНЫ развернуто и тепло ответить взаимностью, представиться как виртуальный помощник Алии, кратко, но понятно озвучить ключевые условия (онлайн-формат, длительность сессии 1.5 часа, стоимость 25 000 тенге) и мягко поинтересоваться, на какую дату и время клиент хотел бы запланировать свой визит. Категорически запрещено сразу выдавать сухой JSON без текста или игнорировать фазу приветствия!
   
2. Поддержание контекста и эмпатия:
   Общайтесь уважительно, используйте поддерживающий тон. Если клиент задает вопросы о квалификации, сомневается или просто делится проблемой, ответьте ему поддерживающим текстом, напомните, что Алия обязательно поможет на сессии, и плавно верните к выбору свободного времени.

3. Логика назначения новой записи (Бронирование):
   Как только клиент четко выражает намерение записаться и называет конкретную дату и время (или соглашается на предложенный альтернативный слот), вы обязаны сгенерировать управляющую команду в формате JSON внутри вашего ответа:
   ```json
   {
     "action": "check_slot",
     "date": "YYYY-MM-DD",
     "time": "HH:MM"
   }
