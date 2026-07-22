import logging
import os
import json
import httpx
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()
@app.get("/")
async def root():
    return {"status": "online", "message": "AliyaAssistent bot is running"}
# Конфигурация (имя переменной TELEGRAM_TOKEN теперь совпадает с секретами Fly.io)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3-flash-preview"  
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "")
ALYA_CHAT_ID_TG = os.getenv("ALYA_CHAT_ID_TG", "")

genai.configure(api_key=GEMINI_API_KEY)
ASTANA_TZ = timezone(timedelta(hours=5))

SYSTEM_PROMPT = (
    "Ты — ассистент психолога Алии. Принимаешь записи онлайн (1.5 часа, 25 000 тенге, 10:00-21:00 Астана). "
    "Если клиент хочет записаться, используй JSON: " + '{"action": "check_slot", "date": "YYYY-MM-DD", "time": "HH:MM"}. ' +
    "Если клиент хочет перенести, используй JSON: " + '{"action": "reschedule_slot", "date": "YYYY-MM-DD", "time": "HH:MM"}. ' +
    "Пиши эмпатично, JSON добавляй только для команд. Сегодня 18 июля 2026 года."
)

async def send_message(platform, to, text):
    try:
        if platform == "telegram":
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": to, "text": text})
        elif platform == "whatsapp":
            url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
            headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}", "Content-Type": "application/json"}
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}, headers=headers)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

def get_service():
    # Автоматически создаем credentials.json из секрета Fly.io, если файл еще не создан
    if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE) and GOOGLE_CREDENTIALS:
        try:
            with open(GOOGLE_SERVICE_ACCOUNT_FILE, "w", encoding="utf-8") as f:
                f.write(GOOGLE_CREDENTIALS)
        except Exception as e:
            logger.error(f"Не удалось записать credentials.json: {e}")

    if os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        try:
            creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/calendar'])
            return build('calendar', 'v3', credentials=creds)
        except Exception as e:
            logger.error(f"Ошибка инициализации Google Calendar API: {e}")
            return None
    return None

service = get_service()

async def process_message(platform, chat_id, text):
    if not text: return
    try:
        model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=SYSTEM_PROMPT)
        reply = model.generate_content(text).text
        
        if "{" in reply and "}" in reply:
            start, end = reply.find("{"), reply.rfind("}") + 1
            cmd = json.loads(reply[start:end])
            if cmd.get("action") in ["check_slot", "reschedule_slot"]:
                if service:
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
                    if ALYA_CHAT_ID_TG: await send_message("telegram", ALYA_CHAT_ID_TG, f"Запись: {cmd['date']} {cmd['time']} (клиент {chat_id})")
                    reply = f"Записала на {cmd['date']} в {cmd['time']}."
        
        await send_message(platform, chat_id, reply)
    except Exception as e:
        logger.error(f"Ошибка в процессе обработки сообщения: {e}")

@app.post("/webhook")
@app.post("/telegram")
async def webhook(request: Request):
    try:
        data = await request.json()
        if "message" in data:
            await process_message("telegram", str(data["message"]["chat"]["id"]), data["message"].get("text", ""))
        elif "entry" in data:
            changes = data["entry"][0]["changes"][0]["value"]
            if "messages" in changes:
                await process_message("whatsapp", str(changes["messages"][0]["from"]), changes["messages"][0]["text"]["body"])
    except Exception as e:
        logger.error(f"Ошибка в обработчике вебхука: {e}")
    return {"status": "ok"}
