import os
import time
import requests
import feedparser
from bs4 import BeautifulSoup
from transformers import pipeline, logging as hf_logging
import textwrap

# CONFIG
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ADD TOKEN HERE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "ADD TELEGRAM GROUP ID HERE")

RSS_FEED           = "https://www.rbi.org.in/pressreleases_rss.xml"
HEADERS            = {"User-Agent": "Mozilla/5.0"}
LAST_TITLE_FILE    = "last_title.txt"

# LOCAL SUMMARIZER HEREEEEEE
hf_logging.set_verbosity_error()
summarizer = pipeline(
    "summarization",
    model="t5-small",
    tokenizer="t5-small",
    device=-1
)

def summarize_text(text: str) -> str:
    """
    Summarize `text` into a string of ≤200 characters.
    Maximum chunk size is ~2000 characters to stay within T5-small's 512-token limit.
    """
    t = text.strip()
    if not t:
        return "(No content.)"

    if len(t) > 2000:
        t = t[:2000] + " …"

    try:
        out = summarizer(
            t,
            max_length=100,
            min_length=50,
            do_sample=False
        )[0]["summary_text"].strip()
    except Exception as e:
        print(f"[ERROR] Summarization failed: {e}")
        out = t[:200] + "…"

    if len(out) > 200:
        return out[:197].rstrip() + "…"
    return out

def send_message(text: str):
    """
    Split text into ≤4000-character chunks and send each to Telegram.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in textwrap.wrap(text, 4000, replace_whitespace=False):
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}
        )
        if not resp.ok:
            print("[TELEGRAM ERROR]", resp.status_code, resp.text)
        time.sleep(0.5)

def fetch_full_text(url: str) -> str:
    """
    Scrape the circular page for its main body:
      1) Look for <span id="ctl00_Content_Main_lblRelease">
      2) Fallback: <div id="ctl00_Content_Main_panelPressRelease"> <p> tags
      3) Otherwise, return the entire page text
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
    except Exception as e:
        return f"(Fetch failed: {e})"
    if r.status_code != 200:
        return f"(HTTP {r.status_code} fetching.)"

    soup = BeautifulSoup(r.content, "html.parser")
    span = soup.find("span", id="ctl00_Content_Main_lblRelease")
    if span:
        return span.get_text(separator="\n", strip=True)

    panel = soup.find("div", id="ctl00_Content_Main_panelPressRelease")
    if panel:
        paras = panel.find_all("p")
        filtered = [p.get_text(strip=True) for p in paras if len(p.get_text(strip=True)) > 30]
        return "\n\n".join(filtered).strip()

    return soup.get_text(separator="\n", strip=True)

def get_last_title() -> str:
    if os.path.exists(LAST_TITLE_FILE):
        with open(LAST_TITLE_FILE, "r") as f:
            return f.read().strip()
    return ""

def set_last_title(title: str):
    with open(LAST_TITLE_FILE, "w") as f:
        f.write(title)

def fetch_and_send():
    print("[DEBUG] Fetching RSS feed…")
    try:
        resp = requests.get(RSS_FEED, headers=HEADERS, timeout=10)
    except Exception as e:
        print(f"[ERROR] RSS fetch failed: {e}")
        return

    print(f"[DEBUG] RSS HTTP status → {resp.status_code}")
    if resp.status_code != 200:
        return

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        print("[DEBUG] No entries in RSS feed.")
        return

    last_title = get_last_title()
    print(f"[DEBUG] Last sent title → {repr(last_title)}")

    new_entries = []
    for entry in feed.entries:
        if entry.title == last_title:
            break
        new_entries.append(entry)

    if not new_entries:
        print("[DEBUG] No new circulars to send.")
        return

    for entry in reversed(new_entries):
        print(f"[DEBUG] Processing → {entry.title}")
        send_message(f"<b>{entry.title}</b>")
        full_text = fetch_full_text(entry.link)
        summary  = summarize_text(full_text)
        send_message(f"{summary}\n\n🔗 <a href=\"{entry.link}\">Read full circular</a>")

    set_last_title(new_entries[0].title)
    print("[DEBUG] Updated last_title; done.")

if __name__ == "__main__":
    fetch_and_send()
