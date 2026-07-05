---
name: changelog-generator
description: generate a CHANGELOG.md and compute the next semver version from Conventional Commits — use when cutting a release for a real project, or to check what version bump a set of commits requires
status: published
notes: ported from claude-skills engineering/changelog-generator (MIT); scripts copied verbatim, stdlib-only
---
# Changelog generator

Conventional Commit rules: `feat`/`fix`/`perf`/`refactor`/`docs`/`test`/`chore`/etc.
Breaking change = `type(scope)!: summary` or a `BREAKING CHANGE:` footer. SemVer:
breaking → major, non-breaking `feat` → minor, everything else → patch.

1. **Compute the next version from commits** (input must be real `git log --oneline`
   output — hash + message, not bare messages):
   `git log v1.3.0..HEAD --oneline | python3 skills/changelog-generator/scripts/version_bumper.py --current-version 1.3.0 --output-format json`
   → `recommended_version` + `bump_type`.
2. **Generate the changelog entry:**
   `python3 skills/changelog-generator/scripts/generate_changelog.py --from-tag v1.3.0 --to-tag v1.4.0 --next-version v1.4.0 --write CHANGELOG.md`
3. **Lint commits before merging**, if you want to catch bad messages early:
   `git log origin/main..HEAD --oneline | python3 skills/changelog-generator/scripts/commit_linter.py --strict`

Prepend new entries, never overwrite history. Each bullet should be user-meaningful, not
implementation noise — "Fixed checkout crash on empty cart," not "fix null check."
Breaking changes need a migration note. Fail loudly if no valid conventional commits are
found — don't generate an empty changelog that looks like nothing happened.
