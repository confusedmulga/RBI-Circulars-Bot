"""
RBI Circulars Bot.

Each run reads RBI's Notifications and Press Releases RSS feeds, queues anything
not seen before, and once a day (first run at or after DIGEST_HOUR_IST) posts
the queue to Telegram as a single digest.

State lives in STATE_FILE (committed back by the GitHub workflow):
  seen              feed key -> recent item IDs, used to skip already-queued items
  pending           items waiting for the next digest
  notes             warnings to include in the next digest
  last_digest_date  IST date of the last scheduled digest
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# CONFIG
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE         = os.getenv("STATE_FILE", "state.json")
# DRY_RUN=1 prints the digest instead of sending it and leaves the state file untouched
DRY_RUN            = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# SEND_NOW=true sends whatever is queued without waiting for the digest hour
SEND_NOW           = os.getenv("SEND_NOW", "").strip().lower() in ("1", "true", "yes")

DIGEST_HOUR_IST    = 9
IST                = timezone(timedelta(hours=5, minutes=30))

FEEDS = [
    {"key": "notifications",  "heading": "Notifications",
     "url": "https://www.rbi.org.in/notifications_rss.xml",  "id_param": "id"},
    {"key": "press_releases", "heading": "Press releases",
     "url": "https://www.rbi.org.in/pressreleases_rss.xml", "id_param": "prid"},
]

# Press releases whose titles match any of these are listed as bare links under
# "Market operations" instead of getting a summary. Edit to taste.
ROUTINE_TITLE_PATTERNS = [
    r"\bauction",
    r"money market operations",
    r"\breserve money\b",
    r"\bmoney supply\b",
    r"liabilities and assets",
    r"ways and means advances",
    r"\bbuy ?back\b",
    r"\bswitch\b.*\bsecurities\b",
    r"statistical supplement",
    r"^sale \((?:re-)?issue\)",
    r"foreign exchange turnover",
    r"statement of position",
    r"\b(?:OMO|open market operations?)\b.*\b(?:cut-?offs?|results?)\b",
]
ROUTINE_RE = re.compile("|".join(ROUTINE_TITLE_PATTERNS), re.IGNORECASE)

HEADERS            = {"User-Agent": "Mozilla/5.0"}
SEEN_LIMIT         = 500    # IDs remembered per feed; each feed only shows the latest 10
TITLE_MAX          = 300
SUMMARY_MIN        = 160    # keep adding paragraphs until the summary is at least this long
SUMMARY_MAX        = 420
MESSAGE_LIMIT      = 3900   # Telegram rejects messages over 4096 characters


class TelegramError(Exception):
    pass


# STATE

def load_state() -> dict:
    state = {"seen": {}, "pending": [], "notes": [], "last_digest_date": ""}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_state(state: dict):
    if DRY_RUN:
        return
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# FEED PARSING

def fetch_feed(url: str) -> list:
    """Fetch and parse an RSS feed, retrying transient failures."""
    error = None
    for attempt in range(3):
        if attempt:
            time.sleep(10 * attempt)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            if feed.entries:
                return feed.entries
            error = "feed has no entries"
        except requests.RequestException as e:
            error = e
    raise RuntimeError(f"{url}: {error}")


def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def item_id(link: str, id_param: str) -> str:
    """The numeric ID in the item's link (prid=… / Id=…), falling back to the link itself."""
    query = parse_qs(urlparse(link).query)
    for key, values in query.items():
        if key.lower() == id_param and values and values[0].strip():
            return values[0].strip()
    return link.strip().lower()


def published_key(entry) -> str:
    parsed = entry.get("published_parsed")
    return time.strftime("%Y-%m-%d %H:%M", parsed) if parsed else ""


# SUMMARIES
# RBI's RSS descriptions carry the full text as HTML <p> paragraphs. A notification
# opens with reference numbers, date, addressee, salutation and its own title; a press
# release opens straight into the body and ends with a signature block. The summary is
# the first body paragraph(s), trimmed at a sentence boundary.

SENTENCE_PUNCT_RE = re.compile(r"[.:;?!](?:\s|$)")
REFERENCE_RE      = re.compile(r"^(?:RBI/\d|Ref\.?\s*No)", re.IGNORECASE)
SENTENCE_END_RE   = re.compile(r"[.!?]")
NUMBERING_RE      = re.compile(r"^\d+(?:\.\d+)*\.?\s+(?=[A-Z(])")
# Operative paragraphs of Directions ("…hereby issues the Directions hereinafter specified",
# "shall be called…", "shall come into effect…"): kept out of the summary
BOILERPLATE_RE    = re.compile(
    r"hereinafter specified|shall be called|come into (?:effect|force)|shall be effective from"
    r"|are (?:hereby )?amended as (?:provided|given) below",
    re.IGNORECASE,
)
PROVISO_RE        = re.compile(r"provided (?:further |also )?that\b", re.IGNORECASE)
TRAILER_RE        = re.compile(r"^(?:yours (?:faithfully|sincerely)|press release\s*:|encl)", re.IGNORECASE)
SIGNATURE_RE      = re.compile(r"^\([^()]{2,60}\)$")
# "shall come into effect from April 1, 2027", "will take effect from …", "shall come into force
# with immediate effect", "shall be effective from …". A dated phrase stops at the first year.
EFFECTIVE_RE      = re.compile(
    r"(?:shall|will)\s+(?:come\s+into\s+(?:effect|force)|be\s+effective|take\s+effect)\s+"
    r"(with immediate effect|with effect immediately|immediately"
    r"|(?:from|on|with effect from)\s(?:[^.;]{0,80}?\b\d{4}(?:-\d{2,4})?\b|[^.;,]{1,80}))",
    re.IGNORECASE,
)
DOTTED_ABBREV_RE  = re.compile(r"(?:[a-z]{1,5}\.)+[a-z]{1,5}")   # A.P., S.O., CO.DOS.SED., F.No.
ABBREVIATIONS = {
    "no", "nos", "rs", "ltd", "co", "pvt", "viz", "i.e", "e.g", "sr", "dr", "mr", "ms",
    "mrs", "prof", "shri", "smt", "govt", "dept", "sl", "dt", "st", "vs", "para", "paras", "ref",
    "cf", "approx", "inc", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
}


def body_paragraphs(description: str, title: str) -> list:
    """
    (text, is list item) for the body paragraphs of an RSS description, without
    header, tables or signature.
    """
    soup = BeautifulSoup(description, "html.parser")
    for tag in soup.find_all(["sup", "script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(" ")
    # The whole description is wrapped in one layout table; any table inside it is data.
    # It is replaced by a marker so a summary ending "as under:" stops there.
    for table in soup.find_all("table"):
        if table.find_parent("table"):
            table.replace_with(soup.new_tag("p", attrs={"data-table": "1"}))

    blocks = [b for b in soup.find_all(["p", "li"]) if not (b.name == "li" and b.find("p"))]
    if not blocks:
        text = clean_space(soup.get_text(" "))
        return [(text, False)] if text else []

    norm_title = clean_space(title).lower()
    alnum_title = re.sub(r"[^a-z0-9]", "", norm_title)
    paragraphs = []
    for n, block in enumerate(blocks):
        if block.get("data-table"):
            if paragraphs:
                paragraphs.append(("", False))
            continue
        text = clean_space(block.get_text())
        if not text:
            continue
        # Everything before a notification's own title is header (addressee, salutation…)
        if n < 10 and alnum_title and re.sub(r"[^a-z0-9]", "", text.lower()) == alnum_title:
            paragraphs = []
            continue
        align = (block.get("align") or "").lower()
        head = block.find(class_="head")
        is_heading = ("head" in (block.get("class") or [])
                      or (head is not None and clean_space(head.get_text()) == text))
        if not paragraphs:
            # Header: reference numbers, date, addressee, salutation, title
            if (align in ("right", "center") or is_heading or len(text) < 40
                    or not SENTENCE_PUNCT_RE.search(text) or REFERENCE_RE.match(text)
                    or text.lower() == norm_title):
                continue
        else:
            if (align == "right" or TRAILER_RE.match(text) or SIGNATURE_RE.match(text)
                    or ("General Manager" in text and len(text) < 120)):
                break
            if is_heading:
                continue
        in_list = block.name == "li" or block.find_parent("li") is not None
        paragraphs.append((NUMBERING_RE.sub("", text), in_list))
    return paragraphs


def is_abbreviation(text_before: str) -> bool:
    words = text_before.split()
    if not words:
        return False
    word = words[-1].lstrip("(\"'“‘").lower()
    return (word in ABBREVIATIONS or (len(word) == 1 and word.isalpha())
            or DOTTED_ABBREV_RE.fullmatch(word) is not None)


def shorten(text: str, limit: int) -> str:
    """Cut text to at most `limit` characters, at a sentence end if one is reasonably close."""
    if len(text) <= limit:
        return text
    best = None
    for m in SENTENCE_END_RE.finditer(text, 0, limit):
        end = m.end()
        if end < len(text) and not text[end].isspace():
            continue  # "4.1", "Rs.5", ".pdf"
        if not is_abbreviation(text[:m.start()]):
            best = end
    if best and best >= limit * 0.4:
        return text[:best]
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:–-") + "…"


def summarize(description: str, title: str) -> tuple:
    """(summary, effective-date note) for an RSS description; either may be empty."""
    paragraphs = body_paragraphs(description, title)
    summary, prev_in_list = "", False
    for text, in_list in paragraphs:
        if not text:   # a data table was here
            if summary.endswith(":"):
                break
            continue
        if BOILERPLATE_RE.search(text) or (not summary and PROVISO_RE.match(text)):
            continue
        sep = "; " if in_list and prev_in_list else " "
        summary = f"{summary}{sep}{text}" if summary else text
        prev_in_list = in_list
        if len(summary) >= SUMMARY_MIN and not summary.endswith(":"):
            break
    summary = shorten(summary, SUMMARY_MAX)

    effective = ""
    m = EFFECTIVE_RE.search(" ".join(text for text, _ in paragraphs))
    if m:
        phrase = re.sub(r"^on\s+(?=\d|[A-Z][a-z]+ \d)", "", clean_space(m.group(1)), flags=re.IGNORECASE)
        effective = "Effective immediately" if "immediate" in phrase.lower() else f"Effective {phrase}"
    return summary, effective


# COLLECTION

def collect_new(state: dict, feed: dict, entries: list) -> int:
    """Queue entries not seen before. Returns the number queued."""
    key = feed["key"]
    seen = state["seen"].setdefault(key, [])
    seen_set = set(seen)

    new_items = []
    for entry in entries:
        link = entry.get("link", "").strip()
        title = clean_space(html.unescape(entry.get("title", "")))
        if not link or not title:
            continue
        iid = item_id(link, feed["id_param"])
        if iid in seen_set:
            continue
        seen_set.add(iid)

        routine = key == "press_releases" and bool(ROUTINE_RE.search(title))
        summary, effective = ("", "") if routine else summarize(entry.get("summary", ""), title)
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rstrip() + "…"
        new_items.append({
            "feed": key, "id": iid, "title": title, "link": link,
            "published": published_key(entry), "routine": routine,
            "summary": summary, "effective": effective,
        })

    # Nothing in the feed was seen before, so more items may have been published since
    # the last run than the feed holds.
    if seen and new_items and len(new_items) == len(entries):
        note = (f"The {feed['heading'].lower()} feed only shows the latest {len(entries)} items and all "
                f"of them were new, so some earlier ones may be missing. Full list: https://www.rbi.org.in")
        if note not in state["notes"]:
            state["notes"].append(note)

    seen.extend(item["id"] for item in new_items)
    del seen[:-SEEN_LIMIT]
    state["pending"].extend(new_items)
    return len(new_items)


# DIGEST
# Telegram's HTML mode only knows &lt; &gt; &amp; &quot;, so apostrophes are left as they are.

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def esc_attr(text: str) -> str:
    return html.escape(text, quote=False).replace('"', "&quot;")


def render_item(number: int, item: dict) -> str:
    lines = [f'{number}. <a href="{esc_attr(item["link"])}">{esc(item["title"])}</a>']
    if item.get("effective"):
        lines.append(f"<i>{esc(item['effective'])}</i>")
    if item.get("summary"):
        lines.append(f"<blockquote expandable>{esc(item['summary'])}</blockquote>")
    return "\n".join(lines)


def render_routine(item: dict) -> str:
    return f'• <a href="{esc_attr(item["link"])}">{esc(item["title"])}</a>'


def build_blocks(pending: list, notes: list) -> list:
    """
    Digest body as blocks that are never split across messages. Each block is
    (separator before it, html, pending item keys, notes, heading to use if it opens a message).
    """
    def order(item):
        num = int(item["id"]) if item["id"].isdigit() else 0
        return (item["published"], num)

    sections = [
        (feed["heading"], [i for i in pending if i["feed"] == feed["key"] and not i.get("routine")])
        for feed in FEEDS
    ]
    known = {feed["key"] for feed in FEEDS}
    routine = [i for i in pending if i.get("routine") or i["feed"] not in known]

    blocks = []
    for heading, items in sections:
        for n, item in enumerate(sorted(items, key=order), 1):
            text = render_item(n, item)
            cont = f"<b>{esc(heading)} (contd.)</b>\n"
            if n == 1:
                text, cont = f"<b>{esc(heading)}</b>\n{text}", ""
            blocks.append(("\n\n", text, [(item["feed"], item["id"])], [], cont))
    for n, item in enumerate(sorted(routine, key=order)):
        text, sep, cont = render_routine(item), "\n", "<b>Market operations (contd.)</b>\n"
        if n == 0:
            text, sep, cont = f"<b>Market operations</b>\n{text}", "\n\n", ""
        blocks.append((sep, text, [(item["feed"], item["id"])], [], cont))
    for note in notes:
        blocks.append(("\n\n", f"<i>{esc(note)}</i>", [], [note], ""))
    return blocks


def build_messages(pending: list, notes: list, now: datetime) -> list:
    """Split the digest into messages under MESSAGE_LIMIT. Returns [(html, item keys, notes)]."""
    count = len(pending)
    title = f"RBI updates · {now.strftime('%d %b %Y')} · {count} new"
    header_room = len(title) + 80   # "<b>…</b> (2/3)\n\n" and a "(contd.)" section heading

    chunks = []
    current, keys, chunk_notes = "", [], []
    for sep, text, item_keys, block_notes, cont in build_blocks(pending, notes):
        if current and len(current) + len(sep) + len(text) + header_room > MESSAGE_LIMIT:
            chunks.append((current, keys, chunk_notes))
            current, keys, chunk_notes = "", [], []
        current = f"{current}{sep}{text}" if current else cont + text
        keys += item_keys
        chunk_notes += block_notes
    if current:
        chunks.append((current, keys, chunk_notes))

    messages = []
    for i, (body, item_keys, chunk_notes) in enumerate(chunks, 1):
        part = f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""
        messages.append((f"<b>{esc(title)}</b>{part}\n\n{body}", item_keys, chunk_notes))
    return messages


def html_to_plain(text: str) -> str:
    """Plain-text version of a digest message, keeping link URLs."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2\n\1", text, flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_message(text: str):
    """
    Send one HTML message, retrying rate limits and server errors. If Telegram can't
    parse the HTML, the message is resent as plain text. Raises TelegramError.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    error = ""
    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            # Exception text includes the URL, which contains the token
            error = str(e).replace(TELEGRAM_BOT_TOKEN, "<token>") if TELEGRAM_BOT_TOKEN else str(e)
            time.sleep(5 * (attempt + 1))
            continue
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.ok and data.get("ok"):
            return
        error = f"HTTP {r.status_code}: {data.get('description') or r.text[:200]}"
        if r.status_code == 429:
            time.sleep(int(data.get("parameters", {}).get("retry_after", 5)) + 1)
        elif r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
        elif "parse_mode" in payload and "parse entities" in error:
            print(f"[WARN] Telegram could not parse the HTML ({error}); sending as plain text.")
            payload["text"] = html_to_plain(text)
            del payload["parse_mode"]
        else:
            break   # other 4xx: retrying will not help
    raise TelegramError(error)


def send_digest(state: dict, now: datetime):
    """Send all pending items. Sent items are removed from the state as each message succeeds."""
    messages = build_messages(state["pending"], state["notes"], now)
    for i, (text, item_keys, sent_notes) in enumerate(messages):
        if i:
            time.sleep(1)
        if DRY_RUN:
            print(f"----- message {i + 1}/{len(messages)} ({len(text)} chars) -----\n{text}\n", flush=True)
        else:
            send_message(text)
        sent = set(item_keys)
        state["pending"] = [p for p in state["pending"] if (p["feed"], p["id"]) not in sent]
        state["notes"] = [n for n in state["notes"] if n not in sent_notes]
        save_state(state)


def main() -> int:
    if not DRY_RUN and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        print("[ERROR] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use DRY_RUN=1).")
        return 1

    state = load_state()
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    failed = False

    for feed in FEEDS:
        try:
            entries = fetch_feed(feed["url"])
        except Exception as e:
            print(f"[ERROR] {feed['key']}: {e}")
            failed = True
            continue
        added = collect_new(state, feed, entries)
        print(f"[INFO] {feed['key']}: {len(entries)} in feed, {added} new")
    save_state(state)

    scheduled = now.hour >= DIGEST_HOUR_IST and state["last_digest_date"] != today
    print(f"[INFO] {len(state['pending'])} pending; digest due: {scheduled or SEND_NOW}")
    if scheduled or SEND_NOW:
        if state["pending"] or state["notes"]:
            try:
                send_digest(state, now)
            except TelegramError as e:
                print(f"[ERROR] Telegram: {e}. Unsent items stay queued for the next run.")
                return 1
            print("[INFO] Digest sent.")
        if scheduled:
            state["last_digest_date"] = today
            save_state(state)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
