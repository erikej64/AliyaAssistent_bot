import os
import json
import httpx
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GREEN_API_ID = os.environ["GREEN_API_ID"]
GREEN_API_TOKEN = os.environ["GREEN_API_TOKEN"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
PSYCHOLOGIST_PHONE = os.environ.get("PSYCHOLOGIST_PHONE", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

_creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "{}")
try:
    GOOGLE_CREDS = json.loads(_creds_raw)
except Exception:
    GOOGLE_CREDS = {}

CURRENT_YEAR = datetime.now().year

SYSTEM_PROMPT = f"""Ты — ассистент психолога Алии. Ты не проводишь терапию, не ставишь диагнозы, не даёшь медицинских назначений.

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
- Алия работает по времени Астаны (UTC+5)

Текущий год: {CURRENT_YEAR}.

Если клиент хочет записаться — собери по очереди (один вопрос за раз):
1. Имя
2. Город — это важно для определения часового пояса
3. Удобные дата и время — спроси в каком часовом поясе это время. Затем самостоятельно переведи в UTC+5 (Астана). Например: Новосибирск UTC+7, значит 15:00 по Новосибирску = 13:00 по Астане. Москва UTC+3, значит 15:00 по Москве = 17:00 по Астане.
4. Кратко — с каким запросом обращается

После того как собрал все 4 пункта — напиши клиенту подтверждение с временем В ЕГО ЧАСОВОМ ПОЯСЕ и временем по Астане. Затем добавь строку точно в таком формате (время пиши уже переведённое в UTC+5/Астана):
ЗАПИСЬ: Имя: {{имя}} | Город: {{город}} | Дата: {{ГГГГ-ММ-ДД}} | Время: {{ЧЧ:ММ по Астане}} | Запрос: {{запрос}}

Пример для клиента из Новосибирска (UTC+7) который хочет в 15:00 по своему времени:
— скажи клиенту: встреча в 15:00 по вашему времени (13:00 по Астане)
— в строке ЗАПИСЬ укажи время 13:00

Никогда не обещай гарантированный результат. Не используй "я вас вылечу".
Пиши на русском, тепло и профессионально. Сообщения короткие — как в реальном чате.
Не используй маркированные списки со звёздочками или дефисами."""

conversations: dict[str, list] = {}


# ───────────────────────── Google Calendar ─────────────────────────

async def get_google_token() -> str:
    import base64, time

    if not GOOGLE_CREDS:
        raise Exception("GOOGLE_CREDENTIALS не настроены")

    service_account = GOOGLE_CREDS.get("client_email", "")
    private_key = GOOGLE_CREDS.get("private_key", "")

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()

    now = int(time.time())
    claim = base64.urlsafe_b64encode(
        json.dumps({
            "iss": service_account,
            "scope": "https://www.googleapis.com/auth/calendar",
            "aud": "https://oauth2.googleapis.com/token",
            "exp": now + 3600,
            "iat": now,
        }).encode()
    ).rstrip(b"=").decode()

    signing_input = f"{header}.{claim}"

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key_obj = serialization.load_pem_private_key(private_key.encode(), password=None)
    signature = private_key_obj.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    jwt_token = f"{signing_input}.{sig_b64}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt_token},
        )
        data = resp.json()
        if "access_token" not in data:
            raise Exception(f"Token error: {data}")
        return data["access_token"]


async def create_calendar_event(name: str, date: str, time_str: str, request_text: str, city: str) -> bool:
    try:
        token = await get_google_token()
        start_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(hours=1, minutes=30)

        title = f"Консультация: {name}" if name else "Консультация"

        event = {
            "summary": title,
            "description": (
                f"Клиент: {name}\n"
                f"Город: {city}\n"
                f"Запрос: {request_text}\n"
                f"Время указано по Астане (UTC+5)\n\n"
                f"Записан через бота"
            ),
            "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": "Asia/Almaty"},
            "end":   {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:00"),   "timeZone": "Asia/Almaty"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 60},
                    {"method": "email", "minutes": 1440},
                ],
            },
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=event,
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                print(f"✅ Событие создано: {result.get('summary')} {date} {time_str} (Астана)")
                return True
            else:
                print(f"❌ Calendar API error {resp.status_code}: {resp.text}")
                return False
    except Exception as e:
        print(f"❌ Ошибка создания события: {e}")
        return False


def parse_booking(line: str) -> dict | None:
    try:
        import re
        data = {}
        parts = line.replace("ЗАПИСЬ:", "").strip().split("|")
        for part in parts:
            key, _, val = part.partition(":")
            data[key.strip().lower()] = val.strip()
        if all(k in data for k in ["имя", "город", "дата", "время", "запрос"]):
            # Исправляем год если нужно
            date_parts = data["дата"].split("-")
            if len(date_parts) == 3 and int(date_parts[0]) < CURRENT_YEAR:
                data["дата"] = f"{CURRENT_YEAR}-{date_parts[1]}-{date_parts[2]}"
            # Вытаскиваем только ЧЧ:ММ из поля времени (убираем "по Астане" и прочее)
            time_match = re.search(r"\d{1,2}:\d{2}", data["время"])
            if time_match:
                data["время"] = time_match.group(0).zfill(5)
            return data
    except Exception:
        pass
    return None


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


def extract_booking_line(reply: str) -> str | None:
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:"):
            return line
    return None


async def process_reply(reply: str, source: str, contact: str) -> str:
    booking_line = extract_booking_line(reply)
    if not booking_line:
        return reply

    booking = parse_booking(booking_line)
    clean_reply = reply.replace(booking_line, "").strip()

    if not booking:
        return clean_reply

    calendar_ok = False
    if GOOGLE_CALENDAR_ID and GOOGLE_CREDS:
        calendar_ok = await create_calendar_event(
            name=booking["имя"],
            date=booking["дата"],
            time_str=booking["время"],
            request_text=booking["запрос"],
            city=booking["город"],
        )

    if PSYCHOLOGIST_PHONE:
        cal_status = "✅ Добавлено в Google Calendar" if calendar_ok else "⚠️ Добавьте в календарь вручную"
        notify = (
            f"📋 Новая запись через {source}!\n\n"
            f"Клиент: {booking.get('имя')}\n"
            f"Город: {booking.get('город')}\n"
            f"Дата: {booking.get('дата')} в {booking.get('время')} (по Астане)\n"
            f"Запрос: {booking.get('запрос')}\n"
            f"Контакт: {contact}\n\n"
            f"{cal_status}"
        )
        await send_whatsapp(PSYCHOLOGIST_PHONE + "@c.us", notify)

    return clean_reply


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

    phone = chat_id.replace("@c.us", "")
    reply = await ask_claude(f"wa:{chat_id}", text)
    reply = await process_reply(reply, "WhatsApp", f"+{phone}")
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

    if text == "/start":
        text = "Здравствуйте! Хочу узнать подробнее о консультациях."

    username = message.get("from", {}).get("username", "")
    name = message.get("from", {}).get("first_name", "")
    contact = f"@{username}" if username else name

    reply = await ask_claude(f"tg:{chat_id}", text)
    reply = await process_reply(reply, "Telegram", contact)
    await send_telegram(chat_id, reply)


# ───────────────────────── Startup ─────────────────────────

async def set_telegram_webhook():
    if not TELEGRAM_TOKEN:
        return
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            json={"url": f"https://{domain}/telegram"},
        )
        print(f"Telegram webhook: {resp.json()}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await set_telegram_webhook()
    yield

app = FastAPI(lifespan=lifespan)


# ───────────────────────── Routes ─────────────────────────

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
    return {"status": f"Бот Алии — WhatsApp + Telegram + Google Calendar ✅"}


@app.get("/health")
async def health():
    return {"status": "ok"}

    
