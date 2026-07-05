---
name: ship-gate-check
description: pre-production audit before pushing a real change live — use when the user says "deploy", "push to production", "go live", or before changing anything on the WooCommerce store, a trading bot, or another real service
status: published
notes: ported from claude-skills engineering/ship-gate (MIT); script copied verbatim, stdlib-only; original also ships a checklist-based methodology (references/checks.md, patterns.md) not copied here — the script covers the automatable part
---
# Ship gate check

When the user signals deploy-intent ("push this live", "deploy", "go live") on
something with real-world consequences (the WooCommerce store, a trading bot, a public
site), don't just do it — run the gate first, or ask if they already have.

```
python3 skills/ship-gate-check/scripts/ship_gate_scanner.py <project_path> --json
```

Findings sort into three severities:
- **CRITICAL** — must fix first (exposed secrets, no auth on a route, SQL injection
  shape, no HTTPS)
- **HIGH** — should fix (no error handling, console/debug logging left in, no rate
  limiting)
- **ADVISORY** — nice to have, not blocking

Report the verdict plainly: **CLEAR TO SHIP** (zero critical), **SHIP WITH CAUTION**
(only high items — say what the risk is), or **DO NOT SHIP** (critical items open). This
skill audits; it doesn't fix — hand fixes to [[debug-systematically]] or
[[security-self-check]] and re-run before shipping.
