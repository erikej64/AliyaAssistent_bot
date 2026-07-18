import os
import re
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64
import time as _time

# --- КОНФИГУРАЦИЯ ---
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GREEN_API_ID = os.environ["GREEN_API_ID"]
GREEN_API_TOKEN = os.environ["GREEN_API_TOKEN"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_CREDS = json.loads(os.environ.get("GOOGLE_CREDENTIALS", "{}"))

conversations = {}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def now_astana(): return datetime.utcnow() + timedelta(hours=5)

async def get_google_token():
    service_account = GOOGLE_CREDS.get("client_email")
    private_key = GOOGLE_CREDS.get("private_key")
    if not service_account or not private_key: return ""
    
    now = int(_time.time())
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    claim = base64.urlsafe_b64encode(json.dumps({
        "iss": service_account, "scope": "https://www.googleapis.com/auth/calendar",
        "aud": "https://oauth2.googleapis.com/token", "exp": now + 3600, "iat": now,
    }).encode()).rstrip(b"=").decode()
    
    sig = base64.urlsafe_b64encode(serialization.load_pem_private_key(private_key.encode(), None).sign(
        f"{header}.{claim}".encode(), padding.PKCS1v15(), hashes.SHA256())).rstrip(b"=").decode()
    
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{header}.{claim}.{sig}"})
        return resp.json().get("access_token", "")

async def check_slot(date, time_str):
    token = await get_google_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events",
            headers={"Authorization": f"Bearer {token}"}, params={"timeMin": f"{date}T00:00:00Z", "timeMax": f"{date}T23:59:59Z"})
        events = resp.json().get("items", [])
    
    # Простейшая проверка пересечений
    for e in events:
        if time_str in e.get("start", {}).get("dateTime", ""): return False, ["12:00", "14:00"]
    return True, []

# --- ЛОГИКА ОБЩЕНИЯ ---
def get_system_prompt():
    return """Ты — ассистент психолога. 
Если клиент предлагает время, выведи строго: ПРОВЕРКА: ГГГГ-ММ-ДД | ЧЧ:ММ
Не предлагай подтверждение, пока не получишь статус от системы.
После того как система подтвердила слот, выведи: ЗАПИСЬ: Имя | Дата | Время | Запрос"""

async def process_message(chat_id_key, text, source, contact):
    if chat_id_key not in conversations: conversations[chat_id_key] = []
    conversations[chat_id_key].append({"role": "user", "content": text})
    
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", 
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1000, "system": get_system_prompt(), 
                  "messages": conversations[chat_id_key][-10:]})
        reply = resp.json()["content"][0]["text"]

    if "ПРОВЕРКА:" in reply:
        match = re.search(r"(\d{4}-\d{2}-\d{2}) \| (\d{2}:\d{2})", reply)
        if match:
            is_free, sugg = await check_slot(match.group(1), match.group(2))
            if is_free: reply = "Слот свободен. Всё верно?"
            else: reply = f"Занято. Свободно: {', '.join(sugg)}. Выбирайте."

    conversations[chat_id_key].append({"role": "assistant", "content": reply})
    return reply

# --- WEBHOOKS ---
app = FastAPI()

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    msg = data.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    if not text or not chat_id: return {"ok": True}
    
    reply = await process_message(f"tg:{chat_id}", text, "Telegram", "User")
    await httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": reply})
    return {"ok": True}

@app.post("/webhook")
async def wa_webhook(request: Request):
    data = await request.json()
    chat_id = data.get("senderData", {}).get("chatId", "")
    text = data.get("messageData", {}).get("textMessageData", {}).get("textMessage", "")
    if not text or not chat_id: return {"ok": True}
    
    reply = await process_message(f"wa:{chat_id}", text, "WhatsApp", chat_id)
    await httpx.post(f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}", 
                     json={"chatId": chat_id, "message": reply})
    return {"ok": True}
