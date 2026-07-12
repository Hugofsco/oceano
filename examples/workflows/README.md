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
| `app-builder-idea-to-launch` | an idea becomes a running app, **staffed like a company**: PM requirements → CTO architecture → **kickoff panel** → design spec → branding → **your sign-off** → the backend-engineer builds db + API, the frontend-engineer builds the UI (unit tests per layer) → on-call fix-until-green → **per-area review** → quality report → a **launch meeting** votes GO/NO-GO | engineers **build**, specialists review · **two orchestrated panels + a voting meeting** · approval gate · write tiers · delegation idle-timeout on builds · fork (minutes saved while the verdict routes) |
| `app-builder-iteration` | keep developing the app after launch: change request → impact analysis on the real code → sign-off → implement+test → review → report proposing the next 3 iterations | the follow-up loop · context from the previous cycle's reports · approval · fix/retest loop |
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
- **App builder (both) / Daily standup** — work inside your `workspace/`. The build/implement/fix
  steps use the **write** access tier — read the ✎ notes on those nodes before running them
  against anything you care about, and note the fix/retest loop is bounded only by the run's
  visit cap (cancel from the jobs popup if it thrashes). Build delegates deliberately set **no
  timeout**: they run on delegation's idle timeout (a long active build survives; only a
  stalled one is stopped — set the node's *timeout (s)* field to cap one explicitly). Run
  *idea-to-launch* once, then *iteration* per change request — it reads the launch cycle's
  reports for context, and its description shows how to wire a chat-keyword or chain trigger
  to keep the loop going.
- **Competitor watch** — replace the placeholder URLs in the input default; the default is
  what scheduled runs use.
- Schedules import enabled — pause one from the ⏱ dialog if you're just exploring.

These files are covered by `tests/test_example_workflows.py`, which imports each one and
checks every tool/persona it references actually exists — so they can't silently rot.
