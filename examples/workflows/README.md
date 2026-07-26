# Example workflows

Ready-to-import workflows. Import via **Workflows → ⤒ Import** (multi-select works — they're
standard export files), then open one in the editor to poke around: every node's settings are
in the right-hand inspector.

## App builder

| File | What it does |
|---|---|
| `app-builder-idea-to-production` | An idea becomes a reviewed **release candidate** under a deterministic `projects/<slug>-<id>/` path. Discovery and technical planning happen first; a human must approve the exact plan and destination before code is written. Backend → frontend → devops/security build with shell access, then a real zero-exit test gate runs before execute-only security, operability, and UX reviewers. Failed tests or readiness findings loop through a fix-and-reverify cycle. Discovery and readiness reports stay under the generated project's `docs/` directory. |

Every fallible step routes its error edge to a shared failure notice instead of dying silently,
single-shot steps get one automatic retry, and both meeting/review panels stagger to 2-then-1
concurrent agents to leave headroom under the box's agent concurrency cap.

Before running:

- Work happens inside your `workspace/`. Build/fix steps use the **⚠ shell** tier; reviewers use
  **▶ execute** so they can run checks without file-edit tools. Shell commands may still have side
  effects, so inspect the plan at the approval gate before allowing implementation.
- The development loop is bounded by the run's visit cap. Pause or cancel from the jobs popup if
  it thrashes. If a run reaches its failure notice, fix the cause and start a new run; Resume is for
  a run that is still paused, not one that already completed its failure path.

## Everyday automations

| File | What it does |
|---|---|
| `inbox-sentry` | Processes every **new email**, including concurrent bursts. Email bodies are fenced as untrusted data before a model-judged urgency decision; urgent/personal mail becomes a two-line alert, while classification failures produce a content-free warning. After importing, **select your mail account on the trigger node** (accounts are added in Settings → Mail), then Save. |
| `daily-standup` | Weekday mornings at 09:00: commits from the last 24 hours (`git log`), test-suite health (`run_tests`), and calendar events for today and tomorrow, compiled into `workspace/dev/standup.md` with suggested top-3 priorities. Preparation failures notify separately. Point your workspace at a git project first; the schedule imports enabled — pause it from the ⏱ dialog if you're just exploring. |

These files are covered by `tests/test_example_workflows.py`, which imports each one and
checks every tool and persona it references actually exists — so they can't silently rot.
