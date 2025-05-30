import requests
import feedparser
from bs4 import BeautifulSoup
from huggingface_hub import InferenceClient
import os
import textwrap
import time

# ── CONFIG ───────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
HUGGINGFACE_TOKEN  = os.environ["HUGGINGFACE_TOKEN"]

RSS_FEED           = "https://www.rbi.org.in/pressreleases_rss.xml"
HEADERS            = {"User-Agent": "Mozilla/5.0"}

LAST_TITLE_FILE    = "last_title.txt"

# ── SETUP HUGGING FACE CLIENT ─────────────────────────────────────────
client = InferenceClient(token=HUGGINGFACE_TOKEN)

def summarize(text: str, max_length: int = 200) -> str:
    """Use HF inference API to summarize a chunk of text."""
    try:
        # some feeds may be long—HF will truncate if over limit
        output = client.summarization("facebook/bart-large-cnn", text)
        return output[0]["summary_text"]
    except Exception as e:
        print("[ERROR] Summarization failed:", e)
        return "(summary unavailable)"

def send_message(text: str):
    """Send a text message to the Telegram group."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in textwrap.wrap(text, 4000, replace_whitespace=False):
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML"
        })
        print(f"[DEBUG] Telegram send → {resp.status_code}: {resp.text}")
        time.sleep(1)  # avoid hitting rate limits

def fetch_full_text(url: str) -> str:
    """Fetch and return the main body text from a circular’s page."""
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return f"(Could not fetch content; HTTP {r.status_code})"

    soup = BeautifulSoup(r.content, "html.parser")
    span = soup.find("span", id="ctl00_Content_Main_lblRelease")
    if span:
        return span.get_text(separator="\n", strip=True)

    # fallback—grab all <p> under the main panel
    panel = soup.find("div", id="ctl00_Content_Main_panelPressRelease")
    if panel:
        paras = panel.find_all("p")
        return "\n\n".join(p.get_text(strip=True) for p in paras if len(p.get_text(strip=True))>20)

    # last resort: whole text
    return soup.get_text(separator="\n", strip=True)

def load_last_title() -> str:
    if os.path.exists(LAST_TITLE_FILE):
        return open(LAST_TITLE_FILE).read().strip()
    return ""

def save_last_title(title: str):
    with open(LAST_TITLE_FILE, "w") as f:
        f.write(title)

def main():
    print("[DEBUG] Fetching RSS feed…")
    resp = requests.get(RSS_FEED, headers=HEADERS, timeout=10)
    print(f"[DEBUG] RSS HTTP status → {resp.status_code}")
    if resp.status_code != 200:
        return

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        print("[DEBUG] No entries found in RSS.")
        return

    last_title = load_last_title()
    print(f"[DEBUG] Last sent title → {repr(last_title)}")

    new_entries = []
    for entry in feed.entries:
        if entry.title == last_title:
            break
        new_entries.append(entry)

    if not new_entries:
        print("[DEBUG] No new circulars to send.")
        return

    # Process oldest first
    for entry in reversed(new_entries):
        print(f"[DEBUG] Processing → {entry.title}")
        # send header
        send_message(f"<b>{entry.title}</b>")

        # fetch full text and summarize
        full_text = fetch_full_text(entry.link)
        summary = summarize(full_text)
        send_message(summary + f"\n\n🔗 <a href=\"{entry.link}\">Read full circular</a>")

    # update state
    save_last_title(new_entries[0].title)
    print("[DEBUG] Updated last_title, done.")

if __name__ == "__main__":
    main()
