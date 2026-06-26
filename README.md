# Бот-ассистент психолога Алии 🤖

WhatsApp-бот на базе Claude AI для психолога Алии.

## Деплой на Railway

### 1. Загрузи код на GitHub
- Создай новый репозиторий на github.com
- Загрузи все файлы (main.py, requirements.txt, Procfile)

### 2. Деплой на Railway
- Зайди на railway.app
- New Project → Deploy from GitHub repo
- Выбери свой репозиторий

### 3. Переменные окружения (Variables)
В Railway → твой проект → Variables добавь:

```
ANTHROPIC_API_KEY=sk-ant-...твой ключ...
GREEN_API_ID=твой idInstance
GREEN_API_TOKEN=твой apiTokenInstance
PSYCHOLOGIST_PHONE=77001234567  # номер Алии без + (для уведомлений)
```

### 4. Получи URL сервера
После деплоя Railway даст URL вида:
`https://aliya-bot-production.up.railway.app`

### 5. Настрой вебхук в Green API
- Зайди на app.green-api.com
- Выбери свой инстанс
- Настройки → URL для уведомлений:
  `https://твой-url.up.railway.app/webhook`
- Включи: "Получать уведомления о входящих сообщениях"

### 6. Подключи WhatsApp
- В Green API → QR код → сканируй своим WhatsApp

### 7. Тест
Напиши боту с любого номера — он должен ответить!

## Структура проекта
- `main.py` — основной сервер
- `requirements.txt` — зависимости Python
- `Procfile` — команда запуска для Railway
