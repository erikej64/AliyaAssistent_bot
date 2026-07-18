import os
import httpx
from fastapi import FastAPI, Request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GREEN_API_ID = os.environ.get("GREEN_API_ID", "")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN", "")

app = FastAPI()

# --- СИСТЕМНЫЙ ПРОМПТ (ЖЕСТКОЕ ОГРАНИЧЕНИЕ) ---
SYSTEM_INSTRUCTION = """Ты — администратор. Твои правила:
1. Отвечай максимально кратко (1-2 предложения).
2. Если спросили услуги — напиши список: Онлайн-терапия (ACT, КПТ, DBT). Стоимость: 25 000 тг/сессия.
3. Если спросили запись — запроси имя и город.
4. Никаких лекций о стрессе, никакой психологии, только администрирование."""

async def ask_gemini(text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": f"{SYSTEM_INSTRUCTION} Клиент написал: {text}"}]}]
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return "Извините, техническая заминка. Повторите запрос."

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    msg = data.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    if text and chat_id:
        reply = await ask_gemini(text)
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": reply})
    return {"ok": True}

@app.post("/webhook")
async def wa_webhook(request: Request):
    data = await request.json()
    chat_id = data.get("senderData", {}).get("chatId", "")
    text = data.get("messageData", {}).get("textMessageData", {}).get("textMessage", "")
    if text and chat_id:
        reply = await ask_gemini(text)
        httpx.post(f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}", 
                     json={"chatId": chat_id, "message": reply})
    return {"ok": True}
