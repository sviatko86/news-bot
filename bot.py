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
REQUIRE_PHOTO = True   # True = постити лише новини з фото
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


def rss_image(e):
    """Шукає картинку в RSS-записі."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []) or []:
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    raw = e.get("summary", "") or ""
    for c in e.get("content", []) or []:
        raw += c.get("value", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', raw)
    return m.group(1) if m else None


def og_image(url):
    """Бере головну картинку зі сторінки новини (og:image)."""
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for prop in ("og:image", "twitter:image"):
            tag = soup.find("meta", attrs={"property": prop}) or \
                  soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content", "").startswith("http"):
                return tag["content"]
    except Exception as e:
        print(f"[!] og:image {url}: {e}")
    return None


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
            image = None
            photo = msg.select_one(".tgme_widget_message_photo_wrap")
            if photo and photo.get("style"):
                mm = re.search(r"url\('([^']+)'\)", photo["style"])
                image = mm.group(1) if mm else None
            text = clean(text_el.get_text(" "))
            if len(text) < 40:
                continue
            items.append({
                "source": "@" + name,
                "title": text[:120],
                "summary": text[:500],
                "link": link,
                "image": image,
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
                "image": rss_image(e),
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
    return "".join(b.text for b in r.content if b.type == "text").strip()


def pick_stories(items):
    lines = [f"{i}. {'📷 ' if it.get('image') else ''}[{it['source']}] {it['title']}" for i, it in enumerate(items)]
    n = POSTS_PER_RUN * 4
    prompt = f"""Ось список свіжих новин з різних джерел (Україна і світ).
Згрупуй їх за подіями. Обери до {n} найважливіших подій і відсортуй від
найважливішої. Перевага подіям, про які пишуть кілька різних джерел і про які
є достатньо конкретики (хто, що, де, цифри).

Події з позначкою 📷 (є фото) мають невеликий пріоритет, але головне важливість.

ПРОПУСКАЙ: повітряні тривоги і «шахеди в напрямку», локальну погоду, рекламу,
промокоди, вакансії, розіграші, плітки про знаменитостей, голі заголовки без
подробиць, особисті думки блогерів.

Поверни ТІЛЬКИ JSON без пояснень і без ```:
{{"stories": [{{"ids": [номери новин про цю подію]}}]}}

Новини:
""" + "\n".join(lines)
    raw = ask(prompt, max_tokens=2000)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)["stories"]


# ---------- 3. Claude пише пост ----------
def write_post(group):
    material = "\n\n".join(
        f"[{it['source']}] {it['title']}\n{it['summary']}" for it in group
    )
    prompt = f"""Ти редактор українського новинного Telegram-каналу.
На основі матеріалів нижче напиши пост українською. Пиши своїми словами,
нічого не копіюй дослівно. Текст має бути зрозумілий людині, яка нічого не
знає про контекст.

Формат (звичайний текст, без markdown і HTML), рівно такий:
Рядок 1: емодзі + чіткий заголовок (що саме сталося, не загальні слова).
Порожній рядок.
Далі 2-4 речення: що сталося, де, коли, хто, цифри.
Порожній рядок.
"Чому це важливо:" і 1-2 речення аналізу.

Правила:
- Зведи джерела в одну цілісну історію з власною структурою.
- Не вигадуй фактів, яких немає в матеріалах. Якщо джерела розходяться, скажи про це.
- Довжина 400-800 символів.
- Якщо матеріалів замало для зрозумілого посту (лише заголовок без подробиць),
  відповідай одним словом: SKIP

Матеріали:
{material}"""
    return ask(prompt, max_tokens=900)


def is_good(text):
    return text.strip() != "SKIP" and len(text) >= 300 and "Чому це важливо" in text


# ---------- 4. Публікація ----------
def send(text, image=None, plain_len=0):
    """Повертає True, якщо пост опубліковано."""
    if DRY_RUN:
        print("\n--- ПОСТ (DRY RUN) ---\n" + text + "\nФОТО: " + str(image) + "\n")
        return True
    api = f"https://api.telegram.org/bot{TG_TOKEN}/"
    if image:
        try:
            if plain_len <= 1000:   # підпис до фото максимум 1024 символи
                r = requests.post(api + "sendPhoto", json={
                    "chat_id": TG_CHANNEL, "photo": image,
                    "caption": text, "parse_mode": "HTML"}, timeout=30)
                r.raise_for_status()
                return True
            r = requests.post(api + "sendPhoto", json={
                "chat_id": TG_CHANNEL, "photo": image}, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"[!] фото не вдалось ({e})")
            if REQUIRE_PHOTO:
                return False
    elif REQUIRE_PHOTO:
        return False
    r = requests.post(api + "sendMessage", json={
        "chat_id": TG_CHANNEL, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()
    return True


def main():
    items = collect()
    if len(items) < 3:
        print("Замало нових новин, виходжу")
        return
    stories = pick_stories(items)
    posted = 0
    for s in stories:
        if posted >= POSTS_PER_RUN:
            break
        group = [items[i] for i in s["ids"] if 0 <= i < len(items)]
        if not group:
            continue
        image = next((it["image"] for it in group if it.get("image")), None)
        if not image:
            for it in group[:4]:
                image = og_image(it["link"])
                if image:
                    break
        print("Фото:", image)
        if REQUIRE_PHOTO and not image:
            print("Пропускаю подію: немає фото")
            continue
        text = write_post(group)
        if not is_good(text):
            print("Пропускаю подію: замало подробиць")
            for it in group:
                mark_seen(it["link"])
            continue
        # унікальні джерела, по одному посиланню на джерело
        uniq = {}
        for it in group:
            uniq.setdefault(it["source"], it["link"])
        sources = "\n".join(
            f'• <a href="{html.escape(link)}">{html.escape(name)}</a>'
            for name, link in list(uniq.items())[:5]
        )
        plain_len = len(text) + 12 + sum(len(n) + 3 for n in list(uniq)[:5])
        ok = send(f"{html.escape(text)}\n\n<b>Джерела:</b>\n{sources}", image, plain_len)
        if not ok:
            print("Пропускаю подію: фото не прийнято Telegram")
            continue
        for it in group:
            mark_seen(it["link"])
        posted += 1
        time.sleep(3)
    if posted == 0:
        print("Нічого гідного для публікації")


if __name__ == "__main__":
    main()
