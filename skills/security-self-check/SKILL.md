---
name: security-self-check
description: check code you're about to write/edit against common vulnerability patterns before saving — use before Edit/Write on auth, payments, user-input handling, SQL, shell commands, or IaC/workflow files
status: published
notes: advisory self-check, not an enforced hook — Oceano has no PreToolUse hook mechanism, so this only helps if retrieved and actually followed
---
# Security self-check

Before writing or editing security-sensitive code, scan what you're about to save for these
patterns. Any hit is a stop-and-reconsider, not an automatic block — explain the risk to the
user if you keep it.

- Shell/command injection: `os.system`, `subprocess` with `shell=True`, Node `exec(`/`execSync(`,
  building a shell string from untrusted input instead of passing an argv list
- Code injection: `eval(`, `new Function(`, Python `pickle` on untrusted data,
  `yaml.load(`/`yaml.unsafe_load` instead of `yaml.safe_load`
- XSS: `dangerouslySetInnerHTML`, `.innerHTML =`, `document.write` with unsanitized input
- SQL injection: building a query with an f-string/`.format`/`+` instead of parameterized
  queries
- CI/workflow injection: GitHub Actions `${{ }}` expressions interpolating untrusted PR
  content directly into a `run:` step

If a file legitimately needs one of these (a sandboxed REPL, an internal tool with no
untrusted input), say so in a one-line comment next to it rather than silently proceeding —
future edits (by any mind) need to know it was a deliberate, reviewed choice.

This applies double to anything Oceano itself can reach: the SSH keychain, mail send/delete,
the shell/Python tools, workflow definitions — a bug there is a bug with real-world blast
radius, not just a demo.
