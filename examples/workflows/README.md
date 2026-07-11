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

After importing, a few need one touch of setup:

- **Inbox sentry** — select your mail account on the trigger node (accounts are added in
  Settings → Mail), then Save.
- **API watchdog** — point the input default at a real health endpoint.
- **GitHub release digest** — works unauthenticated on public repos; for private ones add
  an `Authorization: Bearer {{secret.GITHUB_TOKEN}}` header on the http node and store the
  token via the workflow list's **🔑 Secrets** button.
- Schedules import enabled — pause one from the ⏱ dialog if you're just exploring.

These files are covered by `tests/test_example_workflows.py`, which imports each one and
checks every tool/persona it references actually exists — so they can't silently rot.
