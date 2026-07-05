---
name: env-secrets-audit
description: scan a repo for likely-leaked secrets (API keys, tokens, passwords in code or .env files committed to git) — use before pushing commits that touched config/env files, or when hardening a project
status: published
notes: ported from claude-skills engineering/env-secrets-manager (MIT), heavily trimmed — dropped the Kubernetes/Vault/CloudTrail/SIEM material (no matching infra here), kept the practical scan + rotation basics
---
# Env & secrets audit

```
python3 skills/env-secrets-audit/scripts/env_auditor.py <path> --json
```
Scope it to the actual project, not a whole vendored/third-party tree — noise from
vendored code (e.g. a bundled C++ engine, node_modules) swamps real findings otherwise.

Findings sort critical/high/medium. Priority: rotate any real credential the scan finds
committed or hardcoded, then fix the pattern (move it to an untracked `.env`, add it to
`.gitignore`, put a placeholder in `.env.example`).

If a real credential leaked (not just a false positive): revoke it at the provider
immediately, generate + deploy the replacement, then check for unauthorized use before
considering it closed. This applies with extra weight to anything Oceano itself holds —
mail app passwords, the SSH keychain, the mind token in `data/`.
