---
name: skill-security-auditor
description: scan a skill directory (or a new one before you save it) for dangerous code patterns, prompt injection in its SKILL.md, and filesystem-boundary violations — use before installing a skill from an untrusted source, or when learn_skill/save_skill is about to publish something
status: published
notes: ported from claude-skills engineering/skill-security-auditor (MIT); script copied verbatim, stdlib-only, static analysis only (doesn't execute code)
---
# Skill security auditor

Static scanner, not a sandbox — it flags patterns, doesn't execute anything. Run it on
any skill before trusting it, including ones you're about to write yourself with
[[author-a-skill]].

```
python3 skills/skill-security-auditor/scripts/skill_security_auditor.py skills/<slug>/
```
`--strict` turns any WARN into FAIL; `--json` for machine-readable output.

Verdict meaning:
- **PASS** — no critical/high findings, safe to trust
- **WARN** — review the findings manually before trusting it
- **FAIL** — critical finding (command injection, eval/exec, credential-file reads,
  data exfiltration to a network call, prompt-injection phrasing in the SKILL.md body)
  — do not run scripts from this skill until fixed

When a network call gets flagged (`urllib`/`requests`/`socket`) but is genuinely needed
(e.g. [[aeo-content-citations]]'s URL-mode audit, [[academic-literature-search]]'s free
API calls), that's expected — confirm the destination is the documented one, not a
surprise. When in doubt, don't run the scripts.
