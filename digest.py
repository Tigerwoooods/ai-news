#!/usr/bin/env python3
"""AI Daily Digest — собирает новости AI-компаний и шлёт саммари в Telegram."""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

HOURS_WINDOW = 26          # окно свежести
SEEN_FILE = Path(__file__).parent / "seen.json"
SEEN_TTL_DAYS = 7
MAX_ARTICLE_CHARS = 1500   # сколько текста статьи отдаём в LLM
MODEL = "claude-haiku-4-5"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

RSS_SOURCES = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("Microsoft AI", "https://blogs.microsoft.com/ai/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
]

# сайты без RSS: (имя, страница списка, regex ссылок на статьи)
HTML_SOURCES = [
    ("Anthropic", "https://www.anthropic.com/news", r"^/news/[\w-]+$"),
    ("DeepMind", "https://deepmind.google/discover/blog/", r"/discover/blog/[\w-]+/?$"),
    ("Meta AI", "https://ai.meta.com/blog/", r"^/blog/[\w-]+/?$"),
    ("Mistral", "https://mistral.ai/news/", r"^/news/[\w-]+/?$"),
    ("xAI", "https://x.ai/news", r"^/news/[\w-]+$"),
]

MAX_LINKS_PER_HTML_SOURCE = 8

# ---------------------------------------------------------------- helpers


def log(msg: str) -> None:
    print(f"[digest] {msg}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    cutoff = now_utc() - timedelta(days=SEEN_TTL_DAYS)
    pruned = {
        url: ts
        for url, ts in seen.items()
        if datetime.fromisoformat(ts) > cutoff
    }
    SEEN_FILE.write_text(json.dumps(pruned, indent=1, ensure_ascii=False))


def fetch(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        r = client.get(url, headers=HEADERS, follow_redirects=True, timeout=20)
        r.raise_for_status()
        return r
    except Exception as e:
        log(f"fetch failed {url}: {e}")
        return None


def parse_article_page(client: httpx.Client, url: str) -> dict:
    """Достаёт из страницы статьи дату, og:image и текст."""
    out = {"published": None, "image": None, "text": ""}
    r = fetch(client, url)
    if r is None:
        return out
    soup = BeautifulSoup(r.text, "html.parser")

    for prop in ("article:published_time", "og:article:published_time"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        if tag and tag.get("content"):
            try:
                out["published"] = datetime.fromisoformat(
                    tag["content"].replace("Z", "+00:00")
                )
                break
            except ValueError:
                pass
    if out["published"] is None:
        tag = soup.find("time")
        if tag and tag.get("datetime"):
            try:
                out["published"] = datetime.fromisoformat(
                    tag["datetime"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content", "").startswith("http"):
        out["image"] = og["content"]

    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    out["text"] = " ".join(paragraphs)[:MAX_ARTICLE_CHARS]
    return out


# ---------------------------------------------------------------- collectors


def collect_rss(client: httpx.Client, cutoff: datetime, seen: dict) -> list[dict]:
    articles = []
    for name, feed_url in RSS_SOURCES:
        try:
            r = fetch(client, feed_url)
            if r is None:
                continue
            feed = feedparser.parse(r.text)
            for entry in feed.entries[:20]:
                url = entry.get("link", "")
                if not url or url in seen:
                    continue
                published = None
                for key in ("published_parsed", "updated_parsed"):
                    if entry.get(key):
                        published = datetime(*entry[key][:6], tzinfo=timezone.utc)
                        break
                if published is None or published < cutoff:
                    continue
                summary_html = entry.get("summary", "") or ""
                text = BeautifulSoup(summary_html, "html.parser").get_text(
                    " ", strip=True
                )[:MAX_ARTICLE_CHARS]
                image = None
                for media in entry.get("media_content", []) or []:
                    if media.get("url", "").startswith("http"):
                        image = media["url"]
                        break
                articles.append(
                    {
                        "source": name,
                        "title": entry.get("title", "").strip(),
                        "url": url,
                        "published": published.isoformat(),
                        "text": text,
                        "image": image,
                    }
                )
            log(f"{name}: ok")
        except Exception as e:
            log(f"{name}: source failed: {e}")
    return articles


def collect_html(client: httpx.Client, cutoff: datetime, seen: dict) -> list[dict]:
    articles = []
    for name, page_url, link_re in HTML_SOURCES:
        try:
            r = fetch(client, page_url)
            if r is None:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            base = re.match(r"https?://[^/]+", page_url).group(0)
            links, seen_links = [], set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(link_re, href):
                    full = href if href.startswith("http") else base + href
                    if full not in seen_links and full != page_url.rstrip("/"):
                        seen_links.add(full)
                        links.append((full, a.get_text(" ", strip=True)))
            for url, link_title in links[:MAX_LINKS_PER_HTML_SOURCE]:
                if url in seen:
                    continue
                meta = parse_article_page(client, url)
                published = meta["published"]
                # если дату не нашли — статью пропускаем (иначе будут дубли старья)
                if published is None or published < cutoff:
                    continue
                title = link_title
                if not title or len(title) < 10:
                    title = url.rstrip("/").split("/")[-1].replace("-", " ").title()
                articles.append(
                    {
                        "source": name,
                        "title": title[:200],
                        "url": url,
                        "published": published.isoformat(),
                        "text": meta["text"],
                        "image": meta["image"],
                    }
                )
            log(f"{name}: ok")
        except Exception as e:
            log(f"{name}: source failed: {e}")
    return articles


# ---------------------------------------------------------------- LLM


def summarize(articles: list[dict]) -> dict:
    """Просит Claude выбрать главное и написать русские саммари. Возвращает dict."""
    import anthropic

    payload = [
        {
            "id": i,
            "source": a["source"],
            "title": a["title"],
            "url": a["url"],
            "text": a["text"][:MAX_ARTICLE_CHARS],
        }
        for i, a in enumerate(articles)
    ]

    prompt = f"""Ты редактор ежедневного дайджеста новостей об AI для занятого читателя.
Ниже JSON со статьями за последние сутки.

Выбери 3-7 САМЫХ важных новостей (релизы моделей, крупные анонсы, деньги, регулирование).
Мелочь и маркетинговый шум отправь в "briefly" (максимум 5 штук) или выброси совсем.
Дубли одной новости из разных источников объедини в одну (выбери первоисточник).

Для каждой главной новости напиши на русском:
- title: короткий цепкий заголовок
- summary: 2-4 предложения — что произошло и почему это важно

Ответь СТРОГО валидным JSON без markdown-обёртки:
{{"top": [{{"id": <id статьи>, "title": "...", "summary": "..."}}],
 "briefly": [{{"id": <id>, "title": "..."}}]}}

Статьи:
{json.dumps(payload, ensure_ascii=False)}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    for attempt in range(2):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
            assert isinstance(data.get("top"), list)
            return data
        except (json.JSONDecodeError, AssertionError) as e:
            log(f"LLM JSON parse failed (attempt {attempt + 1}): {e}")
    raise RuntimeError("LLM did not return valid JSON")


# ---------------------------------------------------------------- telegram

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_call(client: httpx.Client, method: str, **params) -> bool:
    try:
        r = client.post(f"{TG_API}/{method}", data=params, timeout=30)
        ok = r.json().get("ok", False)
        if not ok:
            log(f"telegram {method} error: {r.text[:300]}")
        return ok
    except Exception as e:
        log(f"telegram {method} failed: {e}")
        return False


def send_news_item(client: httpx.Client, item: dict, article: dict) -> None:
    caption = (
        f"<b>{esc(item['title'])}</b>\n\n"
        f"{esc(item['summary'])}\n\n"
        f"<a href=\"{article['url']}\">{esc(article['source'])} →</a>"
    )
    sent = False
    if article.get("image") and len(caption) <= 1024:
        sent = tg_call(
            client,
            "sendPhoto",
            chat_id=TELEGRAM_CHAT_ID,
            photo=article["image"],
            caption=caption,
            parse_mode="HTML",
        )
    if not sent:
        tg_call(
            client,
            "sendMessage",
            chat_id=TELEGRAM_CHAT_ID,
            text=caption[:4096],
            parse_mode="HTML",
            disable_web_page_preview="false",
        )


# ---------------------------------------------------------------- main


def main() -> None:
    cutoff = now_utc() - timedelta(hours=HOURS_WINDOW)
    seen = load_seen()

    with httpx.Client() as client:
        articles = collect_rss(client, cutoff, seen) + collect_html(
            client, cutoff, seen
        )
        log(f"collected {len(articles)} fresh articles")

        today = now_utc().strftime("%d.%m.%Y")

        if not articles:
            tg_call(
                client,
                "sendMessage",
                chat_id=TELEGRAM_CHAT_ID,
                text=f"🤖 AI Digest — {today}\n\nСегодня без больших новостей 🤷",
            )
            return

        digest = summarize(articles)

        tg_call(
            client,
            "sendMessage",
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 <b>AI Digest — {today}</b>",
            parse_mode="HTML",
        )

        for item in digest.get("top", []):
            article = articles[item["id"]] if 0 <= item.get("id", -1) < len(articles) else None
            if article is None:
                continue
            send_news_item(client, item, article)
            seen[article["url"]] = now_utc().isoformat()

        briefly = digest.get("briefly", [])
        if briefly:
            lines = ["📎 <b>Ещё коротко:</b>"]
            for item in briefly:
                idx = item.get("id", -1)
                if 0 <= idx < len(articles):
                    a = articles[idx]
                    lines.append(f"• <a href=\"{a['url']}\">{esc(item['title'])}</a>")
                    seen[a["url"]] = now_utc().isoformat()
            tg_call(
                client,
                "sendMessage",
                chat_id=TELEGRAM_CHAT_ID,
                text="\n".join(lines)[:4096],
                parse_mode="HTML",
                disable_web_page_preview="true",
            )

        # всё собранное, но не попавшее в дайджест, тоже помечаем виденным
        for a in articles:
            seen.setdefault(a["url"], now_utc().isoformat())

    save_seen(seen)
    log("done")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
