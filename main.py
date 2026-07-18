import os
import re
import json
import asyncio
import httpx
import uvicorn
import google.generativeai as genai
from datetime import datetime, timedelta
from fastapi import FastAPI, Request

# Инициализация
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')
app = FastAPI()

SESSION_BLOCK = 120
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

# --- ВАШИ ФУНКЦИИ ---
async def get_google_token():
    return os.environ.get("GOOGLE_TOKEN", "")

async def get_events_for_date(date, calendar_id):
    return []

async def check_slot(date, time):
    return True, []

async def create_calendar_event(name, date, time_str, request_text, city, client_phone, client_tg):
    return True

async def extract_booking(text):
    match = re.search(r"ЗАПИСЬ:\s*(.*)", text, re.IGNORECASE)
    if not match: return None
    parts = match.group(1).split("|")
    res = {p.split(":")[0].strip().lower(): p.split(":")[1].strip() for p in parts if ":" in p}
    return res if "дата" in res and "время" in res else None

async def find_and_delete_event(name, date):
    return True

# --- ОСНОВНАЯ ЛОГИКА ---
@app.post("/whatsapp")
async def handle_whatsapp(request: Request):
    data = await request.json()
    # Логика обработки вебхука
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    # Явный запуск на порту 8080
    uvicorn.run(app, host="0.0.0.0", port=8080)
