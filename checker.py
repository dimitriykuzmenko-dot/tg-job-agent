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


# ---------------------------------------------------- дедуп и AI-фильтр ----

import hashlib

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|@\w+")
_NONWORD_RE = re.compile(r"[^\wа-яё]+", re.IGNORECASE)


def fingerprint(text: str) -> str:
    """Отпечаток текста поста: одинаковые вакансии из разных каналов
    дают одинаковый отпечаток, даже если различаются ссылками/эмодзи."""
    t = _URL_RE.sub(" ", text.lower())
    t = _NONWORD_RE.sub("", t)
    return hashlib.md5(t[:600].encode("utf-8")).hexdigest()


def ai_verdict(profile: str, post_text: str):
    """Спрашивает у Claude Haiku, релевантна ли вакансия профилю.
    Возвращает (True/False, причина) или None при ошибке/отсутствии ключа."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    prompt = (
        f"{profile}\n\n"
        "Ниже пост из телеграм-канала с вакансиями. Реши, релевантна ли "
        "вакансия этому кандидату. Если пост вообще не вакансия (реклама, "
        "статья, дайджест без конкретной позиции) — это НЕ релевантно.\n"
        "Ответь строго одной строкой JSON без пояснений: "
        '{"relevant": true|false, "reason": "очень кратко, до 10 слов"}\n\n'
        f"ПОСТ:\n{post_text[:2000]}"
    )
    try:
        r = requests.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        return bool(data.get("relevant")), str(data.get("reason", ""))[:120]
    except Exception as e:
        print(f"[warn] AI-фильтр недоступен ({e}), переключаюсь на ключевые слова")
        return None


def is_relevant(post_text: str, config) -> tuple:
    """Решает, релевантен ли пост. Возвращает (bool, описание_причины).

    Логика: AI-фильтр (если включён и есть ключ) после широкого префильтра;
    при недоступности AI — откат на строгий список keywords."""
    ai_cfg = config.get("ai_filter") or {}
    keywords = config.get("keywords", [])
    exclude = config.get("exclude_keywords", [])

    low = post_text.lower()
    if any(_keyword_hit(ex, low) for ex in exclude):
        return False, "стоп-слово"

    if ai_cfg.get("enabled"):
        pre = ai_cfg.get("prefilter_keywords", [])
        if pre and not any(_keyword_hit(p, low) for p in pre):
            return False, "не прошёл префильтр"
        verdict = ai_verdict(ai_cfg.get("profile", ""), post_text)
        if verdict is not None:
            ok, reason = verdict
            return ok, f"AI: {reason}" if reason else "AI"
        # AI недоступен — откат на ключевые слова

    matched = [kw for kw in keywords if _keyword_hit(kw, low)]
    if matched:
        return True, "слова: " + ", ".join(matched[:4])
    return False, "нет совпадений"




# ---------------------------------------------------------------- hh.ru ----

HH_API = "https://api.hh.ru/vacancies"
HH_HEADERS = {"User-Agent": "tg-job-agent/1.0 (personal job monitor)"}


def fetch_hh_vacancies(search: dict):
    """Один поисковый запрос к hh.ru. Возвращает список вакансий или None."""
    params = {
        "text": search["text"],
        "search_field": "name",       # ищем только в названии вакансии
        "period": 2,                  # за последние 2 дня (дедупом отсеем)
        "per_page": 50,
        "order_by": "publication_time",
    }
    if search.get("area"):
        params["area"] = search["area"]
    if search.get("schedule"):
        params["schedule"] = search["schedule"]
    token = os.environ.get("HH_API_TOKEN", "").strip()
    headers = dict(HH_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(HH_API, params=params, headers=headers, timeout=30)
        if r.status_code == 403:
            print("[error] hh.ru вернул 403 — анонимный доступ ограничен. "
                  "Зарегистрируй приложение на dev.hh.ru и добавь секрет "
                  "HH_API_TOKEN в репозиторий.")
            return None
        r.raise_for_status()
        return r.json().get("items", [])
    except requests.RequestException as e:
        print(f"[error] hh.ru ({search['text'][:40]}): {e}")
        return None


def format_hh_salary(salary) -> str:
    if not salary:
        return ""
    cur = {"RUR": "₽", "USD": "$", "EUR": "€"}.get(
        salary.get("currency"), salary.get("currency") or ""
    )
    lo, hi = salary.get("from"), salary.get("to")
    if lo and hi:
        s = f"{lo:,}–{hi:,} {cur}"
    elif lo:
        s = f"от {lo:,} {cur}"
    elif hi:
        s = f"до {hi:,} {cur}"
    else:
        return ""
    return "💰 " + s.replace(",", " ")


def process_hh(config, state, exclude_keywords, token, chat_id, first_run):
    hh_cfg = config.get("hh") or {}
    if not hh_cfg.get("enabled"):
        return 0, 0

    seen = set(state.setdefault("seen_hh", []))
    matched_total, sent_total = 0, 0

    for search in hh_cfg.get("searches", []):
        items = fetch_hh_vacancies(search)
        if items is None:
            continue
        for v in items:
            vid = str(v.get("id"))
            if vid in seen:
                continue
            seen.add(vid)
            if first_run:
                continue

            name = v.get("name", "")
            low = name.lower()
            if any(_keyword_hit(ex, low) for ex in exclude_keywords):
                continue

            salary_min = hh_cfg.get("salary_from")
            if salary_min:
                s = v.get("salary") or {}
                top = s.get("to") or s.get("from")
                if top and top < salary_min:
                    continue
                if not top and hh_cfg.get("only_with_salary"):
                    continue

            employer = (v.get("employer") or {}).get("name", "")
            area = (v.get("area") or {}).get("name", "")
            sched = (v.get("schedule") or {}).get("name", "")
            salary_line = format_hh_salary(v.get("salary"))

            lines = [f"🟥 hh.ru: {name}", f"🏢 {employer}"]
            loc = " · ".join(x for x in [area, sched] if x)
            if loc:
                lines.append(f"📍 {loc}")
            if salary_line:
                lines.append(salary_line)
            lines.append(f"\n👉 {v.get('alternate_url', '')}")

            matched_total += 1
            if send_telegram_message(token, chat_id, "\n".join(lines)):
                sent_total += 1
            time.sleep(1.5)
        time.sleep(0.5)  # пауза между поисковыми запросами

    state["seen_hh"] = sorted(seen)[-2000:]
    return matched_total, sent_total


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
    sent_fps = state.setdefault("sent_fingerprints", [])
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

            if not post["text"].strip():
                continue

            # Дедупликация: одна вакансия часто публикуется в нескольких
            # каналах — шлём только первую встреченную копию.
            fp = fingerprint(post["text"])
            if fp in sent_fps:
                print(f"[dup] {channel}/{post['id']}: уже присылал из другого канала")
                continue

            relevant, why = is_relevant(post["text"], config)
            if not relevant:
                continue

            total_matched += 1
            preview = post["text"][:700]
            msg = (
                f"💼 Вакансия в @{channel}\n"
                f"🔎 {why}\n\n"
                f"{preview}{'…' if len(post['text']) > 700 else ''}\n\n"
                f"👉 {post['url']}"
            )
            if send_telegram_message(token, chat_id, msg):
                total_sent += 1
                sent_fps.append(fp)
            time.sleep(1.5)  # не упираемся в лимиты Bot API

        # Храним только последние N ID, чтобы state.json не разрастался
        seen[channel] = sorted(channel_seen)[-MAX_SEEN_IDS_PER_CHANNEL:]
        time.sleep(1)  # пауза между каналами, чтобы не злить t.me

    state["first_run_done"] = True
    state["sent_fingerprints"] = sent_fps[-800:]

    # ------------------------------------------------------------ hh.ru ----
    hh_first_run = not state.get("hh_first_run_done", False)
    hh_matched, hh_sent = process_hh(
        config, state, config.get("exclude_keywords", []),
        token, chat_id, hh_first_run,
    )
    state["hh_first_run_done"] = True

    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"Telegram-каналы: новых постов {total_new}, "
          f"совпадений {total_matched}, отправлено {total_sent}")
    print(f"hh.ru: совпадений {hh_matched}, отправлено {hh_sent}")
    if first_run or hh_first_run:
        print("Первый запуск источника: проиндексировано без уведомлений, "
              "рассылка начнётся со следующего запуска.")


if __name__ == "__main__":
    main()
