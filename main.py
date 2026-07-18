import os
import uvicorn
import httpx
import google.generativeai as genai
from fastapi import FastAPI, Request

# Конфигурация
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')
app = FastAPI()

# Синхронная функция (без await)
def ask_gemini(user_message: str) -> str:
    try:
        chat = model.start_chat(history=[])
        response = chat.send_message(user_message)
        return response.text
    except Exception as e:
        print(f"Ошибка Gemini: {e}")
        return "Ошибка при получении ответа от ИИ."

# Эндпоинт Telegram
@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")
    
    if text and chat_id:
        reply = ask_gemini(text)
        
        # Отправка ответа через httpx (вместо requests)
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": chat_id, "text": reply})
            
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "running"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
