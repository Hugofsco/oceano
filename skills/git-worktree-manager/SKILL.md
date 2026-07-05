---
name: git-worktree-manager
description: create an isolated git worktree (own branch, own ports, own .env) for parallel feature work, and clean up merged/stale ones — use when running 2+ branches at once, or when a spawn_agent sub-agent needs to work on a branch without colliding with the main one
status: published
notes: ported from claude-skills engineering/git-worktree-manager (MIT); scripts copied verbatim, stdlib-only
---
# Git worktree manager

Directly useful for Oceano's own `spawn_agent`/background-job model: give each parallel
sub-agent its own worktree instead of letting them collide on one checkout.

1. **Create a prepared worktree:**
   `python3 skills/git-worktree-manager/scripts/worktree_manager.py --repo . --branch <branch> --name <wt-name> --base-branch main --install-deps`
   Allocates non-conflicting ports (persisted to `.worktree-ports.json`) and copies
   `.env*` files from the main repo.
2. **Clean up when done:**
   `python3 skills/git-worktree-manager/scripts/worktree_cleanup.py --repo . --stale-days 14`
   (`--remove-merged` to actually remove; without it, it only reports)

Never force-remove a worktree with uncommitted changes. One branch per worktree, remove
it once merged — don't let them pile up.
