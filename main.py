import logging
import os
import json
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# --- КОНФИГУРАЦИЯ (Имена возвращены к исходным) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-1.5-flash"
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
ALYA_CHAT_ID_TG = os.getenv("ALYA_CHAT_ID_TG", "")

genai.configure(api_key=GEMINI_API_KEY)
ASTANA_TZ = timezone(timedelta(hours=5))

SYSTEM_PROMPT = (
    "Ты — ассистент психолога Алии. Принимаешь записи онлайн (1.5 часа, 25 000 тенге, 10:00-21:00 Астана). "
    "Если клиент хочет записаться, используй JSON: " + '{"action": "check_slot", "date": "YYYY-MM-DD", "time": "HH:MM"}. ' +
    "Если клиент хочет перенести, используй JSON: " + '{"action": "reschedule_slot", "date": "YYYY-MM-DD", "time": "HH:MM"}. ' +
    "Пиши эмпатично, JSON добавляй только для команд. Сегодня 18 июля 2026 года."
)

# --- ОТПРАВКА ---
async def send_message(platform, to, text):
    try:
        if platform == "telegram":
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": to, "text": text})
        elif platform == "whatsapp":
            url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
            headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}", "Content-Type": "application/json"}
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}, headers=headers)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# --- КАЛЕНДАРЬ ---
def get_service():
    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar'])
        return build('calendar', 'v3', credentials=creds)
    return None

service = get_service()

# --- НАПОМИНАНИЯ ---
async def reminder_loop():
    while True:
        if service:
            try:
                now = datetime.now(ASTANA_TZ)
                events = service.events().list(calendarId=GOOGLE_CALENDAR_ID, timeMin=now.isoformat(), singleEvents=True).execute().get('items', [])
                for e in events:
                    start = datetime.fromisoformat(e['start']['dateTime'].replace('Z', '+00:00')).astimezone(ASTANA_TZ)
                    diff = start - now
                    summary = e.get('summary', '')
                    if "Client:" in summary:
                        _, platform, chat_id = summary.split(":")
                        if timedelta(hours=23, minutes=50) < diff < timedelta(hours=24, minutes=10):
                            await send_message(platform, chat_id, "Напоминание: сессия завтра.")
                        elif timedelta(minutes=50) < diff < timedelta(hours=1, minutes=10):
                            await send_message(platform, chat_id, "Напоминание: сессия через час.")
            except Exception as e: logger.error(f"Ошибка напоминаний: {e}")
        await asyncio.sleep(600)

@app.on_event("startup")
async def startup(): asyncio.create_task(reminder_loop())

# --- WEBHOOK ---
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    platform, chat_id, text = None, None, ""
    
    if "message" in data:
        platform, chat_id = "telegram", str(data["message"]["chat"]["id"])
        text = data["message"].get("text", "")
    elif "entry" in data:
        changes = data["entry"][0]["changes"][0]["value"]
        platform, chat_id = "whatsapp", str(changes["messages"][0]["from"])
        text = changes["messages"][0]["text"]["body"]

    model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=SYSTEM_PROMPT)
    reply = model.generate_content(text).text
    
    if "{" in reply and "}" in reply:
        try:
            start, end = reply.find("{"), reply.rfind("}") + 1
            cmd = json.loads(reply[start:end])
            
            if cmd.get("action") in ["check_slot", "reschedule_slot"]:
                if cmd.get("action") == "reschedule_slot":
                    now = datetime.utcnow().isoformat() + 'Z'
                    events = service.events().list(calendarId=GOOGLE_CALENDAR_ID, timeMin=now, q=chat_id).execute().get('items', [])
                    for e in events: service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=e['id']).execute()
                
                start_dt = datetime.strptime(f"{cmd['date']} {cmd['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=ASTANA_TZ)
                service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body={
                    'summary': f'Client:{platform}:{chat_id}',
                    'start': {'dateTime': start_dt.isoformat()},
                    'end': {'dateTime': (start_dt + timedelta(minutes=90)).isoformat()}
                }).execute()
                
                await send_message(platform, chat_id, "Запись подтверждена.")
                if ALYA_CHAT_ID_TG: await send_message("telegram", ALYA_CHAT_ID_TG, f"Новая запись/перенос: {cmd['date']} {cmd['time']} (клиент {chat_id})")
                reply = f"Записала на {cmd['date']} в {cmd['time']}."
        except Exception as e: logger.error(f"Ошибка логики: {e}")
            
    if platform: await send_message(platform, chat_id, reply)
    return {"status": "success"}
