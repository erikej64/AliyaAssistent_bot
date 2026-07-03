import os
import re
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
GOOGLE_CALENDAR_ID_PERSONAL = os.environ.get("GOOGLE_CALENDAR_ID_PERSONAL", "")

_creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "{}")
try:
    GOOGLE_CREDS = json.loads(_creds_raw)
except Exception:
    GOOGLE_CREDS = {}

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SESSION_BLOCK = 120  # 1.5ч сеанс + 0.5ч обработка
WORK_START = 10 * 60
WORK_END   = 21 * 60


def now_astana() -> datetime:
    return datetime.utcnow() + timedelta(hours=5)


def get_system_prompt() -> str:
    now = now_astana()
    today_str    = now.strftime("%d.%m.%Y")
    tomorrow     = (now + timedelta(days=1)).strftime("%d.%m.%Y")
    day_after    = (now + timedelta(days=2)).strftime("%d.%m.%Y")
    weekday      = WEEKDAYS[now.weekday()]
    current_time = now.strftime("%H:%M")
    year         = now.year

    return f"""Ты — ассистент психолога Алии. Ты не проводишь терапию, не ставишь диагнозы, не даёшь медицинских назначений.

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
- Алия работает по времени Астаны (UTC+5), с 10:00 до 21:00

ТЕКУЩАЯ ДАТА И ВРЕМЯ (Астана UTC+5):
- Сегодня: {today_str} ({weekday}), {current_time}
- Завтра: {tomorrow}
- Послезавтра: {day_after}
- Год: {year}

Когда клиент говорит "сегодня" → дата {today_str}
Когда клиент говорит "завтра" → дата {tomorrow}
Когда клиент говорит "послезавтра" → дата {day_after}

Если клиент хочет записаться — собери по очереди (один вопрос за раз):
1. Имя
2. Город — важно для часового пояса
3. Дату и время — переведи в UTC+5 (Астана). Новосибирск UTC+7: 15:00 = 13:00 Астана. Москва UTC+3: 15:00 = 17:00 Астана. Казахстан — время не меняй.
4. Кратко — с каким запросом обращается

После сбора всех 4 пунктов — напиши клиенту короткое подтверждение и ОБЯЗАТЕЛЬНО добавь последней строкой:
ЗАПИСЬ: Имя: {{имя}} | Город: {{город}} | Дата: {{ГГГГ-ММ-ДД}} | Время: {{ЧЧ:ММ}} | Запрос: {{запрос}}

ВАЖНО для строки ЗАПИСЬ:
- Дата в формате ГГГГ-ММ-ДД (например {year}-07-15)
- Время только цифры ЧЧ:ММ по Астане (например 14:00)
- Строка ЗАПИСЬ — последняя строка, без неё запись не сохранится!

Никогда не обещай гарантированный результат. Не используй "я вас вылечу".
Пиши на русском, тепло и профессионально. Сообщения короткие — как в реальном чате.
Не используй маркированные списки со звёздочками или дефисами."""


conversations: dict[str, list] = {}


# ───────────────────────── Google Calendar ─────────────────────────

async def get_google_token() -> str:
    import base64, time as _time

    if not GOOGLE_CREDS:
        raise Exception("GOOGLE_CREDENTIALS не настроены")

    service_account = GOOGLE_CREDS.get("client_email", "")
    private_key     = GOOGLE_CREDS.get("private_key", "")

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()

    now = int(_time.time())
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

    pk = serialization.load_pem_private_key(private_key.encode(), password=None)
    signature = pk.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
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


async def get_busy_slots(date: str, calendar_id: str = None) -> list[tuple[int, int]]:
    """Возвращает занятые интервалы (start_min, end_min) по Астане для указанного календаря."""
    cal = calendar_id or GOOGLE_CALENDAR_ID
    try:
        token = await get_google_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": f"{date}T00:00:00+05:00",
                    "timeMax": f"{date}T23:59:59+05:00",
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            events = resp.json().get("items", [])
            slots = []
            for e in events:
                s  = e.get("start", {}).get("dateTime", "")
                en = e.get("end",   {}).get("dateTime", "")
                if s and en:
                    sh, sm = int(s[11:13]),  int(s[14:16])
                    eh, em = int(en[11:13]), int(en[14:16])
                    slots.append((sh * 60 + sm, eh * 60 + em))
            print(f"Занятые слоты [{cal[:20]}] на {date}: {slots}")
            return slots
    except Exception as e:
        print(f"❌ Ошибка чтения календаря {cal[:20]}: {e}")
        return []


async def check_slot(date: str, time_str: str) -> tuple[bool, list[str]]:
    """Проверить слот в обоих календарях. Возвращает (свободен, 2 ближайших слота)."""
    # Читаем оба календаря
    busy = await get_busy_slots(date, GOOGLE_CALENDAR_ID)
    if GOOGLE_CALENDAR_ID_PERSONAL:
        busy += await get_busy_slots(date, GOOGLE_CALENDAR_ID_PERSONAL)

    h, m = map(int, time_str.split(":"))
    req_start = h * 60 + m
    req_end   = req_start + SESSION_BLOCK

    def slot_free(s: int) -> bool:
        se = s + SESSION_BLOCK
        for bs, be in busy:
            if s < be and se > bs:
                return False
        return True

    # Проверяем запрошенный слот
    if not slot_free(req_start):
        before, after = [], []
        for mins in range(WORK_START, WORK_END - SESSION_BLOCK + 1, 30):
            if slot_free(mins):
                if mins % 60 != 0:
                    continue
                label = f"{mins // 60:02d}:00"
                if mins < req_start and slot_free(mins):
                    before.append(label)
                elif mins >= req_start + SESSION_BLOCK:
                    after.append(label)

        suggestions = []
        if before:
            suggestions.append(before[-1])
        if after:
            suggestions.append(after[0])

        return False, suggestions

    return True, []


async def create_calendar_event(name: str, date: str, time_str: str, request_text: str, city: str) -> bool:
    try:
        token    = await get_google_token()
        start_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        end_dt   = start_dt + timedelta(hours=1, minutes=30)

        event = {
            "summary": f"Консультация: {name}" if name else "Консультация",
            "description": (
                f"Клиент: {name}\nГород: {city}\nЗапрос: {request_text}\n"
                f"Время по Астане (UTC+5)\nЗаписан через бота"
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
                print(f"✅ Событие создано: {name} {date} {time_str}")
                return True
            else:
                print(f"❌ Calendar API error {resp.status_code}: {resp.text}")
                return False
    except Exception as e:
        print(f"❌ Ошибка создания события: {e}")
        return False


# ───────────────────────── Парсинг данных записи ─────────────────────────

def parse_booking_line(line: str) -> dict | None:
    try:
        data  = {}
        parts = line.replace("ЗАПИСЬ:", "").strip().split("|")
        for part in parts:
            key, _, val = part.partition(":")
            data[key.strip().lower()] = val.strip()
        if not all(k in data for k in ["имя", "город", "дата", "время", "запрос"]):
            return None
        dp = data["дата"].split("-")
        if len(dp) == 3 and int(dp[0]) < now_astana().year:
            data["дата"] = f"{now_astana().year}-{dp[1]}-{dp[2]}"
        t = re.search(r"\d{1,2}:\d{2}", data["время"])
        if t:
            data["время"] = t.group(0).zfill(5)
        return data
    except Exception:
        return None


async def extract_booking(reply: str) -> dict | None:
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:"):
            result = parse_booking_line(line)
            if result:
                return result

    keywords = ["подтверждаю", "записал", "записала", "запись", "консультаци"]
    if not any(k in reply.lower() for k in keywords):
        return None

    now    = now_astana()
    prompt = f"""Из текста извлеки данные о записи клиента к психологу.
Сегодня: {now.strftime('%Y-%m-%d')}.
Верни ТОЛЬКО JSON (без markdown, без пояснений):
{{"имя": "...", "город": "...", "дата": "ГГГГ-ММ-ДД", "время": "ЧЧ:ММ", "запрос": "..."}}
Если данных нет — верни null.

Текст:
{reply}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            text = resp.json()["content"][0]["text"].strip()
            print(f"EXTRACT RAW: {text[:300]}")
            text = text.replace("```json", "").replace("```", "").strip()
            if not text or text == "null":
                return None
            data = json.loads(text)
            if data.get("дата"):
                m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", data["дата"])
                if m2 and int(m2.group(1)) < now.year:
                    data["дата"] = f"{now.year}-{m2.group(2)}-{m2.group(3)}"
            if data.get("время"):
                t2 = re.search(r"\d{1,2}:\d{2}", data["время"])
                if t2:
                    data["время"] = t2.group(0).zfill(5)
            print(f"✅ Данные извлечены: {data}")
            return data
    except Exception as e:
        print(f"❌ Ошибка извлечения: {e}")
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
                "system": get_system_prompt(),
                "messages": messages,
            },
        )
        reply = resp.json()["content"][0]["text"]

    conversations[chat_id].append({"role": "assistant", "content": reply})
    print(f"BOT REPLY [{chat_id}]: {reply[:200]}")
    return reply


async def process_reply(reply: str, source: str, contact: str) -> str:
    booking = await extract_booking(reply)
    if not booking:
        return reply

    clean_reply = reply
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:"):
            clean_reply = reply.replace(line, "").strip()
            break

    is_free, suggestions = await check_slot(booking["дата"], booking["время"])

    if not is_free:
        if suggestions:
            s   = " и ".join(suggestions)
            msg = (
                f"К сожалению, {booking['время']} {booking['дата']} уже занято 😔\n\n"
                f"В этот день рядом свободно: {s} (по Астане)\n\n"
                f"Какое время вам подойдёт?"
            )
        else:
            msg = (
                f"К сожалению, {booking['дата']} полностью занят 😔\n\n"
                f"Давайте подберём другой день — какая дата вам удобна?"
            )
        print(f"⚠️ Слот занят: {booking['дата']} {booking['время']}")
        return msg

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
            f"Дата: {booking.get('дата')} в {booking.get('время')} (Астана)\n"
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
    msg_type = msg.get("typeMessage")
    if msg_type == "textMessage":
        text = msg.get("textMessageData", {}).get("textMessage", "").strip()
    elif msg_type == "extendedTextMessage":
        text = msg.get("extendedTextMessageData", {}).get("text", "").strip()
    else:
        return
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
    text    = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not text or not chat_id:
        return

    if text == "/start":
        text = "Здравствуйте! Хочу узнать подробнее о консультациях."

    username = message.get("from", {}).get("username", "")
    name     = message.get("from", {}).get("first_name", "")
    contact  = f"@{username}" if username else name

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
    return {"status": "Бот Алии — WhatsApp + Telegram + Google Calendar ✅"}


@app.get("/health")
async def health():
    return {"status": "ok"}
