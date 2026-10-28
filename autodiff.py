#!/usr/bin/env python3
"""Herdr plugin: keep a background hunk diff tab against a fixed baseline.

Event hooks
- pane.agent_status_changed: when an agent goes idle/done and its checkout
  differs from the branch baseline, open (or refresh) a background "diff" tab
  in that workspace running `hunk diff <baseline> --watch`.
- worktree.created: record the new worktree's starting commit as its baseline.

Actions (argv[1])
- open:  open or refresh the diff tab for the focused checkout and focus it.
- reset: move the baseline for the focused checkout to its current HEAD.

The baseline is stored per worktree and branch as a local ref
(refs/worktree/hunk-autodiff/<branch>), so it survives commits, pushes and
gc, and is never pushed. It is inferred on first sight as the merge-base with
the default branch, and follows that merge-base forward on feature branches
(rebase or merge from the default branch) but never moves backwards.

Closing the diff tab (or quitting hunk) keeps it closed until the cumulative
diff against the baseline changes.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

REF_PREFIX = "refs/worktree/hunk-autodiff/"
TAB_LABEL = "diff"
PANE_NAME = "hunk"
TRIGGER_STATUSES = {"idle", "done"}


def herdr(args: list[str], fatal: bool = True) -> dict[str, Any] | None:
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    result = subprocess.run([binary, *args], text=True, capture_output=True)
    payload = None
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
    if payload is None or "error" in payload:
        if fatal:
            sys.exit(0)
        return None
    return payload


def git(cwd: str, args: list[str]) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return result.stdout if result.returncode == 0 else None


def git_ok(cwd: str, args: list[str]) -> bool:
    return git(cwd, args) is not None


def rev(cwd: str, name: str) -> str | None:
    out = git(cwd, ["rev-parse", "--verify", "--quiet", name + "^{commit}"])
    return out.strip() if out else None


def find_hunk() -> str | None:
    # The herdr server runs hooks with a minimal PATH; check the usual
    # install locations (including bun's global bin) before giving up.
    found = shutil.which("hunk")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/hunk",
        "/usr/local/bin/hunk",
        os.path.expanduser("~/.local/bin/hunk"),
        os.path.expanduser("~/.bun/bin/hunk"),
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


# --- state -----------------------------------------------------------------

def state_dir() -> str:
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp/hunk-autodiff"
    os.makedirs(path, exist_ok=True)
    return path


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def load_tabs() -> dict[str, Any]:
    try:
        with open(os.path.join(state_dir(), "tabs.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_tabs(tabs: dict[str, Any]) -> None:
    path = os.path.join(state_dir(), "tabs.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tabs, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def seen_path(root: str, branch: str) -> str:
    return os.path.join(state_dir(), "seen-" + short_hash(root + "\0" + branch))


def read_seen(root: str, branch: str) -> str:
    try:
        with open(seen_path(root, branch)) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_seen(root: str, branch: str, signature: str) -> None:
    with open(seen_path(root, branch), "w") as f:
        f.write(signature)


def clear_seen(root: str, branch: str) -> None:
    try:
        os.remove(seen_path(root, branch))
    except OSError:
        pass


class Lock:
    """Serialize hook runs so bursts of status events can't open two tabs."""

    def __enter__(self) -> "Lock":
        self.fd = open(os.path.join(state_dir(), "lock"), "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


# --- baseline --------------------------------------------------------------

def current_branch(root: str) -> str:
    out = git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return out.strip() if out else "HEAD-detached"


def baseline_ref(branch: str) -> str:
    return REF_PREFIX + branch


def default_branch(root: str) -> str | None:
    configured = (git(root, ["config", "--get", "hunk-autodiff.base"]) or "").strip()
    candidates = [configured] if configured else []
    origin_head = git(root, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    if origin_head:
        candidates.append(origin_head.strip())
    candidates += ["origin/main", "origin/master", "main", "master"]
    for name in candidates:
        if name and rev(root, name):
            return name
    return None


def is_default_branch(branch: str, base: str | None) -> bool:
    if not base:
        return False
    return branch == base or base.endswith("/" + branch)


def set_baseline(root: str, branch: str, commit: str) -> None:
    git(root, ["update-ref", "-m", "hunk-autodiff baseline", baseline_ref(branch), commit])


def ensure_baseline(root: str, branch: str) -> str | None:
    head = rev(root, "HEAD")
    if not head:
        return None
    stored = rev(root, baseline_ref(branch))
    base = default_branch(root)
    merge_base = None
    if base:
        out = git(root, ["merge-base", "HEAD", base])
        merge_base = out.strip() if out else None

    if stored and git_ok(root, ["merge-base", "--is-ancestor", stored, "HEAD"]):
        # Feature branch picked up newer default-branch commits (rebase or
        # merge): follow the merge-base forward so their changes drop out.
        if (
            merge_base
            and merge_base != stored
            and not is_default_branch(branch, base)
            and git_ok(root, ["merge-base", "--is-ancestor", stored, merge_base])
        ):
            set_baseline(root, branch, merge_base)
            return merge_base
        return stored

    # First sight, or history was rewritten past the old baseline.
    if stored and merge_base and git_ok(root, ["merge-base", "--is-ancestor", merge_base, "HEAD"]):
        commit = merge_base
    elif not stored and merge_base and not is_default_branch(branch, base):
        commit = merge_base
    else:
        commit = head
    set_baseline(root, branch, commit)
    return commit


def diff_signature(root: str, baseline: str) -> str | None:
    """Hash of the cumulative change vs baseline, or None when there is none."""
    diff = git(root, ["diff", "--no-ext-diff", "--find-renames", baseline]) or ""
    status = git(root, ["status", "--porcelain=v1", "--untracked-files=all"]) or ""
    untracked = "".join(line + "\n" for line in status.splitlines() if line.startswith("??"))
    if not diff and not untracked:
        return None
    return hashlib.sha1((baseline + "\0" + diff + "\0" + untracked).encode()).hexdigest()


# --- herdr panes -----------------------------------------------------------

def foreground_cmdlines(pane_id: str) -> list[str]:
    info = herdr(["pane", "process-info", "--pane", pane_id], fatal=False)
    if not info:
        return []
    procs = info["result"]["process_info"].get("foreground_processes") or []
    return [p.get("cmdline") or "" for p in procs]


def hunk_running(pane_id: str) -> bool:
    return any("hunk" in c for c in foreground_cmdlines(pane_id))


def pane_alive(pane_id: str) -> bool:
    return herdr(["pane", "get", pane_id], fatal=False) is not None


def hunk_command(baseline: str) -> str:
    hunk = find_hunk()
    target = baseline[:12]
    if hunk:
        return f"{shlex.quote(hunk)} diff {target} --watch"
    return f"bunx hunkdiff diff {target} --watch"


def launch_hunk(pane_id: str, baseline: str) -> None:
    # `pane run` types the command into the pane's shell; sending it before
    # the shell is ready silently drops the input, so wait, then verify and
    # retry once.
    command = hunk_command(baseline)
    for _ in range(20):
        if foreground_cmdlines(pane_id):
            break
        time.sleep(0.25)
    time.sleep(0.3)
    for _ in range(2):
        herdr(["pane", "run", pane_id, command], fatal=False)
        for _ in range(10):
            time.sleep(0.5)
            if hunk_running(pane_id):
                return


def tab_key(workspace_id: str, root: str) -> str:
    return workspace_id + "|" + root


def close_tab(entry: dict[str, Any]) -> None:
    if entry.get("tab_id"):
        herdr(["tab", "close", entry["tab_id"]], fatal=False)


def open_tab(workspace_id: str, root: str, baseline: str, focus: bool) -> dict[str, Any] | None:
    created = herdr([
        "tab", "create", "--workspace", workspace_id, "--cwd", root,
        "--label", TAB_LABEL, "--focus" if focus else "--no-focus",
    ], fatal=False)
    if not created:
        return None
    result = created["result"]
    entry = {
        "tab_id": result["tab"]["tab_id"],
        "pane_id": result["root_pane"]["pane_id"],
        "baseline": baseline,
    }
    herdr(["pane", "rename", entry["pane_id"], PANE_NAME], fatal=False)
    return entry


def sync_tab(
    workspace_id: str, root: str, baseline: str, *, focus: bool, respect_closed: bool,
    signature: str | None,
) -> None:
    """Make sure the workspace's diff tab shows hunk against `baseline`."""
    branch = current_branch(root)
    tabs = load_tabs()
    key = tab_key(workspace_id, root)
    entry = tabs.get(key)
    alive = bool(entry) and pane_alive(entry["pane_id"])

    if alive and entry.get("baseline") != baseline:
        # hunk can't change its base in place; replace the tab.
        close_tab(entry)
        alive = False

    if alive and hunk_running(entry["pane_id"]):
        if focus:
            herdr(["tab", "focus", entry["tab_id"]], fatal=False)
        return

    if respect_closed and signature is not None and read_seen(root, branch) == signature:
        return

    if not alive:
        if not focus and not entry and live_foreign_session(root):
            return
        entry = open_tab(workspace_id, root, baseline, focus)
        if not entry:
            return
    elif focus:
        herdr(["tab", "focus", entry["tab_id"]], fatal=False)

    # Record before the launch dance so a transient failure there can't leave
    # this diff unrecorded (which would reopen tabs on every idle).
    if signature is not None:
        write_seen(root, branch, signature)
    tabs[key] = entry
    save_tabs(tabs)
    launch_hunk(entry["pane_id"], baseline)


def live_foreign_session(root: str) -> bool:
    """A hunk session the user opened themselves for this repo."""
    hunk = find_hunk()
    if not hunk:
        return False
    probe = subprocess.run(
        [hunk, "session", "get", "--repo", root],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return probe.returncode == 0


# --- entrypoints -----------------------------------------------------------

def repo_root(cwd: str | None) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    root = (git(cwd, ["rev-parse", "--show-toplevel"]) or "").strip()
    return root or None


def on_agent_status(data: dict[str, Any]) -> None:
    if data.get("agent_status") not in TRIGGER_STATUSES:
        return
    pane_id = data.get("pane_id")
    if not pane_id:
        return
    pane = herdr(["pane", "get", pane_id])["result"]["pane"]
    workspace_id = pane.get("workspace_id") or data.get("workspace_id")
    root = repo_root(pane.get("foreground_cwd") or pane.get("cwd"))
    if not root or not workspace_id:
        return
    with Lock():
        branch = current_branch(root)
        baseline = ensure_baseline(root, branch)
        if not baseline:
            return
        signature = diff_signature(root, baseline)
        if signature is None:
            return
        sync_tab(workspace_id, root, baseline, focus=False, respect_closed=True,
                 signature=signature)


def on_worktree_created(data: dict[str, Any]) -> None:
    worktree = data.get("worktree") or {}
    root = repo_root(worktree.get("path"))
    if not root:
        return
    branch = current_branch(root)
    head = rev(root, "HEAD")
    if head and not rev(root, baseline_ref(branch)):
        set_baseline(root, branch, head)


def action_context() -> tuple[str | None, str | None]:
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except json.JSONDecodeError:
        ctx = {}
    cwd = ctx.get("focused_pane_cwd") or ctx.get("workspace_cwd")
    workspace_id = ctx.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    pane_id = ctx.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    if pane_id and (not cwd or not workspace_id):
        pane = (herdr(["pane", "get", pane_id], fatal=False) or {}).get("result", {}).get("pane", {})
        cwd = cwd or pane.get("foreground_cwd") or pane.get("cwd")
        workspace_id = workspace_id or pane.get("workspace_id")
    return repo_root(cwd), workspace_id


def action_open() -> None:
    root, workspace_id = action_context()
    if not root or not workspace_id:
        return
    with Lock():
        baseline = ensure_baseline(root, current_branch(root))
        if baseline:
            sync_tab(workspace_id, root, baseline, focus=True, respect_closed=False,
                     signature=diff_signature(root, baseline))


def action_reset() -> None:
    root, workspace_id = action_context()
    if not root:
        return
    with Lock():
        branch = current_branch(root)
        head = rev(root, "HEAD")
        if not head:
            return
        set_baseline(root, branch, head)
        clear_seen(root, branch)
        if workspace_id:
            tabs = load_tabs()
            entry = tabs.pop(tab_key(workspace_id, root), None)
            if entry:
                close_tab(entry)
                save_tabs(tabs)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "event"
    if command == "open":
        action_open()
        return
    if command == "reset":
        action_reset()
        return
    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except json.JSONDecodeError:
        return
    data = event.get("data") or {}
    name = os.environ.get("HERDR_PLUGIN_EVENT") or event.get("event") or event.get("type")
    if name == "worktree.created" or ("worktree" in data and "agent_status" not in data):
        on_worktree_created(data)
    else:
        on_agent_status(data)


if __name__ == "__main__":
    main()
