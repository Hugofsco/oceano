---
name: grill-the-plan
description: interview the user one question at a time to stress-test a plan or design before acting on it — use before a risky/irreversible change, or when the user says "grill me" or wants their plan challenged
status: published
notes: adapted from Matt Pocock's grill-me skill (MIT)
---
# Grill the plan

Before committing to a nontrivial plan (a big refactor, an SSH batch on a registered server,
a store/site change, a trading-bot config change), interview the user until you both share
the same understanding. Don't just accept the first framing.

1. **Explore first.** If a question can be answered by reading the workspace, a registered
   server, or past memory instead of asking, do that — see [[search-the-codebase]] and
   [[debug-systematically]]. Only ask what you genuinely can't resolve yourself.
2. **One question per turn.** Never bundle several into one message.
3. **Always give a recommended answer** with your reasoning — "what do you think?" is lazy.
4. **Walk the decision tree depth-first.** Finish one branch (all its follow-ups) before
   opening the next.
5. **Respect dependencies.** If decision B only makes sense after decision A is settled,
   ask A first.
6. **Stop when it's actually resolved** — once every branch has an answer, say so explicitly
   ("shared understanding reached") and summarize the locked-in decisions before acting.

Output pattern per turn:
```
Q: <question>
Recommended: <your call + one-sentence why>
```
