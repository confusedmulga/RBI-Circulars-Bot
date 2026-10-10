# RBI Circulars Bot

Posts a daily digest of new Reserve Bank of India notifications (circulars, directions) and press releases to a Telegram channel or group.

## How it works

- A GitHub Actions workflow runs `rbibot.py` every hour.
- Each run reads RBI's two RSS feeds, [notifications](https://www.rbi.org.in/notifications_rss.xml) and [press releases](https://www.rbi.org.in/pressreleases_rss.xml), and queues every item it has not seen before. Each feed only shows its latest 10 items, which is why it checks hourly rather than once a day.
- The first run at or after 9:00 IST sends everything queued as one digest (split into messages of up to about 3900 characters, under Telegram's 4096 limit):
  - **Notifications** and **Press releases**: title linked to the RBI page, the effective date when the circular states one, and a short extract of its opening paragraphs in a collapsible quote.
  - **Market operations**: routine press releases (auctions, money market operations, reserve money and similar, set by `ROUTINE_TITLE_PATTERNS` in `rbibot.py`) as bare links.
- Summaries are extracted from the text RBI includes in the feed. No ML model or external API is involved.
- `state.json` records the IDs already queued, the items waiting for the next digest and the date of the last digest. The workflow commits it back after each run that changes it. Items stay queued until Telegram accepts them, so a failed send is retried on the next run.
- A failed feed fetch or Telegram send fails the run, so GitHub shows it red and emails you instead of looking green.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather), add it to your channel or group (as an admin for channels) and note the chat ID.
2. In the repository's **Settings → Secrets and variables → Actions**, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never put the token in the code; if it ever lands in a commit, revoke it with `/revoke` in @BotFather and store the new one.
3. The workflow pushes `state.json` to the default branch as `github-actions[bot]`, so that branch must accept direct pushes from it (no rule requiring pull requests or signed commits).
4. Scheduled workflows only run from the default branch. The Actions tab should show a run every hour; if none appear, enable Actions under **Settings → Actions → General** and click **Enable workflow** on the workflow's page.

To send the queue immediately, run the workflow manually from the **Actions** tab with **send_now** ticked.

## Running locally

```sh
pip install -r requirements.txt
DRY_RUN=1 SEND_NOW=1 python rbibot.py
```

`DRY_RUN=1` prints the digest instead of sending it and leaves `state.json` untouched. Without it, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment.
