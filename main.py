import os
import httpx
from fastapi import FastAPI, Request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GREEN_API_ID = os.environ.get("GREEN_API_ID", "")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN", "")

app = FastAPI()

# Хранилище истории (простое, для всего диалога)
chat_histories = {}

SYSTEM_PROMPT = """Ты — администратор центра. 
Услуги: Онлайн-терапия (ACT, КПТ, DBT) — 25 000 тг/сессия. 
Правила:
1. Если клиент хочет записаться — собери имя, город, время.
2. Если данных не хватает — вежливо спроси недостающее.
3. Если все есть — подтверди запись.
4. Помни всю историю диалога."""

async def ask_gemini(text, chat_id):
    # Инициализация истории, если её нет
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [{"role": "user", "parts": [{"text": SYSTEM_PROMPT}]}]
    
    # Добавляем новое сообщение пользователя
    chat_histories[chat_id].append({"role": "user", "parts": [{"text": text}]})
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"contents": chat_histories[chat_id]}, timeout=20.0)
            data = resp.json()
            answer = data["candidates"][0]["content"]["parts"][0]["text"]
            
            # Добавляем ответ бота в историю
            chat_histories[chat_id].append({"role": "model", "parts": [{"text": answer}]})
            return answer
    except:
        return "Извините, сейчас заминка. Повторите запрос."

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    chat_id = str(data["message"]["chat"]["id"])
    text = data["message"].get("text", "")
    reply = await ask_gemini(text, chat_id)
    httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": reply})
    return {"ok": True}
