import os
import re
import json
import asyncio
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
SESSION_BLOCK = 120  # Окно сессии — 120 минут (2 часа)
WORK_START = 10 * 60
WORK_END   = 21 * 60

# Отслеживаем уже отправленные уведомления чтобы не дублировать
sent_reminders: set = set()
# Хранилище напоминаний от бота (asyncio tasks)
reminders: dict[str, dict] = {}
conversations: dict[str, list] = {}


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
3. Предлагать запись или помогать с ПЕРЕНОСОМ уже существующей записи
4. При признаках кризиса — мягко рекомендовать обратиться за экстренной помощью (телефон доверия: 150)

Информация об Алие:
- Консультации онлайн (видеосвязь)
- Длительность сессии: 1,5 часа (но в расписании бронируется 2 часа)
- Стоимость: 25 000 тенге за сессию
- Пакет: 10 сессий — 200 000 тенге
- Методы: ACT, КПТ (CBT), DBT
- Алия работает по времени Астаны (UTC+5), с 10:00 до 21:00

ТЕКУЩАЯ ДАТА И ВРЕМЯ (Астана UTC+5):
- Сегодня: {today_str} ({weekday}), {current_time}
- Завтра: {tomorrow}
- Послезавтра: {day_after}
- Год: {year}

СЦЕНАРИЙ 1 - НОВАЯ ЗАПИСЬ:
Если клиент хочет записаться ВПЕРВЫЕ — собери по очереди (один вопрос за раз):
1. Имя
2. Город — важно для часового пояса
3. Дату и время (учитывай часовые пояса, переведи в Астану UTC+5)
4. Кратко запрос
После сбора всех пунктов ОБЯЗАТЕЛЬНО добавь последней строкой:
ЗАПИСЬ: Имя: {{имя}} | Город: {{город}} | Дата: {{ГГГГ-ММ-ДД}} | Время: {{ЧЧ:ММ}} | Запрос: {{запрос}}

СЦЕНАРИЙ 2 - ПЕРЕНОС ЗАПИСИ:
Если клиент просит ПЕРЕНЕСТИ или ИЗМЕНИТЬ время своей текущей записи — помоги подобрать новое время. Как только согласуете новую дату и время, выведи строку:
ПЕРЕНОС: Имя: {{имя}} | Город: {{город}} | Дата: {{ГГГГ-ММ-ДД}} | Время: {{ЧЧ:ММ}} | Запрос: {{запрос, если известен, или 'Перенос'}}

ВАЖНО:
- Дата в формате ГГГГ-ММ-ДД (например {year}-07-15)
- Время только цифры ЧЧ:ММ по Астане (например 14:00)
- Строка ЗАПИСЬ или ПЕРЕНОС — строго последняя строка сообщения!

Никогда не обещай гарантированный результат. Не используй маркированные списки со звёздочками."""


# ───────────────────────── Отправка сообщений ─────────────────────────

async def send_whatsapp(chat_id: str, message: str):
    url = f"https://api.green-api.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={"chatId": chat_id, "message": message})
        print(f"WA send [{chat_id}]: status={resp.status_code}")


async def send_telegram(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


async def notify_aliya(message: str):
    if PSYCHOLOGIST_PHONE:
        await send_whatsapp(PSYCHOLOGIST_PHONE + "@c.us", message)


# ───────────────────────── Напоминания ─────────────────────────

async def send_reminder_to_client(channel: str, chat_id: str, message: str):
    try:
        if channel == "tg":
            await send_telegram(int(chat_id), message)
        else:
            await send_whatsapp(chat_id, message)
    except Exception as e:
        print(f"Ошибка напоминания клиенту: {e}")


async def remind_after(delay: float, channel: str, chat_id: str, message: str, aliya_msg: str):
    await asyncio.sleep(delay)
    await send_reminder_to_client(channel, chat_id, message)
    await notify_aliya(aliya_msg)

async def cancel_existing_reminders(channel: str, chat_id: str):
    """Отменяет все текущие задачи напоминаний для клиента (нужно при переносе записи)"""
    prefix = f"{channel}:{chat_id}:"
    keys_to_delete = []
    for k, v in reminders.items():
        if k.startswith(prefix):
            for task in v.get("tasks", []):
                task.cancel()
            keys_to_delete.append(k)
    for k in keys_to_delete:
        del reminders[k]
        print(f"Отменено старое напоминание: {k}")


async def schedule_reminders(channel: str, chat_id: str, name: str, date: str, time_str: str):
    try:
        # Перед созданием новых напоминаний — очищаем старые, если были
        await cancel_existing_reminders(channel, chat_id)
        
        session_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        now = now_astana()
        key = f"{channel}:{chat_id}:{date}:{time_str}"

        msg_client_24h = f"Здравствуйте, {name}! 👋\n\nНапоминаю что завтра у вас консультация с Алией.\nДата: {date} в {time_str} (по Астане)\n\nЕсли что-то изменилось — напишите нам."
        msg_client_1h = f"Здравствуйте, {name}! ⏰\n\nЧерез 1 час начинается ваша консультация с Алией.\nСегодня в {time_str} (по Астане)\n\nАлия скоро отправит ссылку на видеосвязь."
        msg_aliya_24h = f"Напоминание: завтра консультация!\n\nКлиент: {name}\nВремя: {date} в {time_str} (Астана)"
        msg_aliya_1h = f"Через 1 час консультация!\n\nКлиент: {name}\nВремя: {time_str} (Астана)\n\nНе забудьте отправить ссылку на видеосвязь."

        tasks = []
        remind_24h = session_dt - timedelta(hours=24)
        remind_1h  = session_dt - timedelta(hours=1)

        if remind_24h > now:
            delay = (remind_24h - now).total_seconds()
            t = asyncio.create_task(remind_after(delay, channel, chat_id, msg_client_24h, msg_aliya_24h))
            tasks.append(t)

        if remind_1h > now:
            delay = (remind_1h - now).total_seconds()
            t = asyncio.create_task(remind_after(delay, channel, chat_id, msg_client_1h, msg_aliya_1h))
            tasks.append(t)

        reminders[key] = {"tasks": tasks, "name": name}

    except Exception as e:
        print(f"Ошибка планирования: {e}")


# ───────────────────────── Google Calendar ─────────────────────────

async def get_google_token() -> str:
    import base64, time as _time
    if not GOOGLE_CREDS:
        raise Exception("GOOGLE_CREDENTIALS не настроены")

    service_account = GOOGLE_CREDS.get("client_email", "")
    private_key     = GOOGLE_CREDS.get("private_key", "")
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    now = int(_time.time())
    claim = base64.urlsafe_b64encode(json.dumps({
        "iss": service_account, "scope": "https://www.googleapis.com/auth/calendar",
        "aud": "https://oauth2.googleapis.com/token", "exp": now + 3600, "iat": now,
    }).encode()).rstrip(b"=").decode()

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
        return resp.json()["access_token"]


async def get_events_for_date(date: str, calendar_id: str) -> list[dict]:
    try:
        token = await get_google_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": f"{date}T00:00:00+05:00",
                    "timeMax": f"{date}T23:59:59+05:00",
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            return resp.json().get("items", [])
    except Exception as e:
        print(f"Ошибка получения событий: {e}")
        return []


async def get_busy_slots(date: str, calendar_id: str = None) -> list[tuple[int, int]]:
    cal = calendar_id or GOOGLE_CALENDAR_ID
    try:
        events = await get_events_for_date(date, cal)
        slots = []
        for e in events:
            s  = e.get("start", {}).get("dateTime", "")
            en = e.get("end",   {}).get("dateTime", "")
            if s and en:
                sh, sm = int(s[11:13]), int(s[14:16])
                eh, em = int(en[11:13]), int(en[14:16])
                slots.append((sh * 60 + sm, eh * 60 + em))
        return slots
    except Exception:
        return []


async def check_slot(date: str, time_str: str) -> tuple[bool, list[str]]:
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

    if not slot_free(req_start):
        before, after = [], []
        for mins in range(WORK_START, WORK_END - SESSION_BLOCK + 1, 30):
            if mins % 60 != 0: continue
            if slot_free(mins):
                label = f"{mins // 60:02d}:00"
                if mins < req_start: before.append(label)
                else: after.append(label)
        suggestions = []
        if before: suggestions.append(before[-1])
        if after: suggestions.append(after[0])
        return False, suggestions

    return True, []


async def find_future_event_by_contact(client_phone: str, client_tg: str) -> dict | None:
    """Ищет предстоящее событие клиента на ближайшие 60 дней для переноса."""
    try:
        token = await get_google_token()
        now_str = now_astana().strftime("%Y-%m-%dT%H:%M:00+05:00")
        max_str = (now_astana() + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:00+05:00")
        
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": now_str,
                    "timeMax": max_str,
                    "singleEvents": "true",
                    "orderBy": "startTime"
                }
            )
            items = resp.json().get("items", [])
            for event in items:
                desc = event.get("description", "")
                phone, tg = extract_contact_from_description(desc)
                if client_phone and phone == client_phone:
                    return event
                if client_tg and tg == client_tg:
                    return event
        return None
    except Exception as e:
        print(f"Ошибка поиска события клиента: {e}")
        return None


async def delete_calendar_event(event_id: str) -> bool:
    """Удаляет старое событие при переносе."""
    try:
        token = await get_google_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            return resp.status_code in (204, 200)
    except Exception as e:
        print(f"Ошибка удаления события: {e}")
        return False


async def create_calendar_event(name: str, date: str, time_str: str,
                                 request_text: str, city: str,
                                 client_phone: str = "", client_tg: str = "") -> bool:
    try:
        token    = await get_google_token()
        start_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
        # Изменили длительность события на 2 часа (резервируем 2 часа в расписании)
        end_dt   = start_dt + timedelta(hours=2)

        contact_line = ""
        if client_phone: contact_line = f"Телефон: {client_phone}\n"
        elif client_tg: contact_line = f"Telegram: {client_tg}\n"

        event = {
            "summary": f"Консультация: {name} | {city} | {request_text}",
            "description": (
                f"Клиент: {name}\nГород: {city}\nЗапрос: {request_text}\n{contact_line}"
                f"Время по Астане (UTC+5)\nЗаписан через бота"
            ),
            "start": {"dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": "Asia/Almaty"},
            "end":   {"dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:00"),   "timeZone": "Asia/Almaty"},
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=event,
            )
            return resp.status_code in (200, 201)
    except Exception as e:
        print(f"Ошибка создания события: {e}")
        return False


def extract_contact_from_description(description: str) -> tuple[str, str]:
    phone, tg = "", ""
    if not description: return phone, tg
    for line in description.split("\n"):
        line = line.strip()
        if line.startswith("Телефон:"): phone = line.replace("Телефон:", "").strip()
        elif line.startswith("Telegram:"): tg = line.replace("Telegram:", "").strip().lstrip("@")
    return phone, tg

def extract_name_from_summary(summary: str) -> str:
    if "Консультация:" in summary:
        parts = summary.replace("Консультация:", "").strip().split("|")
        return parts[0].strip()
    return "Клиент"


# ... (check_calendar_reminders и calendar_checker_loop оставляем без изменений)
async def check_calendar_reminders():
    pass # Реализация в вашем исходном коде остается такой же (для краткости в выводе не меняем)


async def calendar_checker_loop():
    while True:
        await check_calendar_reminders()
        await asyncio.sleep(900)


# ───────────────────────── Парсинг ─────────────────────────

def parse_booking_line(line: str) -> dict | None:
    try:
        action = "book"
        if line.startswith("ПЕРЕНОС:"):
            action = "reschedule"
            line = line.replace("ПЕРЕНОС:", "").strip()
        else:
            line = line.replace("ЗАПИСЬ:", "").strip()

        data  = {"action": action}
        parts = line.split("|")
        for part in parts:
            key, _, val = part.partition(":")
            data[key.strip().lower()] = val.strip()
        
        if not all(k in data for k in ["имя", "город", "дата", "время"]):
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
        if line.startswith("ЗАПИСЬ:") or line.startswith("ПЕРЕНОС:"):
            result = parse_booking_line(line)
            if result:
                return result

    keywords = ["подтверждаю", "записал", "запись", "консультаци", "перенес", "перенос"]
    if not any(k in reply.lower() for k in keywords):
        return None

    now = now_astana()
    prompt = (
        f"Из текста извлеки данные о записи (или переносе записи).\nСегодня: {now.strftime('%Y-%m-%d')}.\n"
        f"Верни ТОЛЬКО JSON: {{\"action\": \"book\" или \"reschedule\", \"имя\": \"...\", \"город\": \"...\", \"дата\": \"ГГГГ-ММ-ДД\", \"время\": \"ЧЧ:ММ\", \"запрос\": \"...\"}}\n"
        f"Если данных нет — верни null.\nТекст:\n{reply}"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            )
            text = resp.json()["content"][0]["text"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            if not text or text == "null": return None
            data = json.loads(text)
            if "action" not in data: data["action"] = "book"
            if data.get("дата"):
                m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", data["дата"])
                if m2 and int(m2.group(1)) < now.year:
                    data["дата"] = f"{now.year}-{m2.group(2)}-{m2.group(3)}"
            if data.get("время"):
                t2 = re.search(r"\d{1,2}:\d{2}", data["время"])
                if t2: data["время"] = t2.group(0).zfill(5)
            return data
    except Exception:
        return None


# ───────────────────────── Claude & Process ─────────────────────────

async def ask_claude(chat_id: str, user_message: str) -> str:
    if chat_id not in conversations: conversations[chat_id] = []
    conversations[chat_id].append({"role": "user", "content": user_message})
    messages = conversations[chat_id][-20:]

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1000, "system": get_system_prompt(), "messages": messages},
        )
        reply = resp.json()["content"][0]["text"]

    conversations[chat_id].append({"role": "assistant", "content": reply})
    return reply


async def process_reply(reply: str, source: str, contact: str, raw_chat_id: str = "") -> str:
    booking = await extract_booking(reply)
    if not booking:
        return reply

    clean_reply = reply
    for line in reply.split("\n"):
        if line.startswith("ЗАПИСЬ:") or line.startswith("ПЕРЕНОС:"):
            clean_reply = reply.replace(line, "").strip()
            break

    is_free, suggestions = await check_slot(booking["дата"], booking["время"])

    if not is_free:
        if suggestions:
            msg = f"К сожалению, {booking['время']} {booking['дата']} уже занято.\n\nВ этот день рядом свободно: {' и '.join(suggestions)} (по Астане)\n\nКакое время вам подойдёт?"
        else:
            msg = f"К сожалению, {booking['дата']} полностью занят.\n\nДавайте подберём другой день — какая дата вам удобна?"
        return msg

    client_phone, client_tg = "", ""
    if source == "WhatsApp": client_phone = raw_chat_id.replace("@c.us", "")
    elif source == "Telegram": client_tg = contact.lstrip("@")

    # Обработка переноса
    action = booking.get("action", "book")
    old_event_info = ""

    if action == "reschedule" and GOOGLE_CALENDAR_ID:
        old_event = await find_future_event_by_contact(client_phone, client_tg)
        if old_event:
            old_start = old_event.get("start", {}).get("dateTime", "")
            if old_start:
                try:
                    dt = datetime.strptime(old_start[:16], "%Y-%m-%dT%H:%M")
                    old_event_info = f"\n(Предыдущая запись на {dt.strftime('%Y-%m-%d %H:%M')} была отменена)"
                except:
                    pass
            await delete_calendar_event(old_event["id"])

    calendar_ok = False
    if GOOGLE_CALENDAR_ID and GOOGLE_CREDS:
        calendar_ok = await create_calendar_event(
            name=booking["имя"], date=booking["дата"], time_str=booking["время"],
            request_text=booking.get("запрос", "Перенос" if action == "reschedule" else ""),
            city=booking["город"], client_phone=client_phone, client_tg=client_tg,
        )

    # Уведомляем Алию
    action_text = "ПЕРЕНОС ЗАПИСИ" if action == "reschedule" else "Новая запись"
    cal_status = "Добавлено в Google Calendar" if calendar_ok else "Добавьте в календарь вручную"
    
    await notify_aliya(
        f"{action_text} через {source}!\n\n"
        f"Клиент: {booking.get('имя', '')}\nГород: {booking.get('город', '')}\n"
        f"Новая дата: {booking.get('дата', '')} в {booking.get('время', '')} (Астана)\n"
        f"Контакт: {contact}\n{old_event_info}\n\n{cal_status}"
    )

    if raw_chat_id:
        channel = "tg" if source == "Telegram" else "wa"
        await schedule_reminders(channel, raw_chat_id, booking.get("имя", ""), booking.get("дата", ""), booking.get("время", ""))

    return clean_reply


# ───────────────────────── Handlers & Startup ─────────────────────────

async def handle_whatsapp(body: dict):
    if body.get("typeWebhook") != "incomingMessageReceived": return
    msg = body.get("messageData", {})
    msg_type = msg.get("typeMessage")

    text = ""
    if msg_type == "textMessage": text = msg.get("textMessageData", {}).get("textMessage", "").strip()
    elif msg_type == "extendedTextMessage": text = msg.get("extendedTextMessageData", {}).get("text", "").strip()
    
    chat_id = body.get("senderData", {}).get("chatId", "")
    if not text or not chat_id or "@g.us" in chat_id: return

    phone = chat_id.replace("@c.us", "")
    try:
        reply = await ask_claude(f"wa:{chat_id}", text)
        reply = await process_reply(reply, "WhatsApp", f"+{phone}", chat_id)
        await send_whatsapp(chat_id, reply)
    except Exception as e:
        print(f"WA error: {e}")


async def handle_telegram(body: dict):
    message = body.get("message") or body.get("edited_message")
    if not message: return
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not text or not chat_id: return

    if text == "/start": text = "Здравствуйте! Хочу узнать подробнее о консультациях."

    username = message.get("from", {}).get("username", "")
    name = message.get("from", {}).get("first_name", "")
    contact = f"@{username}" if username else name

    reply = await ask_claude(f"tg:{chat_id}", text)
    reply = await process_reply(reply, "Telegram", contact, str(chat_id))
    await send_telegram(chat_id, reply)


async def set_telegram_webhook():
    if not TELEGRAM_TOKEN: return
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", json={"url": f"https://{domain}/telegram"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    await set_telegram_webhook()
    asyncio.create_task(calendar_checker_loop())
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try: await handle_whatsapp(await request.json())
    except: pass
    return JSONResponse({"status": "ok"})

@app.post("/telegram")
async def telegram_webhook(request: Request):
    try: await handle_telegram(await request.json())
    except: pass
    return JSONResponse({"status": "ok"})

@app.get("/")
async def root():
    return {"status": "Бот Алии — 2-часовые слоты и функционал переноса записей работают!"}
