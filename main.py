import os
import httpx
from fastapi import FastAPI, Request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GREEN_API_ID = os.environ.get("GREEN_API_ID", "")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN", "")

app = FastAPI()

# Хранилище сессий
chat_history = {}

SYSTEM_INSTRUCTION = """Ты — администратор. Услуги: Индивидуальная (25к), Парная (35к), Краткосрочная (20к).
Если клиент хочет записаться или изменить запись: собери Имя, Город, Время. 
Если данные есть — подтверди. Помни контекст диалога."""

async def ask_gemini(text, chat_id):
    if chat_id not in chat_history:
        chat_history[chat_id] = [{"role": "user", "parts": [{"text": SYSTEM_INSTRUCTION}]}]
    
    chat_history[chat_id].append({"role": "user", "parts": [{"text": text}]})
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"contents": chat_history[chat_id]}, timeout=20.0)
            data = resp.json()
            answer = data["candidates"][0]["content"]["parts"][0]["text"]
            
            chat_history[chat_id].append({"role": "model", "parts": [{"text": answer}]})
            return answer
    except:
        return "Ошибка соединения. Попробуйте еще раз."

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    chat_id = str(data["message"]["chat"]["id"])
    text = data["message"].get("text", "")
    reply = await ask_gemini(text, chat_id)
    httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": reply})
    return {"ok": True}

@app.post("/webhook")
async def wa_webhook(request: Request):
    data = await request.json()
    chat_id = str(data.get("senderData", {}).get("chatId", ""))
    text = data.get("messageData", {}).get("textMessageData", {}).get("textMessage", "")
    if text:
        reply = await ask_gemini(text, chat_id)
        httpx.post(f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}", 
                     json={"chatId": chat_id, "message": reply})
    return {"ok": True}
