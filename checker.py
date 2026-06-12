#!/usr/bin/env python3
"""
Telegram Job Agent
Мониторит публичные Telegram-каналы через веб-версию t.me/s/<канал>,
фильтрует посты по ключевым словам и присылает совпадения в личку через бота.

Запускается по расписанию в GitHub Actions. Состояние (какие посты уже
просмотрены) хранится в state.json и коммитится обратно в репозиторий.

Нужны переменные окружения:
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHAT_ID   — твой chat_id (узнать у @userinfobot)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

MAX_SEEN_IDS_PER_CHANNEL = 300  # сколько ID постов помним на канал


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn] не смог прочитать {path.name}, начинаю с чистого")
    return default


def fetch_channel_posts(channel: str):
    """Возвращает список постов: [{id, text, url, datetime}] или None при ошибке."""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[error] {channel}: не удалось загрузить ({e})")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    wraps = soup.select("div.tgme_widget_message")
    if not wraps:
        # Канал существует, но веб-превью отключено владельцем,
        # либо канал приватный/переименован.
        print(f"[warn] {channel}: посты не найдены — возможно, "
              f"у канала отключено веб-превью")
        return []

    posts = []
    for w in wraps:
        data_post = w.get("data-post", "")  # формат: channel/12345
        m = re.search(r"/(\d+)$", data_post)
        if not m:
            continue
        post_id = int(m.group(1))

        text_div = w.select_one("div.tgme_widget_message_text")
        text = text_div.get_text(separator="\n", strip=True) if text_div else ""

        time_tag = w.select_one("time[datetime]")
        post_dt = None
        if time_tag:
            try:
                post_dt = datetime.fromisoformat(
                    time_tag["datetime"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        posts.append({
            "id": post_id,
            "text": text,
            "url": f"https://t.me/{channel}/{post_id}",
            "datetime": post_dt,
        })
    return posts


def _keyword_hit(keyword: str, text_low: str) -> bool:
    kw = keyword.lower()
    # Короткие латинские аббревиатуры (ceo, coo, cmo...) — только как
    # отдельное слово, чтобы не срабатывать внутри других слов.
    if len(kw) <= 4 and kw.isascii() and kw.isalpha():
        return re.search(rf"\b{re.escape(kw)}\b", text_low) is not None
    return kw in text_low


def match_keywords(text: str, keywords, exclude_keywords):
    """Вернёт список сработавших ключевых слов или пустой список."""
    low = text.lower()
    for ex in exclude_keywords:
        if _keyword_hit(ex, low):
            return []
    return [kw for kw in keywords if _keyword_hit(kw, low)]


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(api, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"[error] Telegram API: {r.status_code} {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[error] Telegram API: {e}")
        return False


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[fatal] задай TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID "
              "в Secrets репозитория")
        sys.exit(1)

    config = load_json(CONFIG_PATH, None)
    if not config:
        print("[fatal] нет config.json")
        sys.exit(1)

    state = load_json(STATE_PATH, {"seen": {}, "first_run_done": False})
    seen = state.setdefault("seen", {})
    first_run = not state.get("first_run_done", False)

    max_age = timedelta(hours=config.get("max_post_age_hours", 48))
    now = datetime.now(timezone.utc)

    total_new, total_matched, total_sent = 0, 0, 0

    for channel in config["channels"]:
        posts = fetch_channel_posts(channel)
        if posts is None:
            continue  # сетевая ошибка — попробуем в следующий запуск

        channel_seen = set(seen.get(channel, []))
        new_posts = [p for p in posts if p["id"] not in channel_seen]
        total_new += len(new_posts)

        for post in sorted(new_posts, key=lambda p: p["id"]):
            channel_seen.add(post["id"])

            # На первом запуске только запоминаем посты, не шлём, —
            # иначе придёт лавина старых вакансий.
            if first_run:
                continue

            # Не шлём слишком старые посты (например, после долгого простоя)
            if post["datetime"] and now - post["datetime"] > max_age:
                continue

            matched = match_keywords(
                post["text"],
                config.get("keywords", []),
                config.get("exclude_keywords", []),
            )
            if not matched:
                continue

            total_matched += 1
            preview = post["text"][:700]
            msg = (
                f"💼 Вакансия в @{channel}\n"
                f"🔑 Совпало: {', '.join(matched)}\n\n"
                f"{preview}{'…' if len(post['text']) > 700 else ''}\n\n"
                f"👉 {post['url']}"
            )
            if send_telegram_message(token, chat_id, msg):
                total_sent += 1
            time.sleep(1.5)  # не упираемся в лимиты Bot API

        # Храним только последние N ID, чтобы state.json не разрастался
        seen[channel] = sorted(channel_seen)[-MAX_SEEN_IDS_PER_CHANNEL:]
        time.sleep(1)  # пауза между каналами, чтобы не злить t.me

    state["first_run_done"] = True
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"Готово: новых постов {total_new}, "
          f"совпадений {total_matched}, отправлено {total_sent}")
    if first_run:
        print("Первый запуск: посты проиндексированы, уведомления "
              "начнутся со следующего запуска.")


if __name__ == "__main__":
    main()
