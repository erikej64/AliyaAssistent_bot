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
    # Этот запрос просто выведет список всех доступных вам моделей в ответ
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=15.0)
            return f"ДОСТУПНЫЕ МОДЕЛИ: {resp.text[:500]}" # Покажет начало списка
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
