# landing/notifications.py
import os
import requests
from django.conf import settings

def send_telegram_message(text: str) -> bool:
    """
    Envía un mensaje de texto a Telegram. Devuelve True si OK.
    Lee token/chat_id desde settings o variables de entorno.
    """
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=5)
        return r.ok
    except Exception:
        return False
