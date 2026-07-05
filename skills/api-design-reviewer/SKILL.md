---
name: api-design-reviewer
description: lint an OpenAPI/REST API spec for convention violations, detect breaking changes between two spec versions, and score overall design quality — use when reviewing an API change, planning a v2, or before shipping a new endpoint
status: published
notes: ported from claude-skills engineering/api-design-reviewer (MIT); scripts copied verbatim, stdlib-only
---
# API design reviewer

1. **Lint the spec:** `python3 skills/api-design-reviewer/scripts/api_linter.py <openapi.json> --format json`
   — naming conventions, HTTP method usage, error-format consistency, doc gaps.
2. **Check for breaking changes between versions:**
   `python3 skills/api-design-reviewer/scripts/breaking_change_detector.py <v1.json> <v2.json> --exit-on-breaking`
3. **Score design quality:** `python3 skills/api-design-reviewer/scripts/api_scorecard.py <openapi.json> --min-grade B`
   — weighted across consistency/docs/security/usability/performance, letter grade A-F.

Run all three, report findings + grade, fix, re-run until the linter is clean and the
scorecard clears the bar — don't sign off on prose alone.

Quick conventions worth remembering without opening a reference: nouns not verbs in URLs
(`/users` not `/getUsers`), kebab-case resources, version in the URL (`/api/v1/...`),
paginate every list endpoint, never remove a field or make an optional field required
without a version bump.
