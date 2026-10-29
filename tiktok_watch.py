#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok Watcher — следит за новыми видео указанных блогеров и присылает уведомление.

Запуск:  run.bat
Опции:   --once            проверить один раз и выйти
         --setup-telegram  настроить Telegram-бота
         --test            проверить связь с TikTok и настройки
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(os.environ.get("TIKTOK_WATCHER_HOME") or Path(__file__).resolve().parent)
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "watcher.log"
VIDEOS_DIR = BASE_DIR / "videos"
CACHE_DIR = BASE_DIR / ".cache"
NOTIFY_PS1 = BASE_DIR / "notify.ps1"

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None

DEFAULT_CONFIG = {
    "bloggers": ["kz_lemon4ik", "chopocompik_off"],
    "interval_minutes": 10,
    "videos_per_check": 20,
    "download_video": True,
    "download_quality": "best",
    "first_run_silent": True,
    "windows_toast": True,
    "open_in_browser": False,
    "discord_webhook": "",
    "telegram": {"bot_token": "", "chat_id": "", "proxy": ""},
    "keep_seen_ids": 1000,
}


# --------------------------------------------------------------------------- #
#  Логирование
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Конфиг / состояние
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception as e:
            log(f"Не смог прочитать config.json ({e}), использую значения по умолчанию")
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg or {})
    if "telegram" not in merged or not isinstance(merged["telegram"], dict):
        merged["telegram"] = dict(DEFAULT_CONFIG["telegram"])

    # Переопределение через переменные окружения — удобно на сервере / в Docker / в CI.
    env = os.environ
    tg = dict(merged.get("telegram") or {})
    if env.get("TELEGRAM_BOT_TOKEN"):
        tg["bot_token"] = env["TELEGRAM_BOT_TOKEN"]
    if env.get("TELEGRAM_CHAT_ID"):
        tg["chat_id"] = env["TELEGRAM_CHAT_ID"]
    if env.get("TELEGRAM_PROXY"):
        tg["proxy"] = env["TELEGRAM_PROXY"]
    merged["telegram"] = tg
    if env.get("DISCORD_WEBHOOK"):
        merged["discord_webhook"] = env["DISCORD_WEBHOOK"]
    if env.get("BLOGGERS"):
        merged["bloggers"] = [b.strip() for b in env["BLOGGERS"].split(",") if b.strip()]
    if env.get("INTERVAL_MINUTES"):
        try:
            merged["interval_minutes"] = int(env["INTERVAL_MINUTES"])
        except ValueError:
            pass

    if not CONFIG_PATH.exists() and not env.get("TELEGRAM_BOT_TOKEN"):
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Создан файл настроек: {CONFIG_PATH}")
    return merged


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"users": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
#  Уведомления
# --------------------------------------------------------------------------- #
def show_toast(title: str, body: str) -> None:
    if sys.platform != "win32" or not shutil.which("powershell"):
        return
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(NOTIFY_PS1),
             title[:120], body[:500]],
            timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log(f"Уведомление Windows не показалось: {e}")


def _opener(proxy: str):
    # Пустой ProxyHandler({}) отключает даже системный прокси из реестра Windows —
    # так проверка «напрямую» действительно проверяет прямое соединение.
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# Локальные прокси, которые обычно поднимают v2rayN / Xray / Clash.
TG_PROXY_CANDIDATES = [
    "http://127.0.0.1:10809",
    "http://127.0.0.1:7890",
    "http://127.0.0.1:1080",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:8888",
]
_tg_proxy_cache: list[str] = []


def _telegram_reachable(proxy: str, timeout: int = 8) -> bool:
    try:
        req = urllib.request.Request("https://api.telegram.org/bot0:0/getMe",
                                     headers={"User-Agent": "TikTokWatcher/1.0"})
        with _opener(proxy).open(req, timeout=timeout) as r:
            r.read(64)
        return True
    except urllib.error.HTTPError:
        return True  # API ответил (401 Unauthorized) — канал живой
    except Exception:
        return False


def telegram_proxy(cfg: dict) -> str:
    """Рабочий прокси для api.telegram.org ('' = доступен напрямую)."""
    if _tg_proxy_cache:
        return _tg_proxy_cache[0]
    tg = cfg.setdefault("telegram", {})
    configured = (tg.get("proxy") or "").strip()
    candidates = ([configured] if configured else [""]) + [c for c in TG_PROXY_CANDIDATES if c != configured]
    for cand in candidates:
        if not _telegram_reachable(cand):
            continue
        if cand:
            if cand != configured:
                tg["proxy"] = cand
                save_config(cfg)
                log(f"Telegram: подхватил локальный прокси {cand}")
            else:
                log(f"Telegram: прокси {cand} работает")
        else:
            log("Telegram: доступен напрямую")
        _tg_proxy_cache.append(cand)
        return cand
    log("Telegram: api.telegram.org недоступен (ни напрямую, ни через локальные прокси 10809/7890/1080)")
    _tg_proxy_cache.append(configured)
    return configured


def send_discord(cfg: dict, item: dict, video_path: Path | None) -> bool:
    hook = (cfg.get("discord_webhook") or "").strip()
    if not hook:
        return False
    payload = {
        "username": "TikTok Watcher",
        "embeds": [{
            "title": f"Новое видео @{item['user']}: {item['title'] or ''}"[:250],
            "url": item["url"],
            "description": (item["desc"] or "без описания")[:1500],
            "color": 0xEE1D52,
            "image": {"url": item["thumb"]} if item["thumb"] else {},
            "footer": {"text": datetime.fromtimestamp(item["timestamp"]).strftime("%d.%m.%Y %H:%M")},
        }],
    }
    try:
        req = urllib.request.Request(hook, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "TikTokWatcher/1.0"}, method="POST")
        with _opener("").open(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:
        log(f"Discord: ошибка отправки: {e}")
        return False


def tg_api(cfg: dict, method: str, payload: dict | None = None,
           files: dict | None = None, timeout: int = 120) -> dict:
    tg = cfg.get("telegram") or {}
    token = (tg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("не задан bot_token")
    proxy = telegram_proxy(cfg)
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = "----TwB" + str(random.randint(10 ** 9, 10 ** 10))
        body = b""
        for k, v in (payload or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
        for field, (filename, content, ctype) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                     f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with _opener(proxy).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def send_telegram(cfg: dict, item: dict, video_path: Path | None) -> bool:
    tg = cfg.get("telegram") or {}
    if not (tg.get("bot_token") and tg.get("chat_id")):
        return False
    caption = (f"🎬 Новое видео @{item['user']}\n{(item['title'] or item['desc'] or '').strip()[:600]}\n"
               f"{item['url']}")
    try:
        size = video_path.stat().st_size if video_path and video_path.exists() else 0
        if size and size < 48 * 1024 * 1024:
            tg_api(cfg, "sendVideo", {"chat_id": tg["chat_id"], "caption": caption[:1000],
                                      "supports_streaming": "true"},
                   {"video": (video_path.name, video_path.read_bytes(), "video/mp4")})
        elif item.get("thumb"):
            tg_api(cfg, "sendPhoto", {"chat_id": tg["chat_id"], "caption": caption[:1000],
                                      "photo": item["thumb"]})
        else:
            tg_api(cfg, "sendMessage", {"chat_id": tg["chat_id"], "text": caption[:4000],
                                        "disable_web_page_preview": "false"})
        return True
    except Exception as e:
        log(f"Telegram: ошибка отправки: {e}")
        return False


def notify(cfg: dict, items: list[dict], paths: dict) -> None:
    if cfg.get("windows_toast", True):
        if len(items) == 1:
            it = items[0]
            body = (it["title"] or it["desc"] or "новое видео").strip()
            if paths.get(it["id"]):
                body += "\nВидео сохранено в папку videos"
            show_toast(f"Новое видео @{it['user']}", body)
        else:
            by_user: dict[str, int] = {}
            for it in items:
                by_user[it["user"]] = by_user.get(it["user"], 0) + 1
            show_toast("TikTok: новые видео",
                       ", ".join(f"@{u}: {n}" for u, n in by_user.items()))
    for item in items:
        if cfg.get("open_in_browser"):
            try:
                import os
                os.startfile(item["url"])  # noqa: S606
            except Exception:
                pass
        video_path = paths.get(item["id"])
        send_discord(cfg, item, video_path)
        send_telegram(cfg, item, video_path)


# --------------------------------------------------------------------------- #
#  Получение списка видео (yt-dlp)
# --------------------------------------------------------------------------- #
def ydl_opts(cfg: dict, **extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cachedir": str(CACHE_DIR),
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
    }
    opts.update(extra)
    return opts


def list_user_videos(cfg: dict, user: str) -> list[dict]:
    if yt_dlp is None:
        raise RuntimeError("не установлен модуль yt-dlp (pip install yt-dlp)")
    limit = max(3, int(cfg.get("videos_per_check", 20)))
    opts = ydl_opts(cfg, extract_flat="in_playlist", playlistend=limit)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{user}", download=False)
    entries = (info or {}).get("entries") or []
    items = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        thumb = ""
        for t in thumbs:
            if t.get("id") in ("cover", "originCover") and t.get("url"):
                thumb = t["url"]
                break
        if not thumb and thumbs:
            thumb = thumbs[-1].get("url") or ""
        ts = int(e.get("timestamp") or 0)
        items.append({
            "id": str(e["id"]),
            "user": user,
            "title": (e.get("title") or "").strip(),
            "desc": (e.get("description") or e.get("title") or "").strip(),
            "timestamp": ts,
            "url": e.get("url") or f"https://www.tiktok.com/@{user}/video/{e['id']}",
            "thumb": thumb,
            "duration": e.get("duration") or 0,
            "views": e.get("view_count") or 0,
            "likes": e.get("like_count") or 0,
        })
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return items


def download_video(cfg: dict, item: dict) -> Path | None:
    VIDEOS_DIR.mkdir(exist_ok=True)
    existing = list(VIDEOS_DIR.glob(f"{item['user']}_{item['id']}.*"))
    if existing:
        return existing[0]
    # ffmpeg на машине может отсутствовать — берём готовый одиночный формат.
    if cfg.get("download_quality", "best") == "best":
        fmt = "b[ext=mp4]/b"
    else:
        fmt = "b[height<=720][ext=mp4]/b[height<=720]/b"
    opts = ydl_opts(
        cfg,
        skip_download=False,
        format=fmt,
        outtmpl=str(VIDEOS_DIR / f"{item['user']}_{item['id']}.%(ext)s"),
        noplaylist=True,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(item["url"], download=True)
    except Exception as e:
        log(f"  скачивание не удалось: {e}")
        return None
    found = list(VIDEOS_DIR.glob(f"{item['user']}_{item['id']}.*"))
    return found[0] if found else None


# --------------------------------------------------------------------------- #
#  Один цикл проверки
# --------------------------------------------------------------------------- #
def check_once(cfg: dict) -> int:
    state = load_state()
    users_state = state.setdefault("users", {})
    new_total = 0
    for user in cfg["bloggers"]:
        user = (user or "").strip().lstrip("@")
        if not user:
            continue
        log(f"Проверяю @{user} ...")
        items = None
        last_err = None
        for attempt in range(1, 4):
            try:
                items = list_user_videos(cfg, user)
                break
            except Exception as e:
                last_err = e
                log(f"  попытка {attempt}/3 не удалась: {str(e)[:200]}")
                time.sleep(5 * attempt)
        if items is None:
            log(f"  @{user}: не удалось получить список видео ({last_err})")
            continue

        us = users_state.setdefault(user, {"initialized": False, "ids": []})
        known = set(us.get("ids") or [])
        if not us.get("initialized"):
            us["initialized"] = True
            us["ids"] = [it["id"] for it in items][: int(cfg.get("keep_seen_ids", 1000))]
            us["last_check"] = int(time.time())
            log(f"  @{user}: первый запуск — запомнил {len(items)} видео, уведомлений не будет")
            continue

        fresh = [it for it in items if it["id"] not in known]
        us["last_check"] = int(time.time())
        if not fresh:
            newest = ""
            if items and items[0]["timestamp"]:
                newest = datetime.fromtimestamp(items[0]["timestamp"]).strftime("%d.%m.%Y %H:%M")
            log(f"  @{user}: новых видео нет (последнее: {newest or '—'})")
            continue

        fresh.sort(key=lambda x: x["timestamp"])
        log(f"  @{user}: новых видео — {len(fresh)}")
        paths = {}
        for item in fresh:
            when = (datetime.fromtimestamp(item["timestamp"]).strftime("%d.%m.%Y %H:%M")
                    if item["timestamp"] else "—")
            log(f"    + [{when}] {item['title'][:80]}")
            log(f"      {item['url']}")
            if cfg.get("download_video", True):
                p = download_video(cfg, item)
                if p:
                    paths[item["id"]] = p
                    log(f"      сохранено: {p.name} ({p.stat().st_size // 1024} КБ)")
            if item.get("thumb"):
                item["thumb"] = item["thumb"]

        notify(cfg, fresh, paths)
        new_total += len(fresh)
        us["ids"] = ([it["id"] for it in fresh] + list(us.get("ids") or []))[: int(cfg.get("keep_seen_ids", 1000))]

    save_state(state)
    return new_total


# --------------------------------------------------------------------------- #
#  Telegram setup
# --------------------------------------------------------------------------- #
def setup_telegram(cfg: dict, token_arg: str = "") -> None:
    tg = cfg.setdefault("telegram", {"bot_token": "", "chat_id": "", "proxy": ""})
    print("\n=== Настройка Telegram-бота ===")

    # 1. Сеть
    proxy = telegram_proxy(cfg)
    if proxy:
        print(f"[1/4] Telegram API доступен через прокси {proxy}")
    else:
        print("[1/4] Telegram API доступен напрямую")

    # 2. Токен
    token = (token_arg or "").strip()
    if not token:
        print("[2/4] Получите токен у @BotFather: /newbot -> имя -> скопируйте строку вида 123456:AA...")
        token = input("      Токен бота: ").strip()
    if not token:
        print("Отменено.")
        return
    prev_token = (tg.get("bot_token") or "").strip()
    tg["bot_token"] = token
    try:
        me = tg_api(cfg, "getMe", {})
        bot = me.get("result", {})
        print(f"[2/4] Токен верный: @{bot.get('username')} ({bot.get('first_name')})")
    except Exception as e:
        tg["bot_token"] = prev_token
        print(f"[2/4] Токен не принят Telegram: {e}")
        print("      Скопируйте токен из @BotFather целиком, без пробелов.")
        return

    # 3. chat_id — ждём сообщение боту
    print("[3/4] Откройте Telegram, найдите своего бота и отправьте ему /start")
    print("      Жду сообщение до 3 минут ...")
    tg_api(cfg, "deleteWebhook", {"drop_pending_updates": "false"})
    chat_id = None
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            res = tg_api(cfg, "getUpdates", {"timeout": 0, "limit": 10})
            for upd in res.get("result", []):
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                if chat.get("id"):
                    chat_id = chat["id"]
                    who = chat.get("username") or chat.get("first_name") or str(chat_id)
                    print(f"      Получено сообщение от {who}")
                    break
        except Exception as e:
            print(f"      Ошибка связи с Telegram: {e}")
            break
        if chat_id:
            break
        time.sleep(2)
    if not chat_id:
        print("[3/4] Сообщение не пришло. Убедитесь, что бот не заблокирован и напишите ему /start.")
        save_config(cfg)
        return
    tg["chat_id"] = str(chat_id)
    save_config(cfg)
    print(f"[3/4] chat_id = {chat_id} сохранён в config.json")

    # 4. Проверка отправки
    try:
        tg_api(cfg, "sendMessage", {"chat_id": tg["chat_id"],
                                    "text": "TikTok Watcher подключён ✅\n"
                                            "Новые видео будут приходить сюда."})
        print("[4/4] Тестовое сообщение отправлено — проверьте Telegram.")
    except Exception as e:
        print(f"[4/4] Тестовое сообщение не отправилось: {e}")
    print("\nГотово. Теперь запускайте run.bat — уведомления пойдут в Telegram.")


# --------------------------------------------------------------------------- #
#  Самопроверка
# --------------------------------------------------------------------------- #
def self_test(cfg: dict) -> int:
    ok = True
    print(f"Python: {sys.version.split()[0]}")
    print(f"yt-dlp: {getattr(yt_dlp, 'version', None) and yt_dlp.version.__version__}")
    try:
        import curl_cffi  # noqa: F401
        print("curl_cffi: установлен (impersonation доступна)")
    except ImportError:
        print("curl_cffi: НЕ установлен (рекомендуется: pip install curl_cffi)")
    print(f"Папка: {BASE_DIR}")
    print(f"Блогеры: {', '.join('@' + b for b in cfg['bloggers'])}")
    for user in cfg["bloggers"]:
        user = user.strip().lstrip("@")
        try:
            t0 = time.time()
            items = list_user_videos(cfg, user)
            dt = time.time() - t0
            newest = items[0] if items else None
            print(f"  @{user}: OK, {len(items)} видео за {dt:.1f} c; "
                  f"последнее: {newest['title'][:50] if newest else '—'}")
        except Exception as e:
            ok = False
            print(f"  @{user}: ОШИБКА — {str(e)[:200]}")
    hook = (cfg.get("discord_webhook") or "").strip()
    print(f"Discord: {'настроен' if hook else 'не настроен'}")
    tg = cfg.get("telegram") or {}
    print(f"Telegram: {'настроен' if tg.get('bot_token') and tg.get('chat_id') else 'не настроен'}")
    proxy = telegram_proxy(cfg)
    print(f"  прокси для Telegram: {proxy or 'без прокси (напрямую)'}")
    if tg.get("bot_token"):
        try:
            me = tg_api(cfg, "getMe", {})
            print(f"  связь с api.telegram.org: OK (@{me.get('result', {}).get('username')})")
        except Exception as e:
            print(f"  связь с api.telegram.org: ОШИБКА — {str(e)[:150]}")
    elif not proxy:
        print("  совет: включите VPN/прокси (v2rayN и т.п.) — api.telegram.org блокируется провайдером")
    print(f"Уведомления Windows: {'да' if cfg.get('windows_toast') else 'нет'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="проверить один раз и выйти")
    ap.add_argument("--setup-telegram", action="store_true", help="настроить Telegram-бота")
    ap.add_argument("--test", action="store_true", help="проверить настройки и связь с TikTok")
    ap.add_argument("--token", default="", help="токен бота для --setup-telegram")
    args = ap.parse_args()

    cfg = load_config()
    if args.setup_telegram:
        setup_telegram(cfg, args.token)
        return 0
    if args.test:
        return self_test(cfg)
    if not cfg.get("bloggers"):
        log("В config.json не указан ни один блогер (поле bloggers).")
        return 1
    if yt_dlp is None:
        log("Не установлен yt-dlp. Выполните: pip install yt-dlp curl_cffi")
        return 1

    interval = max(1, int(cfg.get("interval_minutes", 10)))
    tg_ok = bool((cfg.get("telegram") or {}).get("bot_token"))
    log("=" * 70)
    log(f"TikTok Watcher запущен. Блогеры: {', '.join('@' + b for b in cfg['bloggers'])}")
    log(f"Интервал проверки: {interval} мин. Остановить: Ctrl+C")
    log(f"Уведомления: Windows={'вкл' if cfg.get('windows_toast') else 'выкл'}, "
        f"Telegram={'вкл' if tg_ok else 'выкл'}, "
        f"Discord={'вкл' if cfg.get('discord_webhook') else 'выкл'}")
    log("=" * 70)

    if args.once:
        n = check_once(cfg)
        log(f"Готово. Новых видео: {n}")
        return 0

    while True:
        try:
            check_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"Ошибка цикла: {e}")
            log(traceback.format_exc(limit=3))
        pause = interval * 60 + random.randint(-20, 20)
        log(f"Следующая проверка через ~{max(1, pause // 60)} мин ...")
        time.sleep(max(60, pause))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
