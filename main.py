import os
import re
import json
import httpx
import uvicorn
import google.generativeai as genai
from datetime import datetime, timedelta
from fastapi import FastAPI, Request

# Инициализация
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-3-flash-preview')
app = FastAPI()

# --- ВАША ЛОГИКА ---
async def extract_booking(text: str) -> dict | None:
    match = re.search(r"ЗАПИСЬ:\s*(.*)", text, re.IGNORECASE)
    if not match: return None
    parts = match.group(1).split("|")
    res = {p.split(":")[0].strip().lower(): p.split(":")[1].strip() for p in parts if ":" in p}
    return res if "дата" in res and "время" in res else None

async def ask_gemini(chat_id: str, user_message: str) -> str:
    chat = model.start_chat(history=[])
    response = await chat.send_message(user_message)
    return response.text

# --- ВЕБХУКИ ---

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    # Извлечение текста (адаптировано под GreenAPI)
    text = data.get("messageData", {}).get("textMessageData", {}).get("textMessage", "")
    chat_id = data.get("senderData", {}).get("chatId", "")
    
    if text:
        reply = await ask_gemini(chat_id, text)
        # Здесь должна быть логика отправки ответа через GreenAPI
        print(f"WhatsApp ответ: {reply}")
    return {"status": "ok"}

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    # Извлечение текста из Telegram (стандартная структура)
    message = data.get("message", {})
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", ""))
    
    if text:
        reply = await ask_gemini(chat_id, text)
        # Здесь должна быть логика отправки ответа через Telegram API
        print(f"Telegram ответ: {reply}")
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    # Фиксируем порт 8080 для Fly.io
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
