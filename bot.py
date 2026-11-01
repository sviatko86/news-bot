"""
Новинний Telegram-бот: збирає RSS -> Claude вибирає топ-події та пише
короткий аналіз українською -> постить у канал.

Запуск:  python bot.py
Пробний запуск без публікації:  DRY_RUN=1 python bot.py
"""
import os
import re
import json
import html
import sqlite3
import calendar
import time

import feedparser
import requests
import anthropic
from bs4 import BeautifulSoup
from datetime import datetime

# ---------- Налаштування ----------
TG_TOKEN = os.environ["TG_TOKEN"]            # токен від @BotFather
TG_CHANNEL = os.environ["TG_CHANNEL"]        # напр. @my_news_channel
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

HOURS_BACK = 3          # скільки годин назад дивимось новини
MAX_PER_FEED = 15       # скільки новин брати з кожної стрічки
POSTS_PER_RUN = 1       # скільки постів за один запуск

FEEDS = {
    # Україна
    "Суспільне": "https://suspilne.media/rss/all.xml",
    "Українська правда": "https://www.pravda.com.ua/rss/view_news/",
    "Європейська правда": "https://www.eurointegration.com.ua/rss/",
    # Світ
    "BBC": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Guardian": "https://www.theguardian.com/world/rss",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "DW": "https://rss.dw.com/rdf/rss-en-world",
    "France 24": "https://www.france24.com/en/rss",
    "Euronews": "https://www.euronews.com/rss?format=mrss",
    "Sky News": "https://feeds.skynews.com/feeds/rss/world.xml",
    "NPR": "https://feeds.npr.org/1004/rss.xml",
    "Kyiv Independent": "https://kyivindependent.com/news-archive/rss/",
}

# Публічні Telegram-канали, які ти читаєш (тільки назва, без @ і без t.me/)
# Приклад: CHANNELS = ["suspilnenews", "ukrpravda_news"]
CHANNELS = [
    "lvivtruexa",
    "bayranews",
    "Lv1256",
    "zahidwtf_official",
    "uanova",
    "voynareal",
    "exilenova_plus",
    "lviv_realno",
    "UaOnlii",
    "ukrbavovna",
    "absolutely_reliable",
]

client = anthropic.Anthropic()  # бере ANTHROPIC_API_KEY з середовища

# ---------- База, щоб не постити двічі ----------
db = sqlite3.connect("seen.db")
db.execute("CREATE TABLE IF NOT EXISTS seen (link TEXT PRIMARY KEY)")


def is_seen(link):
    return db.execute("SELECT 1 FROM seen WHERE link=?", (link,)).fetchone() is not None


def mark_seen(link):
    db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (link,))
    db.commit()


# ---------- 1. Збір новин ----------
def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def collect_channels(cutoff):
    """Читає публічні канали через веб-версію t.me/s/<назва>."""
    items = []
    for name in CHANNELS:
        try:
            r = requests.get(f"https://t.me/s/{name}", timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"})
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"[!] канал {name}: {e}")
            continue
        for msg in soup.select(".tgme_widget_message")[-MAX_PER_FEED:]:
            text_el = msg.select_one(".tgme_widget_message_text")
            date_el = msg.select_one("a.tgme_widget_message_date")
            time_el = msg.select_one("time")
            if not text_el or not date_el:
                continue
            link = date_el["href"]
            if is_seen(link):
                continue
            if time_el and time_el.get("datetime"):
                ts = datetime.fromisoformat(time_el["datetime"]).timestamp()
                if ts < cutoff:
                    continue
            text = clean(text_el.get_text(" "))
            if len(text) < 40:
                continue
            items.append({
                "source": "@" + name,
                "title": text[:120],
                "summary": text[:500],
                "link": link,
            })
    return items


def collect():
    items = []
    cutoff = time.time() - HOURS_BACK * 3600
    items += collect_channels(cutoff)
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[!] {source}: {e}")
            continue
        for e in feed.entries[:MAX_PER_FEED]:
            link = e.get("link")
            if not link or is_seen(link):
                continue
            t = e.get("published_parsed") or e.get("updated_parsed")
            if t and calendar.timegm(t) < cutoff:
                continue
            items.append({
                "source": source,
                "title": clean(e.get("title")),
                "summary": clean(e.get("summary"))[:400],
                "link": link,
            })
    print(f"Зібрано {len(items)} нових новин")
    return items


# ---------- 2. Claude вибирає найважливіші події ----------
def ask(prompt, max_tokens=1500):
    r = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text.strip()


def pick_stories(items):
    lines = [f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(items)]
    prompt = f"""Ось список свіжих новин з різних джерел (Україна і світ).
Згрупуй їх за подіями. Обери {POSTS_PER_RUN} найважливіших подій: перевага тим,
про які пишуть кілька різних джерел, та тим, що найбільше впливають на людей.
Мікс: хоча б одна українська, якщо є гідна.

Поверни ТІЛЬКИ JSON без пояснень і без ```:
{{"stories": [{{"ids": [номери новин про цю подію]}}]}}

Новини:
""" + "\n".join(lines)
    raw = ask(prompt)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)["stories"]


# ---------- 3. Claude пише пост ----------
def write_post(group):
    material = "\n\n".join(
        f"[{it['source']}] {it['title']}\n{it['summary']}" for it in group
    )
    prompt = f"""Ти редактор українського новинного Telegram-каналу.
На основі матеріалів нижче напиши пост українською. Своїми словами, нічого
не копіюй дослівно.

Формат (без markdown і HTML, звичайний текст):
Перший рядок: емодзі + короткий заголовок.
Далі 2-3 речення: що сталося.
Потім рядок "Чому це важливо:" і 1-2 речення аналізу.
Якщо матеріали з кількох джерел, зведи їх в одну цілісну історію з власною
структурою і формулюваннями, не наслідуй порядок і фрази жодного джерела.
Не вигадуй фактів, яких немає в матеріалах. Якщо джерела розходяться, скажи про це.
Разом не більше 700 символів.

Матеріали:
{material}"""
    return ask(prompt, max_tokens=800)


# ---------- 4. Публікація ----------
def send(text):
    if DRY_RUN:
        print("\n--- ПОСТ (DRY RUN) ---\n" + text + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHANNEL,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def main():
    items = collect()
    if len(items) < 3:
        print("Замало нових новин, виходжу")
        return
    stories = pick_stories(items)
    for s in stories[:POSTS_PER_RUN]:
        group = [items[i] for i in s["ids"] if 0 <= i < len(items)]
        if not group:
            continue
        body = html.escape(write_post(group))
        sources = "\n".join(
            f'• <a href="{html.escape(it["link"])}">{html.escape(it["source"])}</a>'
            for it in group[:4]
        )
        send(f"{body}\n\n<b>Джерела:</b>\n{sources}")
        for it in group:
            mark_seen(it["link"])
        time.sleep(3)


if __name__ == "__main__":
    main()
