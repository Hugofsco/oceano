---
name: x-growth-playbook
description: audit an X/Twitter profile, write threads/tweets with proven hook patterns, analyze competitor accounts, and plan a posting calendar — use for X/Twitter-specific growth, distinct from the Facebook/Reddit playbooks in organic-community-launch
status: published
notes: ported from claude-skills marketing-skill/x-twitter-growth (MIT); scripts copied verbatim, stdlib-only; these are calculators/generators over numbers YOU supply, not live X API scrapers
---
# X/Twitter growth playbook

These tools score/generate from metrics you supply (or estimate) — none of them call
the live X API.

1. **Audit the profile:**
   `python3 skills/x-growth-playbook/scripts/profile_auditor.py --handle @you --bio "..." --followers N --posts-per-week N --reply-ratio 0.X`
2. **Study competitors:** `python3 skills/x-growth-playbook/scripts/competitor_analyzer.py --handles @acc1 @acc2 @acc3`
   (or `--import competitors.json`) — extract hook patterns, format mix, posting times.
3. **Write content:** `python3 skills/x-growth-playbook/scripts/tweet_composer.py --type thread --topic "<topic>" --tweets 8`
   (`--type hooks --count 5` for hook ideas alone; `--type validate --validate "<text>"` to check one tweet).
4. **Plan the calendar:** `python3 skills/x-growth-playbook/scripts/content_planner.py --niche "<niche>" --frequency 5 --weeks 2`
5. **Track growth over time:** `python3 skills/x-growth-playbook/scripts/growth_tracker.py --record --handle @you --followers N --eng-rate X` then `--report --period 30d`.

Rules that matter most: never put a link in the tweet body (kills reach — first reply
instead), a thread's tweet 1 must stop the scroll in <7 words or nothing else matters,
replies are 50%+ of growth not just broadcasting, and consistency beats occasional bangers.
