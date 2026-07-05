# Hunk Auto Diff

Herdr plugin that automatically opens a [hunk](https://github.com/modem-dev/hunk) diff in a split pane whenever a coding agent (Claude Code, Codex, etc.) finishes a turn with uncommitted changes.

No keybinding, no manual action — when the agent in a pane goes **idle** and its working tree is dirty, a split opens on the right running `hunk diff --watch`. Focus stays on the agent pane.

## Install

```bash
herdr plugin install scott306lr/herdr-plugin-hunk-autodiff
```

## Behavior

On every `pane.agent_status_changed` event where the agent becomes `idle`:

1. Resolves the pane's cwd; exits silently unless it is inside a git repo with working-tree changes.
2. If a hunk session is already live for that repo (`hunk session get --repo`), does nothing — the existing pane's `--watch` keeps it fresh.
3. If the diff is unchanged since a pane was last opened for it, does nothing — closing the hunk pane keeps it closed until new changes appear.
4. Otherwise splits the agent's pane to the right (`--no-focus`), labels it `hunk`, and runs `hunk diff --watch`.

## Requirements

- Herdr `0.7.0` or newer
- `python3`
- `hunk` (or `bunx hunkdiff` as fallback)
- An agent integration that reports agent state to herdr (`herdr integration install claude`, `codex`, …)

## Notes

- Herdr runs plugin event hooks with a minimal `PATH`; the plugin resolves `hunk` from common install locations (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`).
- Diff signatures are stored under the plugin state dir (`HERDR_PLUGIN_STATE_DIR`), one small file per repo.

## License

MIT
