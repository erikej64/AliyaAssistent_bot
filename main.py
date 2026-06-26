import os
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GREEN_API_ID = os.environ["GREEN_API_ID"]
GREEN_API_TOKEN = os.environ["GREEN_API_TOKEN"]
PSYCHOLOGIST_PHONE = os.environ.get("PSYCHOLOGIST_PHONE", "")  # номер Алии для уведомлений

SYSTEM_PROMPT = """Ты — ассистент психолога Алии. Ты не проводишь терапию, не ставишь диагнозы, не даёшь медицинских назначений.

Твои задачи:
1. Вежливо приветствовать и отвечать на вопросы
2. Объяснять формат консультаций
3. Предлагать запись
4. При признаках кризиса — мягко рекомендовать обратиться за экстренной помощью (телефон доверия: 150)

Информация об Алие:
- Консультации онлайн (видеосвязь)
- Длительность сессии: 1,5 часа
- Стоимость: 20 000 тенге за сессию
- Пакет: 10 сессий — 150 000 тенге
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

# Хранилище истории диалогов в памяти (chat_id -> list of messages)
conversations: dict[str, list] = {}


async def send_whatsapp(chat_id: str, message: str):
    """Отправить сообщение через Green API"""
    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    payload = {"chatId": chat_id, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


async def ask_claude(chat_id: str, user_message: str) -> str:
    """Получить ответ от Claude с историей диалога"""
    if chat_id not in conversations:
        conversations[chat_id] = []

    conversations[chat_id].append({"role": "user", "content": user_message})

    # Ограничиваем историю последними 20 сообщениями
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

    # Если бот собрал данные для записи — уведомить Алию
    if "ЗАПИСЬ:" in reply and PSYCHOLOGIST_PHONE:
        booking_line = [l for l in reply.split("\n") if l.startswith("ЗАПИСЬ:")]
        if booking_line:
            notify = f"📋 Новая запись!\n\nКлиент из WhatsApp:\n{booking_line[0]}\n\nНомер клиента: {chat_id.replace('@c.us', '')}"
            await send_whatsapp(PSYCHOLOGIST_PHONE + "@c.us", notify)
            # Убрать строку ЗАПИСЬ из ответа клиенту
            reply = reply.replace(booking_line[0], "").strip()

    return reply


@app.post("/webhook")
async def webhook(request: Request):
    """Получаем входящие сообщения от Green API"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "ok"})

    # Green API присылает разные типы событий
    type_webhook = body.get("typeWebhook")

    if type_webhook != "incomingMessageReceived":
        return JSONResponse({"status": "ok"})

    message_data = body.get("messageData", {})
    msg_type = message_data.get("typeMessage")

    # Обрабатываем только текстовые сообщения
    if msg_type != "textMessage":
        return JSONResponse({"status": "ok"})

    text = message_data.get("textMessageData", {}).get("textMessage", "").strip()
    sender_data = body.get("senderData", {})
    chat_id = sender_data.get("chatId", "")

    if not text or not chat_id:
        return JSONResponse({"status": "ok"})

    # Игнорируем сообщения от групп
    if "@g.us" in chat_id:
        return JSONResponse({"status": "ok"})

    # Получаем ответ от Claude
    reply = await ask_claude(chat_id, text)

    # Отправляем ответ клиенту
    await send_whatsapp(chat_id, reply)

    return JSONResponse({"status": "ok"})


@app.get("/")
async def root():
    return {"status": "Бот Алии работает ✅"}


@app.get("/health")
async def health():
    return {"status": "ok"}
