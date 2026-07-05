---
name: runbook-generator
description: generate an operational runbook skeleton (health checks, rollback steps, escalation) for a service before it needs one during an incident — use before launching a new project or trading bot to production
status: published
notes: ported from claude-skills engineering/runbook-generator (MIT); script copied verbatim, stdlib-only
---
# Runbook generator

```
python3 skills/runbook-generator/scripts/runbook_generator.py <service-name> --owner <name> --output docs/runbooks/<service>.md
```
Produces a skeleton: overview, preconditions, health checks, mitigation strategies with
rollback steps, recovery/validation checklist.

Then fill in the actually-useful part yourself: real commands (copy-pasteable, not
placeholders), real URLs/log locations, and an expected-output check after every
critical step — a runbook step with no way to verify it worked isn't a runbook step.
Test it once outside of a real incident, not for the first time during one. Update it
after every real incident via [[incident-commander]]'s PIR — a runbook that never
changes after a postmortem wasn't actually read.
