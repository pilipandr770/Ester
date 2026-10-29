"""Тонкий клиент Evolution API (v2). Используем только то, что нужно Эстер:
создать инстанс, получить QR, список групп, скачать медиа, отправить напоминание."""
import requests
from config import Config

TIMEOUT = 30


def _h():
    return {"apikey": Config.EVOLUTION_API_KEY, "Content-Type": "application/json"}


def _url(path):
    return f"{Config.EVOLUTION_URL}{path}"


def create_instance(name, webhook_url):
    body = {
        "instanceName": name,
        "integration": "WHATSAPP-BAILEYS",
        "qrcode": True,
        # Важно для приватности: не читаем статусы и не синхронизируем всю историю
        "rejectCall": False,
        "groupsIgnore": False,
        "alwaysOnline": False,
        "readMessages": False,   # НЕ ставим «прочитано» в чатах пользователя
        "readStatus": False,
        "syncFullHistory": False,
        "webhook": {
            "url": webhook_url,
            "byEvents": False,
            "base64": False,
            "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE"],
        },
    }
    r = requests.post(_url("/instance/create"), json=body, headers=_h(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def connect(name):
    """Возвращает dict с base64 QR-кодом (поле 'base64') или pairing code."""
    r = requests.get(_url(f"/instance/connect/{name}"), headers=_h(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def state(name):
    r = requests.get(_url(f"/instance/connectionState/{name}"), headers=_h(), timeout=TIMEOUT)
    if r.status_code == 404:
        return "missing"
    r.raise_for_status()
    return (r.json().get("instance") or {}).get("state", "unknown")


def fetch_groups(name):
    r = requests.get(
        _url(f"/group/fetchAllGroups/{name}"),
        params={"getParticipants": "false"},
        headers=_h(),
        timeout=60,
    )
    r.raise_for_status()
    return [{"jid": g["id"], "name": g.get("subject", g["id"])} for g in r.json()]


def get_media_base64(name, message):
    """message — исходный объект сообщения из вебхука (data)."""
    body = {"message": {"key": message.get("key")}, "convertToMp4": False}
    r = requests.post(
        _url(f"/chat/getBase64FromMediaMessage/{name}"), json=body, headers=_h(), timeout=60
    )
    r.raise_for_status()
    j = r.json()
    return j.get("base64"), j.get("mimetype")


def send_text(name, number, text):
    body = {"number": number, "text": text}
    r = requests.post(_url(f"/message/sendText/{name}"), json=body, headers=_h(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def logout(name):
    requests.delete(_url(f"/instance/logout/{name}"), headers=_h(), timeout=TIMEOUT)


def delete_instance(name):
    requests.delete(_url(f"/instance/delete/{name}"), headers=_h(), timeout=TIMEOUT)
