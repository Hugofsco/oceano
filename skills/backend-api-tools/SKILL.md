---
name: backend-api-tools
description: scaffold API routes from an OpenAPI spec, analyze/migrate a database schema, or load-test an endpoint — use when building or hardening a backend API
status: published
notes: ported from claude-skills engineering-team/senior-backend (MIT), trimmed — dropped the decision-engine/profiles apparatus, kept the three concrete tools
---
# Backend API tools

1. **Scaffold routes from a spec:** `python3 skills/backend-api-tools/scripts/api_scaffolder.py <openapi.yaml> --framework express|fastify|koa --output src/routes/`
   (or `--from-db <connection-string>` to generate from an existing schema instead;
   `--generate-spec` to go the other way, routes → OpenAPI).
2. **Analyze/migrate a database:** `python3 skills/backend-api-tools/scripts/database_migration_tool.py --connection <conn-string> --analyze`
   → missing indexes, N+1 risks, suggested migrations. Always `--dry-run` a migration
   before applying it for real.
3. **Load test an endpoint:** `python3 skills/backend-api-tools/scripts/api_load_tester.py <url> --concurrency 50 --duration 30`
   → throughput, P50/P95/P99 latency, error rate. `--compare` two endpoints/versions
   side by side.

Security baseline for any new endpoint: never a hardcoded secret (env var only), rate
limit public routes, validate every input before it reaches business logic, prefer
short-lived tokens. Use [[security-self-check]] before writing auth/payment code, and
[[api-design-reviewer]] to lint the spec itself once it exists.
