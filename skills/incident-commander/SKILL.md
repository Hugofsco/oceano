---
name: incident-commander
description: classify severity, reconstruct a timeline, and write a blameless post-incident review when something real breaks (the store, a trading bot, a server) — distinct from security incident triage
status: published
notes: ported from claude-skills engineering-team/incident-commander (MIT), scripts copied verbatim, SKILL.md rewritten from enterprise-SRE scale (war rooms, PagerDuty, exec escalation) down to solo-operator scale
---
# Incident commander (solo scale)

For availability/reliability incidents — the store down, a trading bot stuck or
misbehaving, a server unreachable. Not security incidents (intrusion, data exfil —
that's a different threat model this doesn't cover).

1. **Classify severity first**, don't just start fixing:
   `echo '{"description": "...", "affected_users": "...", "business_impact": "..."}' | python3 skills/incident-commander/scripts/incident_classifier.py`
   SEV1 (real money/customers affected right now) gets your full attention immediately;
   SEV4 (cosmetic, no real impact) can wait.
2. **Reconstruct the timeline** as you go, don't rely on memory after:
   `python3 skills/incident-commander/scripts/timeline_reconstructor.py --input events.json --gap-analysis`
3. **Prefer rollback to a risky fix under pressure.** Validate the fix actually worked
   before declaring it resolved — see [[debug-systematically]]'s reproduce-first
   discipline, which applies here too.
4. **After resolution, write a blameless PIR** — what broke, why, what fixes it for
   good, not who's at fault:
   `python3 skills/incident-commander/scripts/pir_generator.py --incident incident.json --rca-method "5whys" --action-items`
5. **Turn what you learned into a runbook** with [[runbook-generator]] so the next
   occurrence (or a different mind) has the steps ready instead of re-debugging from
   scratch.
