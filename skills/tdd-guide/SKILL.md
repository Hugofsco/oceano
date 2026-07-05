---
name: tdd-guide
description: generate unit tests from source code, analyze test-coverage gaps, and guide a red-green-refactor cycle across Jest/Pytest/Vitest/JUnit — use when writing tests or improving coverage on a real change
status: published
notes: ported from claude-skills engineering-team/tdd-guide (MIT); kept the 4 core scripts, dropped 4 auxiliary format-conversion/metrics helpers with no cross-script dependency on the ones kept
---
# TDD guide

1. **Generate tests from existing code:** `python3 skills/tdd-guide/scripts/test_generator.py --input <source.py> --framework pytest|jest|junit|vitest`
   — covers happy path, error cases, and edge cases; review before trusting it on
   anything non-trivial.
2. **Find coverage gaps:** run your test runner's coverage report, then
   `python3 skills/tdd-guide/scripts/coverage_analyzer.py --report <lcov.info|coverage.json> --threshold 80`
   → P0 (uncovered error paths) / P1 (core logic branches) / P2 (utility functions).
   Write P0 tests first.
3. **Generate fixtures/mocks:** `python3 skills/tdd-guide/scripts/fixture_generator.py --entity <Name> --count 5`
4. **Validate a red-green-refactor cycle:** `python3 skills/tdd-guide/scripts/tdd_workflow.py --phase red|green --test <file>`

Stop and ask the user rather than generating autonomously when: requirements are
ambiguous, a boundary value needs domain knowledge you don't have, the suite would
exceed ~50 new tests, or the code is auth/payment/encryption (needs human sign-off on
scenarios, not just generated coverage). Otherwise — clear spec, plain CRUD, pure
functions, an existing test pattern to follow — just proceed.

100% line coverage isn't the goal; a test that runs code but asserts nothing meaningful
passes coverage and catches nothing. When it matters (auth, payments), that's worth a
mutation-testing pass (`mutmut`/`Stryker`/`PIT`) over chasing another coverage point.
