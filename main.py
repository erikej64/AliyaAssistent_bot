import os
import httpx
from fastapi import FastAPI, Request

# --- КОНФИГУРАЦИЯ ---
# Используем имя переменной, которое мы создадим в секретах
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GREEN_API_ID = os.environ.get("GREEN_API_ID", "")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN", "")

app = FastAPI()

# --- ЛОГИКА GEMINI ---
async def ask_gemini(text):
    # Прямой запрос к модели без лишних проверок
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": f"Ты — ассистент психолога. Отвечай мягко и профессионально: {text}"}]}]
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Техническая ошибка: {e}"

# --- WEBHOOKS ---
@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    msg = data.get("message", {})
    text = msg.get("text")
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
