---
name: fullstack-scaffolder
description: scaffold a new fullstack project (Next.js, FastAPI+React, MERN, Django+React) or audit an existing codebase for security/complexity/test-coverage issues — use when starting a new side project or checking one over
status: published
notes: ported from claude-skills engineering-team/senior-fullstack (MIT), trimmed — dropped the decision-engine/profiles apparatus, kept the two immediately-useful tools
---
# Fullstack scaffolder + quality check

**New project:** `python3 skills/fullstack-scaffolder/scripts/project_scaffolder.py <template> <name>`
— templates: `nextjs`, `fastapi-react`, `mern`, `django-react`. `--list-templates` to see
them, `--json` for machine output. Generates the structure, package configs, Docker
setup, and an `.env.example` — verify `package.json`/`requirements.txt` exists after,
then `npm install`/`pip install` and go.

**Quality audit:** `python3 skills/fullstack-scaffolder/scripts/code_quality_analyzer.py <path> --verbose`
— scores 0-100 across security (hardcoded secrets, injection risks), complexity
(cyclomatic, nesting depth), dependency health, test coverage estimate, and docs. Fix
P0 (critical) findings immediately, re-run to confirm before moving on — don't let a
security finding sit in a P1 backlog.

Quick stack picks when starting something new: SEO-critical → Next.js SSR; internal
dashboard → React+Vite (what you already use); API-first backend → FastAPI; document-
heavy data → MongoDB; complex relational queries → PostgreSQL.
