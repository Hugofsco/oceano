---
name: ci-cd-pipeline-builder
description: detect a project's stack and generate a GitHub Actions or GitLab CI pipeline from it — use when a real project has no CI yet, or its pipeline doesn't match what the project actually needs
status: published
notes: ported from claude-skills engineering/ci-cd-pipeline-builder (MIT); scripts copied verbatim, stdlib-only
---
# CI/CD pipeline builder

```
python3 skills/ci-cd-pipeline-builder/scripts/stack_detector.py --repo <path> --format json > detected-stack.json
python3 skills/ci-cd-pipeline-builder/scripts/pipeline_generator.py --input detected-stack.json --platform github --output .github/workflows/ci.yml
```
(or `--repo <path>` directly on the generator to skip the intermediate JSON file;
`--platform gitlab --output .gitlab-ci.yml` for GitLab)

Before trusting the output: confirm the detected `test`/`lint`/`build` commands actually
exist in the project (`package.json` scripts or equivalent), run the generated pipeline
locally where possible, and document any required secrets/env vars. Start with CI-only
(lint/test/build) — add a staging deploy stage, then a production stage gated by a
manual approval, never the other way around. Keep rollback commands explicit in the
pipeline, not tribal knowledge.
