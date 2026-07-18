import os
import httpx
from fastapi import FastAPI, Request
from collections import defaultdict

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GREEN_API_ID = os.environ.get("GREEN_API_ID", "")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN", "")

app = FastAPI()

# Память для хранения данных пользователя
user_memory = defaultdict(lambda: {"info": "", "time": ""})

SYSTEM_INSTRUCTION = """Ты — профессиональный администратор центра психологии.
Твой стиль: вежливый, внимательный, но четкий.

ПРАВИЛА ОТВЕТОВ:
1. Если спрашивают про УСЛУГИ: опиши их развернуто:
   - Индивидуальные консультации (исследование себя, работа с тревогой, стрессом, самооценкой).
   - Семейная и парная терапия (налаживание взаимопонимания, разрешение конфликтов).
   - Краткосрочная поддержка (принятие решений, сложные ситуации).
   Стоимость: 25 000 тг/сессия.

2. Если клиент хочет ЗАПИСАТЬСЯ: собирай данные (имя, город, время). Если чего-то не хватает — вежливо запрашивай.
3. Если все данные есть (имя, город, время) — подтверди запись и скажи, что свяжешься для уточнения.
4. Помни, что ты уже знаешь о клиенте из памяти."""

async def ask_gemini(text, chat_id):
    mem = user_memory[chat_id]
    prompt = f"Память о клиенте: {mem}. Сообщение клиента: {text}. {SYSTEM_INSTRUCTION}"
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=15.0)
            data = resp.json()
            answer = data["candidates"][0]["content"]["parts"][0]["text"]
            
            # Умное обновление памяти
            if any(word in text.lower() for word in ["эрик", "астана"]):
                user_memory[chat_id]["info"] = "Эрик, Астана"
            if any(char.isdigit() for char in text):
                user_memory[chat_id]["time"] = text
                
            return answer
    except:
        return "Извините, сейчас небольшая техническая заминка. Повторите, пожалуйста."

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    msg = data.get("message", {})
    text = msg.get("text", "")
    chat_id = str(msg.get("chat", {}).get("id"))
    if text and chat_id:
        reply = await ask_gemini(text, chat_id)
        httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": reply})
    return {"ok": True}

@app.post("/webhook")
async def wa_webhook(request: Request):
    data = await request.json()
    chat_id = str(data.get("senderData", {}).get("chatId", ""))
    text = data.get("messageData", {}).get("textMessageData", {}).get("textMessage", "")
    if text and chat_id:
        reply = await ask_gemini(text, chat_id)
        httpx.post(f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}", 
                     json={"chatId": chat_id, "message": reply})
    return {"ok": True}
