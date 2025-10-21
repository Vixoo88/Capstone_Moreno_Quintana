import os
import requests
from django.conf import settings

def send_telegram_message(text: str, *, return_detail: bool = False):
    """
    Envía un mensaje a Telegram.
    - Por defecto devuelve solo bool (compatibilidad con tu código actual).
    - Si return_detail=True, devuelve (ok: bool, detalle: str).
    Hace POST (form-data) y si falla, prueba GET (como en tu navegador).
    """
    token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat  = (getattr(settings, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    def ret(ok, detail=""):
        return (ok, detail) if return_detail else ok

    if not token:
        return ret(False, "Falta TELEGRAM_BOT_TOKEN")
    if token.startswith("bot"):
        return ret(False, "No incluyas el prefijo 'bot' en el token")
    if not chat:
        return ret(False, "Falta TELEGRAM_CHAT_ID")

    base = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    # 1) POST (form-data)
    try:
        r = requests.post(base, data=payload, timeout=8)
        if r.ok:
            data = r.json()
            if data.get("ok"):
                return ret(True, "OK (POST)")
        # 2) Fallback GET (igual que el link que te funcionó)
        rg = requests.get(base, params=payload, timeout=8)
        if rg.ok and rg.json().get("ok"):
            return ret(True, "OK (GET)")
        return ret(False, f"POST {r.status_code}:{r.text} | GET {rg.status_code}:{rg.text}")
    except requests.exceptions.SSLError as e:
        return ret(False, f"SSL error: {e}")
    except Exception as e:
        return ret(False, f"Excepción: {e}")
