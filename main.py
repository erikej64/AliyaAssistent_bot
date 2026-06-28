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

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

def get_system_prompt() -> str:
    now = datetime.utcnow() + timedelta(hours=5)  # Астана UTC+5 (не меняется, нет летнего времени)
    today_str = now.strftime("%d.%m.%Y")
    tomorrow = (now + timedelta(days=1)).strftime("%d.%m.%Y")
    day_after = (now + timedelta(days=2)).strftime("%d.%m.%Y")
    weekday = WEEKDAYS[now.weekday()]
    current_time = now.strftime("%H:%M")
    year = now.year

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
- Алия работает по времени Астаны (UTC+5)

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

После сбора всех 4 пунктов — напиши клиенту короткое подтверждение, и ОБЯЗАТЕЛЬНО в конце сообщения добавь служебную строку в точно таком формате (без изменений структуры):
ЗАПИСЬ: Имя: {{имя}} | Город: {{город}} | Дата: {{ГГГГ-ММ-ДД}} | Время: {{ЧЧ:ММ}} | Запрос: {{запрос}}

ВАЖНО для строки ЗАПИСЬ:
- Дата всегда в формате ГГГГ-ММ-ДД (например 2026-06-28)
- Время только цифры ЧЧ:ММ по Астане, без слов (например 19:00)
- Строка ЗАПИСЬ должна быть последней строкой сообщения
- Без этой строки запись не сохранится в календарь!

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

async def get_busy_slots(date: str) -> list[tuple[str, str]]:
    """Получить занятые слоты на дату (возвращает список (начало, конец) по Астане)"""
    try:
        token = await get_google_token()
        # Начало и конец дня в UTC (Астана UTC+5, значит -5 часов)
        day_start = f"{date}T00:00:00+05:00"
        day_end = f"{date}T23:59:59+05:00"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": day_start,
                    "timeMax": day_end,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            events = resp.json().get("items", [])
            slots = []
            for e in events:
                start = e.get("start", {}).get("dateTime", "")
                end = e.get("end", {}).get("dateTime", "")
                if start and end:
                    # Берём только ЧЧ:ММ
                    s = start[11:16]
                    en = end[11:16]
                    slots.append((s, en))
            print(f"Занятые слоты на {date}: {slots}")
            return slots
    except Exception as e:
        print(f"❌ Ошибка чтения календаря: {e}")
        return []


async def is_slot_free(date: str, time_str: str) -> tuple[bool, list[str]]:
    """Проверить свободен ли слот. Возвращает (свободен, список свободных часов дня)"""
    busy = await get_busy_slots(date)
    
    # Время начала и конца новой записи
    try:
        h, m = map(int, time_str.split(":"))
        new_start = h * 60 + m
        new_end = new_start + 90  # 1.5 часа
    except:
        return True, []

    # Проверяем пересечение
    for (s, e) in busy:
        try:
            sh, sm = map(int, s.split(":"))
            eh, em = map(int, e.split(":"))
            busy_start = sh * 60 + sm
            busy_end = eh * 60 + em
            if new_start < busy_end and new_end > busy_start:
                # Слот занят — найдём свободные часы
                free = []
                for hour in range(9, 21):  # с 9:00 до 21:00
                    slot_start = hour * 60
                    slot_end = slot_start + 90
                    free_slot = True
                    for (bs, be) in busy:
                        bsh, bsm = map(int, bs.split(":"))
                        beh, bem = map(int, be.split(":"))
                        bs_min = bsh * 60 + bsm
                        be_min = beh * 60 + bem
                        if slot_start < be_min and slot_end > bs_min:
                            free_slot = False
                            break
                    if free_slot:
                        free.append(f"{hour:02d}:00")
                return False, free
        except:
            continue
    
    return True, []
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
                "system": get_system_prompt(),
                "messages": messages,
            },
        )
        data = resp.json()
        reply = data["content"][0]["text"]

    conversations[chat_id].append({"role": "assistant", "content": reply})
    print(f"BOT REPLY [{chat_id}]: {reply[:200]}")
    return reply


async def extract_booking_from_reply(reply: str) -> dict | None:
    """Сначала ищем строку ЗАПИСЬ:, если нет — используем Claude для извлечения данных"""
    # Попытка 1: стандартный формат
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:"):
            result = parse_booking(line)
            if result:
                return result

    # Попытка 2: извлечь через Claude если есть ключевые слова подтверждения
    keywords = ["подтверждаю", "записал", "записала", "запись", "консультаци"]
    if not any(k in reply.lower() for k in keywords):
        return None

    now = datetime.utcnow() + timedelta(hours=5)
    extract_prompt = f"""Из текста ниже извлеки данные о записи клиента к психологу.
Сегодня: {now.strftime('%Y-%m-%d')}.
Верни ТОЛЬКО JSON без пояснений:
{{"имя": "...", "город": "...", "дата": "ГГГГ-ММ-ДД", "время": "ЧЧ:ММ", "запрос": "..."}}
Время должно быть по Астане (UTC+5). Если данных нет — верни null.

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
                    "messages": [{"role": "user", "content": extract_prompt}],
                },
            )
            text = resp.json()["content"][0]["text"].strip()
            print(f"EXTRACT RAW: {text[:300]}")
            if not text or text == "null":
                return None
            text = text.replace("```json", "").replace("```", "").strip()
            if not text or text == "null":
                return None
            data = json.loads(text)
            # Исправляем год если нужно
            if data.get("дата"):
                import re
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})", data["дата"])
                if m and int(m.group(1)) < now.year:
                    data["дата"] = f"{now.year}-{m.group(2)}-{m.group(3)}"
            # Очищаем время
            if data.get("время"):
                import re
                t = re.search(r"\d{1,2}:\d{2}", data["время"])
                if t:
                    data["время"] = t.group(0).zfill(5)
            print(f"✅ Данные извлечены через Claude: {data}")
            return data
    except Exception as e:
        print(f"❌ Ошибка извлечения данных: {e}")
        return None


async def process_reply(reply: str, source: str, contact: str) -> str:
    booking = await extract_booking_from_reply(reply)
    if not booking:
        return reply

    clean_reply = reply
    # Убираем строку ЗАПИСЬ: если она есть
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:"):
            clean_reply = reply.replace(line, "").strip()
            break

    # Проверяем занятость слота
    is_free, free_slots = await is_slot_free(booking["дата"], booking["время"])

    if not is_free:
        if free_slots:
            free_str = ", ".join(free_slots[:5])
            busy_msg = (
                f"К сожалению, {booking['дата']} в {booking['время']} уже занято 😔\n\n"
                f"В этот день свободны: {free_str} (по Астане)\n\n"
                f"Какое время вам подойдёт?"
            )
        else:
            busy_msg = (
                f"К сожалению, {booking['дата']} полностью занято 😔\n\n"
                f"Давайте подберём другой день — какая дата вам подходит?"
            )
        print(f"⚠️ Слот занят: {booking['дата']} {booking['время']}")
        return busy_msg

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

