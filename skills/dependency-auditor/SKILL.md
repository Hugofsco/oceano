---
name: dependency-auditor
description: scan a coding project's dependencies for known-vulnerable packages, license conflicts, and plan safe upgrades — use before a release, when investigating a CVE, or before a major version bump
status: published
notes: ported from claude-skills engineering/dependency-auditor (MIT); scripts copied verbatim, stdlib-only, offline pattern-matchers (pair with npm audit/pip-audit/cargo audit for live CVE data)
---
# Dependency auditor

Offline, deterministic scans across npm/pip/go/cargo/gem/maven/composer/nuget manifests
and lockfiles. These are pattern-matchers over an offline CVE/license set, not live
advisory lookups — treat findings as a smoke layer and still run the ecosystem's own
`npm audit` / `pip-audit` / `cargo audit` for current coverage.

1. **Scan for vulnerabilities:**
   `python3 skills/dependency-auditor/scripts/dep_scanner.py <project_path> --format json --fail-on-high -o scan.json`
   (`--quick-scan` skips transitive deps for a faster pass)
2. **Check license compliance:**
   `python3 skills/dependency-auditor/scripts/license_checker.py <project_path> --policy strict --warn-conflicts`
   Flags GPL/AGPL contamination in a permissive-licensed project.
3. **Plan upgrades from the scan:**
   `python3 skills/dependency-auditor/scripts/upgrade_planner.py scan.json --risk-threshold medium --timeline 90`
   Orders by risk (safe → critical); `--security-only` limits to security fixes.

**Verification loop:** after applying upgrades, re-run step 1 and confirm 0 high-severity
findings before calling the audit done. See [[security-self-check]] before editing any
file the scan flags.
