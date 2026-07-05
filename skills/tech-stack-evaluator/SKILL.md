---
name: tech-stack-evaluator
description: compare frameworks/libraries with weighted scoring, calculate multi-year TCO, or estimate migration effort — use when starting a new project and picking a stack, or deciding whether a migration is worth it
status: published
notes: ported from claude-skills engineering-team/tech-stack-evaluator (MIT); kept the 5 analysis scripts, dropped 2 auxiliary format/report helpers with no cross-script dependency
---
# Tech stack evaluator

- **Compare technologies:** `python3 skills/tech-stack-evaluator/scripts/stack_comparator.py --help`
  (weighted criteria — pass your own priorities, don't accept generic defaults)
- **5-year TCO:** `python3 skills/tech-stack-evaluator/scripts/tco_calculator.py --input <config.json>`
- **Ecosystem health:** `python3 skills/tech-stack-evaluator/scripts/ecosystem_analyzer.py --technology <name>`
  (GitHub/npm/community signals)
- **Security posture:** `python3 skills/tech-stack-evaluator/scripts/security_assessor.py --technology <name> --compliance soc2,gdpr`
- **Migration estimate:** `python3 skills/tech-stack-evaluator/scripts/migration_analyzer.py --from <old> --to <new>`

Skip this for trivial or already-decided choices — it's for a real fork in the road
(new project's stack, or "is this migration worth the effort"), not routine tool
picking. Report a confidence level with any comparison: high (clear winner, strong
data) / medium (real tradeoffs) / low (close call, thin data) — don't present a close
call as a clean recommendation.
