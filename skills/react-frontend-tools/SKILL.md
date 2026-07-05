---
name: react-frontend-tools
description: scaffold a new React/Next.js project or component, or check a project's bundle health — use when building/starting frontend work (matches your React+TypeScript+Vite projects)
status: published
notes: ported from claude-skills engineering-team/senior-frontend (MIT), heavily trimmed — dropped the decision-engine/profiles/forcing-question apparatus (built for a multi-agent consulting flow this doesn't need), kept the 3 tools that produce immediate value; bundle_analyzer's Next.js-specific tips don't apply to a Vite project, ignore those
---
# React/frontend tools

1. **New project:** `python3 skills/react-frontend-tools/scripts/frontend_scaffolder.py <name> --template nextjs|react` (`--features auth,api,forms,testing` to add pieces; `--dry-run` to preview first).
2. **New component:** `python3 skills/react-frontend-tools/scripts/component_generator.py <Name> --dir src/components/ui --type client|server|hook --with-test --with-story`.
3. **Bundle health check:** `python3 skills/react-frontend-tools/scripts/bundle_analyzer.py <project_path>` — 0-100 score, flags heavy deps (moment→date-fns, lodash→lodash-es, axios→native fetch) with lighter alternatives. Ignore its Next.js-specific suggestions on a plain Vite project.

React patterns worth defaulting to: extract shared state into a custom hook rather than
prop-drilling; Server Components by default in Next.js, `'use client'` only when you
need state/effects/event handlers/browser APIs; `cn()` (clsx + tailwind-merge) for
conditional Tailwind classes.

Accessibility floor for every new component: semantic HTML over `<div onClick>`,
visible focus states, real `<label>`s not just placeholders — see [[a11y-audit]] for
the full scan. Bundle budget: don't add a dependency heavier than what it replaces
without checking `bundle_analyzer.py` first.
