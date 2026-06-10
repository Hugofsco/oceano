---
name: daily-digest
description: build a dated digest of recent news/updates on a topic and notify the user
---
# Daily digest

Good for scheduled tasks ("every morning, summarize AI news"). Steps:

1. Use the CURRENT DATE from your context — search for genuinely recent items
   (this year / this week), not stale ones.
2. `web_search` the topic, then `fetch_url` the 3-5 most relevant, recent results
   to read what actually happened (snippets aren't enough).
3. Write `digest-YYYY-MM-DD.md` to the workspace:
   - A one-line date header.
   - 4-6 bullets, each: a headline, one sentence of substance, and the source URL.
   - Skip anything you can't confirm from a real page.
4. If running as a scheduled task, `notify` the user with the TL;DR (a few bullets)
   so it reaches their phone.

Keep it skimmable and strictly recent. No filler.
