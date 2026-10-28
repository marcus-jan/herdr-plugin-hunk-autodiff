# Hunk Auto Diff

Herdr plugin that keeps a [hunk](https://github.com/modem-dev/hunk) diff of **everything changed on the current branch** in a background tab, refreshed whenever a coding agent (Claude Code, Codex, OpenCode, etc.) finishes a turn.

Unlike a plain `hunk diff`, the diff is taken against a fixed per-branch **baseline**, so it keeps working when the agent commits and pushes as it goes.

> Fork of [scott306lr/herdr-plugin-hunk-autodiff](https://github.com/scott306lr/herdr-plugin-hunk-autodiff), which opens a right-hand split of uncommitted changes.

## Install

```bash
herdr plugin install marcus-jan/herdr-plugin-hunk-autodiff
```

## Behavior

When the agent in a pane becomes `idle` or `done`:

1. Resolves the pane's git checkout and branch, and its baseline (see below).
2. Exits silently unless the working tree (including untracked files) differs from the baseline.
3. If the workspace already has this plugin's `diff` tab running hunk, does nothing — `--watch` keeps it fresh.
4. If the cumulative diff is unchanged since the tab was last opened, does nothing — closing the tab or quitting hunk keeps it closed until new changes appear.
5. Otherwise opens a background tab labelled `diff` (`--no-focus`) running `hunk diff <baseline> --watch`. One tab per workspace and checkout; it is reused, never duplicated.

It also skips opening a tab when you already have your own hunk session open for that repo.

### Baseline

- Stored per worktree and branch as a local ref, `refs/worktree/hunk-autodiff/<branch>`. It is never pushed and keeps the commit safe from `git gc`.
- **Captured at creation** for worktrees made with `herdr worktree create` (`worktree.created` event).
- **Inferred on first sight** otherwise: the merge-base with the default branch (`origin/HEAD`, then `origin/main`, `origin/master`, `main`, `master`). On the default branch itself, the first-seen `HEAD` is used.
- Override the default branch per repo with `git config hunk-autodiff.base origin/develop`.
- On feature branches it follows the merge-base **forward** after a rebase or merge from the default branch, so upstream changes drop out of the diff. It never moves backwards, and it never moves on its own on the default branch — so auto commit + push there keeps accumulating.

## Actions

| Action | What it does |
| --- | --- |
| `hunk.autodiff.open` | Open (or refresh) the diff tab for the focused checkout and focus it, even with no changes. |
| `hunk.autodiff.reset-baseline` | Move the baseline to the current `HEAD` and close the diff tab. Use this to start a fresh review cycle. |

Bind them in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+d"
type = "plugin_action"
command = "hunk.autodiff.open"
description = "open hunk diff tab"
```

## Requirements

- Herdr `0.7.0` or newer (`worktree.created` capture needs a Herdr that emits that event; inference covers older versions)
- `python3` (stdlib only)
- `git` 2.20+ (per-worktree refs)
- `hunk` (or `bunx hunkdiff` as fallback)
- An agent integration that reports agent state to herdr (`herdr integration install claude`, `codex`, …)

## Notes

- Herdr runs plugin event hooks with a minimal `PATH`; the plugin resolves `hunk` from common install locations (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `~/.bun/bin`).
- State lives under the plugin state dir (`HERDR_PLUGIN_STATE_DIR`): `tabs.json` maps workspace + checkout to the diff tab, and one small `seen-*` signature file per checkout and branch.
- To drop a baseline entirely: `git update-ref -d refs/worktree/hunk-autodiff/<branch>`.

## License

MIT
