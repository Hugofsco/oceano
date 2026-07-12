# Example workflows

Ready-to-import workflows. Import via **Workflows → ⤒ Import** (multi-select works — they're
standard export files), then open one in the editor to poke around: every node's settings are
in the right-hand inspector.

## The App builder pair

| File | What it does |
|---|---|
| `app-builder-idea-to-launch` | An idea becomes a running app, **staffed like a company**: PM requirements → a **kickoff panel** (growth + finance in parallel, devil's advocate attacking them) → an **architecture meeting** where the engineers decide the design *together* (backend + frontend each propose their layer, devops reconciles the mismatches, the **CTO chairs the minutes into the final architecture**) → design spec (frontend-designer) → branding (content-strategist) → **your sign-off** → the **backend-engineer builds** the database layer and API, the **frontend-engineer builds** the UI to the spec (unit tests per layer) → on-call fix-until-green → **per-area review** (backend+db · frontend-vs-spec · ops) → quality report → a **launch meeting** where PM and devops vote GO/NO-GO, minutes saved, verdict routed to a 🚀/🛑 notification. |
| `app-builder-iteration` | The follow-up loop: a change request → PM iteration requirements (reads the previous cycle's reports) → an **engineering huddle** (backend take first, frontend responds to it — both reading the real code) → the **CTO chairs the huddle** into the minimal change-set plan, declaring the owning **AREA** → **your sign-off** → a **switch routes the work to the right engineer** (backend-engineer / frontend-engineer / both in sequence for fullstack) → fix-until-green → code review → an iteration report proposing the next three iterations. |

Between them they exercise most of the canvas: personas per stage, orchestrated panels,
approval gates, switch routing, decision loops, write-access tiers, `run_tests`, forks, and
`{{node.ID}}` templating.

Before running:

- Work happens inside your `workspace/` — the build/implement/fix steps use the **✎ write**
  access tier, so read those nodes' notes before pointing this at anything you care about.
- Build delegates deliberately set **no timeout**: they run on delegation's **idle timeout**
  (a long *active* build survives; only a stalled one is stopped). Set a node's *timeout (s)*
  field to cap one explicitly.
- The fix/retest loop is bounded only by the run's visit cap — cancel from the jobs popup if
  it thrashes.
- Run *idea-to-launch* once, then *iteration* per change request. To keep the loop firing
  itself, give *iteration* a chat-keyword trigger (e.g. "iterate on the app") or a chain
  trigger after another workflow.

## Everyday automations

| File | What it does |
|---|---|
| `inbox-sentry` | Fires on every **new email**: a model-judged decision separates urgent/personal mail from newsletters and notifications; urgent mail becomes a two-line alert notification. After importing, **select your mail account on the trigger node** (accounts are added in Settings → Mail), then Save. |
| `daily-standup` | Weekday mornings at 09:00: yesterday's commits (`git log`), test-suite health (`run_tests`), and today's calendar, compiled into a standup note at `workspace/dev/standup.md` with suggested top-3 priorities. Point your workspace at a git project first; the schedule imports enabled — pause it from the ⏱ dialog if you're just exploring. |

These files are covered by `tests/test_example_workflows.py`, which imports each one and
checks every tool and persona it references actually exists — so they can't silently rot.
