"""
Мониторинг Minecraft-сервера Aternos с уведомлениями в Telegram.

Проверяет доступность Java-сервера напрямую, а при сетевой ошибке
использует mcsrvstat.us как запасной источник. Неизвестный статус
не считается автоматически офлайном.

Render:
- Build Command: pip install -r requirements.txt
- Start Command: python mc_monitor.py
- Environment Variables: TELEGRAM_TOKEN, CHAT_ID
"""

import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from mcstatus import JavaServer

# ========= НАСТРОЙКИ =========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()

# Указанный Telegram ID. При необходимости можно переопределить в Render.
CHAT_ID = os.environ.get("CHAT_ID", "1126888389").strip()

SERVER_HOST = "on3daysmp.aternos.me"
SERVER_PORT = 40856
SERVER_ADDRESS = f"{SERVER_HOST}:{SERVER_PORT}"

# Это имя игрока, которого нужно отслеживать, если выбран режим PLAYER.
BOT_NICKNAME = os.environ.get("BOT_NICKNAME", "bratok").strip()

# SERVER: считать онлайн, если сервер ответил на status-запрос.
# PLAYER: считать онлайн только если BOT_NICKNAME виден в списке игроков.
MONITOR_MODE = os.environ.get("MONITOR_MODE", "SERVER").strip().upper()

CHECK_INTERVAL = 30
FAILS_TO_OFFLINE = 3
UNKNOWN_WARN_AFTER = 20
DIRECT_TIMEOUT = 8
API_TIMEOUT = 15

PROXY = os.environ.get("PROXY", "").strip()

MSG_ONLINE = "Сервер Minecraft работает ✅"
MSG_OFFLINE = "Сервер Minecraft недоступен ❌"
MSG_UNKNOWN = (
    "Не удалось достоверно проверить статус ⚠️\n"
    "Сервер может работать, но проверки не получают ответ."
)
MSG_BLIND = (
    "⚠️ Давно не удаётся достоверно проверить Minecraft-сервер. "
    "Статус неизвестен; это не означает, что сервер выключен."
)
# =============================

_last_info = None


def request_proxies():
    return {"http": PROXY, "https": PROXY} if PROXY else None


def send_telegram(message: str, chat_id=None) -> bool:
    if not TELEGRAM_TOKEN:
        print("[Telegram] Не задан TELEGRAM_TOKEN в Environment Render.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            data={"chat_id": chat_id or CHAT_ID, "text": message},
            timeout=15,
            proxies=request_proxies(),
        )
        if not response.ok:
            print(f"[Telegram] HTTP {response.status_code}: {response.text[:500]}")
            return False
        result = response.json()
        if not result.get("ok"):
            print(f"[Telegram] API error: {result}")
            return False
        return True
    except (requests.RequestException, ValueError) as exc:
        print(f"[Telegram] Ошибка отправки: {type(exc).__name__}: {exc}")
        return False


def check_via_api(prefix=""):
    """Возвращает (True/False/None, диагностическое сообщение)."""
    url = f"https://api.mcsrvstat.us/3/{SERVER_ADDRESS}"
    try:
        response = requests.get(
            url,
            timeout=API_TIMEOUT,
            proxies=request_proxies(),
            headers={"User-Agent": "mc-status-telegram-bot/2.0"},
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        return None, (
            f"{prefix}mcsrvstat.us не дал пригодный ответ: "
            f"{type(exc).__name__}: {exc}"
        )

    players = data.get("players") or {}
    online_count = players.get("online")
    raw_names = players.get("list") or []
    names = [
        str(player.get("name", ""))
        if isinstance(player, dict)
        else str(player)
        for player in raw_names
    ]
    names = [name for name in names if name]

    info = (
        f"{prefix}mcsrvstat.us: online={data.get('online')!r}, "
        f"игроков={online_count!r}/{players.get('max', '?')!r}, "
        f"имена={names or 'не предоставлены'}"
    )

    # В режиме SERVER оцениваем доступность сервера, а не присутствие игроков.
    if MONITOR_MODE == "SERVER":
        if data.get("online") is True:
            return True, info
        if data.get("online") is False:
            return False, info
        return None, info + " [статус неизвестен]"

    # В режиме PLAYER требуется явное присутствие заданного ника.
    if MONITOR_MODE == "PLAYER":
        if any(name.casefold() == BOT_NICKNAME.casefold() for name in names):
            return True, info
        if online_count == 0:
            return False, info + " [на сервере 0 игроков]"
        if online_count is not None and isinstance(online_count, int) and online_count > 0:
            return None, info + " [игроки есть, но список имён недоступен]"
        if data.get("online") is False:
            return False, info
        return None, info + " [невозможно подтвердить присутствие игрока]"

    return None, info + f" [неизвестный MONITOR_MODE={MONITOR_MODE!r}]"


def check_server(server):
    """Возвращает (True/False/None, диагностическое сообщение)."""
    try:
        status = server.status()
    except Exception as exc:
        # Сетевой таймаут сам по себе не доказывает, что сервер выключен.
        return check_via_api(f"Прямое подключение не удалось ({type(exc).__name__}). ")

    players = getattr(status, "players", None)
    online_count = getattr(players, "online", None)
    sample = getattr(players, "sample", None) or []
    names = [getattr(player, "name", "") for player in sample]
    try:
        version = status.version.name
    except Exception:
        version = "?"
    try:
        motd = status.motd.to_plain()
    except Exception:
        motd = "?"

    info = (
        f"Прямой ответ сервера: игроков={online_count!r}/"
        f"{getattr(players, 'max', '?')!r}, "
        f"видимые имена={names or 'не предоставлены'}, "
        f"версия={version}, MOTD={motd}"
    )

    if MONITOR_MODE == "SERVER":
        return True, info

    if MONITOR_MODE == "PLAYER":
        if any(name.casefold() == BOT_NICKNAME.casefold() for name in names if name):
            return True, info
        if online_count == 0:
            return False, info + " [на сервере 0 игроков]"
        if isinstance(online_count, int) and online_count > 0:
            return None, info + " [игроки есть, но список имён не подтверждает ник]"
        return None, info + " [невозможно подтвердить присутствие игрока]"

    return None, info + f" [неизвестный MONITOR_MODE={MONITOR_MODE!r}]"


def bot_is_online(server):
    global _last_info
    result, info = check_server(server)
    if info != _last_info:
        print(f"[Проверка] {info}", flush=True)
        _last_info = info
    return result


def tg_api(method: str, **params):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_TOKEN")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    response = requests.post(
        url, data=params, timeout=40, proxies=request_proxies()
    )
    response.raise_for_status()
    return response.json()


def commands_loop():
    try:
        server = JavaServer.lookup(SERVER_ADDRESS, timeout=DIRECT_TIMEOUT)
    except Exception as exc:
        print(f"[Команды] Не удалось создать объект сервера: {exc}", flush=True)
        server = None

    offset = 0
    try:
        tg_api(
            "setMyCommands",
            commands='[{"command":"status","description":"Проверить статус сервера"},'
                     '{"command":"debug","description":"Диагностика проверки"}]',
        )
    except Exception as exc:
        print(f"[Команды] Не удалось установить команды: {exc}", flush=True)

    # Пропускаем накопившиеся обновления при старте.
    try:
        first = tg_api("getUpdates", offset=-1, timeout=0)
        if first.get("ok") and first.get("result"):
            offset = first["result"][-1]["update_id"] + 1
    except Exception as exc:
        print(f"[Команды] Не удалось прочитать начальные обновления: {exc}", flush=True)

    while True:
        try:
            data = tg_api(
                "getUpdates",
                offset=offset,
                timeout=30,
                allowed_updates='["message"]',
            )
            if not data.get("ok"):
                print(f"[Команды] Telegram API: {data}", flush=True)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                command = text.split()[0].split("@")[0].lower()
                chat_id = msg.get("chat", {}).get("id")
                print(f"[Команды] Получена {command}", flush=True)

                if command in ("/status", "/start", "/debug"):
                    if server is None:
                        try:
                            server = JavaServer.lookup(
                                SERVER_ADDRESS, timeout=DIRECT_TIMEOUT
                            )
                        except Exception:
                            pass

                    if command == "/debug":
                        if server is not None:
                            _, details = check_server(server)
                        else:
                            _, details = check_via_api("Прямое подключение недоступно. ")
                        send_telegram(details, chat_id=chat_id)
                    else:
                        result = bot_is_online(server) if server is not None else None
                        if result is None:
                            send_telegram(MSG_UNKNOWN, chat_id=chat_id)
                        else:
                            send_telegram(
                                MSG_ONLINE if result else MSG_OFFLINE,
                                chat_id=chat_id,
                            )
        except Exception as exc:
            print(
                f"[Команды] Ошибка: {type(exc).__name__}: {exc}; повтор через 5 сек",
                flush=True,
            )
            time.sleep(5)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()
    except Exception as exc:
        print(f"[Health] Не удалось запустить HTTP-сервер: {exc}", flush=True)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Добавь TELEGRAM_TOKEN в Render → Environment. "
            "Токен нельзя публиковать в коде."
        )
    if not CHAT_ID:
        raise RuntimeError("Не задан CHAT_ID.")

    server = JavaServer.lookup(SERVER_ADDRESS, timeout=DIRECT_TIMEOUT)

    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=commands_loop, daemon=True).start()

    state = None
    reported = None
    fails = 0
    unknown = 0
    warned = False

    print(
        f"Мониторинг запущен: {SERVER_ADDRESS}; режим={MONITOR_MODE}; "
        f"интервал={CHECK_INTERVAL} сек.",
        flush=True,
    )

    while True:
        try:
            online = bot_is_online(server)

            if online is None:
                unknown += 1
                # Не объявляем сервер офлайн из-за отсутствия данных.
                if unknown >= UNKNOWN_WARN_AFTER and not warned:
                    if send_telegram(MSG_BLIND):
                        warned = True
                time.sleep(CHECK_INTERVAL)
                continue

            unknown = 0
            warned = False

            if online:
                fails = 0
                if state is not True:
                    state = True
                    print("-> онлайн", flush=True)
            else:
                fails += 1
                if state is not False and fails >= FAILS_TO_OFFLINE:
                    state = False
                    print(
                        f"-> офлайн ({fails} отрицательных проверок подряд)",
                        flush=True,
                    )

            if state is not None and state != reported:
                if send_telegram(MSG_ONLINE if state else MSG_OFFLINE):
                    reported = state
                    print("Уведомление отправлено в Telegram.", flush=True)

        except Exception as exc:
            # Неизвестная ошибка проверки не должна считаться офлайном.
            print(
                f"[Главный цикл] Ошибка: {type(exc).__name__}: {exc}",
                flush=True,
            )

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
