import os
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
 
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GREEN_API_ID = os.environ["GREEN_API_ID"]
GREEN_API_TOKEN = os.environ["GREEN_API_TOKEN"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
PSYCHOLOGIST_PHONE = os.environ.get("PSYCHOLOGIST_PHONE", "")
 
SYSTEM_PROMPT = """Ты — ассистент психолога Алии. Ты не проводишь терапию, не ставишь диагнозы, не даёшь медицинских назначений.
 
Твои задачи:
1. Вежливо приветствовать и отвечать на вопросы
2. Объяснять формат консультаций
3. Предлагать запись
4. При признаках кризиса — мягко рекомендовать обратиться за экстренной помощью (телефон доверия: 150)
 
Информация об Алие:
- Консультации онлайн (видеосвязь)
- Длительность сессии: 1,5 часа
- Стоимость: 25 000 тенге за сессию
- Пакет: 10 сессий — 200 000 тенге
- Методы: ACT, КПТ (CBT), DBT
- Работает с: тревогой, эмоциональной регуляцией, отношениями, самооценкой, стрессом
 
Если клиент хочет записаться — собери по очереди (один вопрос за раз):
1. Имя
2. Город или часовой пояс
3. Удобные дни и время
4. Кратко — с каким запросом обращается
 
После того как собрал все 4 пункта — напиши подтверждение и добавь в конце строку:
ЗАПИСЬ: Имя: {имя} | Город: {город} | Время: {время} | Запрос: {запрос}
 
Никогда не обещай гарантированный результат. Не используй "я вас вылечу".
Пиши на русском, тепло и профессионально. Сообщения короткие — как в реальном чате.
Не используй маркированные списки со звёздочками или дефисами."""
 
# История диалогов в памяти
conversations: dict[str, list] = {}
 
 
# ───────────────────────── Claude ─────────────────────────
 
async def ask_claude(chat_id: str, user_message: str) -> str:
    if chat_id not in conversations:
        conversations[chat_id] = []
 
    conversations[chat_id].append({"role": "user", "content": user_message})
    messages = conversations[chat_id][-20:]
 
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": messages,
            },
        )
        data = resp.json()
        reply = data["content"][0]["text"]
 
    conversations[chat_id].append({"role": "assistant", "content": reply})
    return reply
 
 
def extract_booking(reply: str):
    """Вернуть строку ЗАПИСЬ: если она есть, иначе None"""
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:"):
            return line
    return None
 
 
# ───────────────────────── WhatsApp ─────────────────────────
 
async def send_whatsapp(chat_id: str, message: str):
    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chatId": chat_id, "message": message})
 
 
async def handle_whatsapp(body: dict):
    if body.get("typeWebhook") != "incomingMessageReceived":
        return
    msg = body.get("messageData", {})
    if msg.get("typeMessage") != "textMessage":
        return
    text = msg.get("textMessageData", {}).get("textMessage", "").strip()
    chat_id = body.get("senderData", {}).get("chatId", "")
    if not text or not chat_id or "@g.us" in chat_id:
        return
 
    reply = await ask_claude(f"wa:{chat_id}", text)
    booking = extract_booking(reply)
 
    if booking and PSYCHOLOGIST_PHONE:
        phone = chat_id.replace("@c.us", "")
        notify = f"📋 Новая запись!\n\nКлиент из WhatsApp (+{phone}):\n{booking}"
        await send_whatsapp(PSYCHOLOGIST_PHONE + "@c.us", notify)
        reply = reply.replace(booking, "").strip()
 
    await send_whatsapp(chat_id, reply)
 
 
# ───────────────────────── Telegram ─────────────────────────
 
async def send_telegram(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})
 
 
async def handle_telegram(body: dict):
    message = body.get("message") or body.get("edited_message")
    if not message:
        return
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not text or not chat_id:
        return
 
    # Приветствие на /start
    if text == "/start":
        text = "Здравствуйте! Хочу узнать подробнее о консультациях."
 
    reply = await ask_claude(f"tg:{chat_id}", text)
    booking = extract_booking(reply)
 
    if booking and PSYCHOLOGIST_PHONE:
        username = message.get("from", {}).get("username", "")
        name = message.get("from", {}).get("first_name", "")
        notify = f"📋 Новая запись!\n\nКлиент из Telegram (@{username} / {name}):\n{booking}"
        await send_whatsapp(PSYCHOLOGIST_PHONE + "@c.us", notify)
        reply = reply.replace(booking, "").strip()
 
    await send_telegram(chat_id, reply)
 
 
# ───────────────────────── Startup: регистрация вебхука ─────────────────────────
 
async def set_telegram_webhook():
    if not TELEGRAM_TOKEN:
        return
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not railway_url:
        print("RAILWAY_PUBLIC_DOMAIN не задан, вебхук Telegram не установлен")
        return
    webhook_url = f"https://{railway_url}/telegram"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={"url": webhook_url})
        print(f"Telegram webhook: {resp.json()}")
 
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    await set_telegram_webhook()
    yield
 
app = FastAPI(lifespan=lifespan)
 
 
# ───────────────────────── Роуты ─────────────────────────
 
@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try:
        body = await request.json()
        await handle_whatsapp(body)
    except Exception as e:
        print(f"WhatsApp error: {e}")
    return JSONResponse({"status": "ok"})
 
 
@app.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        await handle_telegram(body)
    except Exception as e:
        print(f"Telegram error: {e}")
    return JSONResponse({"status": "ok"})
 
 
@app.get("/")
async def root():
    return {"status": "Бот Алии работает — WhatsApp + Telegram ✅"}
 
 
@app.get("/health")
async def health():
    return {"status": "ok"}
