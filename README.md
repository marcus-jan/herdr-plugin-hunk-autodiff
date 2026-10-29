# Hunk Auto Diff

Herdr plugin that keeps [hunk](https://github.com/modem-dev/hunk) review tabs in the background, refreshed whenever a coding agent (Claude Code, Codex, OpenCode, etc.) finishes a turn:

| Tab | Shows | When it exists |
| --- | --- | --- |
| `pr #<n>` | The GitHub PR diff (`gh pr diff`), as pushed | The branch has an open PR |
| `diff` | Everything changed on the branch since its baseline | No open PR for the branch |
| `unpushed` | Commits not yet pushed (`@{upstream}...HEAD`) | The branch has an upstream and is ahead of it |
| `uncommitted` | Staged, unstaged, and untracked changes vs `HEAD` | The working tree is dirty |
| `turn` | What the agent changed in its last turn | The last turn changed something |

> Fork of [scott306lr/herdr-plugin-hunk-autodiff](https://github.com/scott306lr/herdr-plugin-hunk-autodiff), which opens a right-hand split of uncommitted changes.

## Install

```bash
herdr plugin install marcus-jan/herdr-plugin-hunk-autodiff
```

## Behavior

When the agent in a pane becomes `working`, the plugin snapshots the working tree (for the `turn` tab). When it becomes `idle` or `done`:

1. Resolves the pane's git checkout and branch. Detached `HEAD` or no repo: exits silently.
2. Computes each view above. Each tab is one per workspace and checkout, opened in the background (`--no-focus`) and reused, never duplicated.
3. For each kind:
   - **Has content, tab running:** updated in place (patch files are rewritten only when their content changes; hunk `--watch` reloads).
   - **Has content, tab missing or closed:** reopened.
   - **No content:** its tab is closed.

It skips opening new tabs when you already have your own hunk session open for that repo.

### PR tab

- Uses `gh pr view` / `gh pr diff` for the branch, so `gh` must be installed and authenticated.
- Only refreshes at turn end — pushes made outside an agent turn show up after the next turn or the `open` action.
- If `gh` fails transiently, the existing tab is left alone. Repos that aren't on GitHub fall back to the `diff` tab.
- Branches pushed to a fork may not resolve to their PR.

### Turn tab

- Snapshot is stored as a local ref, `refs/worktree/hunk-autodiff-turn/<branch>`, and removed when the turn ends.
- `blocked` → `working` keeps the original snapshot, so permission prompts don't split a turn.
- Several agents working in the same checkout at once blur each other's turns.

### Baseline (`diff` tab)

- Stored per worktree and branch as a local ref, `refs/worktree/hunk-autodiff/<branch>`. It is never pushed and keeps the commit safe from `git gc`.
- **Captured at creation** for worktrees made with `herdr worktree create` (`worktree.created` event).
- **Inferred on first sight** otherwise: the merge-base with the default branch (`origin/HEAD`, then `origin/main`, `origin/master`, `main`, `master`). On the default branch itself, the first-seen `HEAD` is used.
- Override the default branch per repo with `git config hunk-autodiff.base origin/develop`.
- On feature branches it follows the merge-base **forward** after a rebase or merge from the default branch, so upstream changes drop out of the diff. It never moves backwards, and it never moves on its own on the default branch — so auto commit + push there keeps accumulating.

## Actions

| Action | What it does |
| --- | --- |
| `hunk.autodiff.open` | Open (or refresh) all applicable tabs for the focused checkout and focus the `pr` tab (or `diff` if there's no PR), even with no changes. |
| `hunk.autodiff.reset-baseline` | Move the baseline to the current `HEAD` and close all of the plugin's tabs. Use this to start a fresh review cycle. |

Bind them in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+d"
type = "plugin_action"
command = "hunk.autodiff.open"
description = "open hunk diff tabs"
```

## Requirements

- Herdr `0.7.0` or newer (`worktree.created` capture needs a Herdr that emits that event; inference covers older versions)
- `python3` (stdlib only)
- `git` 2.20+ (per-worktree refs)
- `hunk` (or `bunx hunkdiff` as fallback)
- `gh` (optional, for the PR tab)
- An agent integration that reports agent state to herdr (`herdr integration install claude`, `codex`, …)

## Notes

- Herdr runs plugin event hooks with a minimal `PATH`; the plugin resolves `hunk` and `gh` from common install locations (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `~/.bun/bin`).
- State lives under the plugin state dir (`HERDR_PLUGIN_STATE_DIR`): `tabs.json` maps workspace + checkout + kind to its tab, and `patches/` holds the patch files the `pr`, `unpushed`, and `turn` tabs watch.
- To drop a baseline entirely: `git update-ref -d refs/worktree/hunk-autodiff/<branch>`.

## License

MIT
