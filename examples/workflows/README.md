# Example workflows

Ready-to-import workflows that each showcase a slice of what the canvas can do.
Import one via **Workflows → ⤒ Import** (they're standard export files), then open it
in the editor to poke around — every node's settings are in the right-hand inspector.

| File | What it does | What it demonstrates |
|---|---|---|
| `morning-briefing` | 08:00 daily: calendar + news → Markdown briefing → notify | schedule trigger · tool chaining · `{{node.ID}}` refs |
| `inbox-sentry` | new email → model judges urgency → alert or ignore | email trigger · model-judged decision · `{{input}}` |
| `api-watchdog` | poll a health URL every 15 min; retry after 5 min before alerting | cron · http retries + error edges · rule decisions · **wait** node · overlap guard |
| `research-fanout` | topic → web search ∥ local docs → merged one-page brief | **fork + merge** · workflow input · local RAG |
| `team-review-board` | persona panel debates an idea; you approve the verdict | **orchestrate** + agent nodes · personas · **approval** |
| `inbox-folder-indexer` | files dropped in `workspace/inbox` get indexed for search | file-watch trigger (restart-safe baselines) |
| `github-release-digest` | latest releases of a repo → looped, templated, digested | http · regex transform · **loop** with `{{item.field}}` · loop aggregation |
| `software-development-cycle` | a request through the whole cycle: PM requirements → CTO architecture → a **design-review panel** (build + ops reviews in parallel, devil's advocate attacking them) → **your sign-off** → implement → test → fix-until-green → code review → release-readiness → dossier | persona-per-stage · **orchestrate as a review board** · approval gate · **write-access tiers** · `run_tests` · a fix/retest loop-back edge |
| `daily-standup` | weekday 09:00: yesterday's commits + suite health + today's calendar → standup note | weekday cron · dev tools (`git` · `run_tests`) as plain tool nodes |
| `content-studio` | topic → outline → draft → adversarial critique → revision → approval → published **with audio narration** | multi-persona editorial chain · critique-then-revise · `speak_to_file` TTS |
| `competitor-watch` | Mondays: fetch every competitor URL from a list, analyze together | **loop over a newline list** from the input · `fetch_url` · aggregation |

After importing, a few need one touch of setup:

- **Inbox sentry** — select your mail account on the trigger node (accounts are added in
  Settings → Mail), then Save.
- **API watchdog** — point the input default at a real health endpoint.
- **GitHub release digest** — works unauthenticated on public repos; for private ones add
  an `Authorization: Bearer {{secret.GITHUB_TOKEN}}` header on the http node and store the
  token via the workflow list's **🔑 Secrets** button.
- **Software development cycle / Daily standup** — expect a project inside your `workspace/`
  (git repo, test suite). The cycle's implement/fix steps use the **write** access tier — read
  the ✎ notes on those nodes before running it against anything you care about, and note the
  fix/retest loop is bounded only by the run's visit cap (cancel from the jobs popup if it
  thrashes).
- **Competitor watch** — replace the placeholder URLs in the input default; the default is
  what scheduled runs use.
- Schedules import enabled — pause one from the ⏱ dialog if you're just exploring.

These files are covered by `tests/test_example_workflows.py`, which imports each one and
checks every tool/persona it references actually exists — so they can't silently rot.
