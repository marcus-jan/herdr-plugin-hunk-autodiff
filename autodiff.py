#!/usr/bin/env python3
"""Herdr plugin: keep background hunk review tabs for a checkout.

Each workspace + checkout gets at most one tab of each kind (turn tabs: one
per agent tab), opened in the background whenever an agent finishes a turn:

- pr #<n>:     the open GitHub PR's diff (`gh pr diff`), as GitHub sees it.
- diff:        fallback when there is no open PR: everything changed since the
               branch baseline (`hunk diff <baseline> --watch`).
- unpushed:    local commits not on the upstream branch yet.
- uncommitted: staged, unstaged and untracked changes vs HEAD.
- <tab> · turn: what the agent in <tab> changed during its last turn (frozen
               snapshot). One per agent tab, named after it, so several agent
               tabs in one checkout don't share or clobber each other.

A tab is updated in place when it is already running hunk, (re)opened when
its view has content, and closed when the view becomes empty.

Event hooks
- pane.agent_status_changed: `working` snapshots the working tree for the turn
  tab; `idle`/`done` refreshes every tab for the pane's checkout.
- worktree.created: record the new worktree's starting commit as its baseline.

Actions (argv[1])
- open:  open or refresh every tab for the focused checkout and focus the main
         one (pr, else diff).
- reset: move the baseline for the focused checkout to its current HEAD and
         close its tabs.

The baseline is stored per worktree and branch as a local ref
(refs/worktree/hunk-autodiff/<branch>), so it survives commits, pushes and
gc, and is never pushed. It is inferred on first sight as the merge-base with
the default branch, and follows that merge-base forward on feature branches
(rebase or merge from the default branch) but never moves backwards.
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
import tempfile
import time
from typing import Any

REF_PREFIX = "refs/worktree/hunk-autodiff/"
TURN_REF_PREFIX = "refs/worktree/hunk-autodiff-turn-tab/"
PANE_NAME = "hunk"
TRIGGER_STATUSES = {"idle", "done"}
TURN_START_STATUS = "working"
MAIN_KINDS = ("pr", "diff")
KEEP = object()  # view state unknown (e.g. GitHub unreachable): leave the tab alone


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


def git(cwd: str, args: list[str], env: dict[str, str] | None = None) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )
    return result.stdout if result.returncode == 0 else None


def git_ok(cwd: str, args: list[str]) -> bool:
    return git(cwd, args) is not None


def rev(cwd: str, name: str) -> str | None:
    out = git(cwd, ["rev-parse", "--verify", "--quiet", name + "^{commit}"])
    return out.strip() if out else None


def find_bin(name: str) -> str | None:
    # The herdr server runs hooks with a minimal PATH; check the usual
    # install locations (including bun's global bin) before giving up.
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        os.path.expanduser(f"~/.local/bin/{name}"),
        os.path.expanduser(f"~/.bun/bin/{name}"),
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def find_hunk() -> str | None:
    return find_bin("hunk")


def hunk_prefix() -> str:
    hunk = find_hunk()
    return shlex.quote(hunk) if hunk else "bunx hunkdiff"


# --- state -----------------------------------------------------------------

def state_dir() -> str:
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp/hunk-autodiff"
    os.makedirs(path, exist_ok=True)
    return path


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def tab_key(workspace_id: str, root: str, kind: str) -> str:
    return workspace_id + "|" + root + "|" + kind


def turn_kind(source_tab: str) -> str:
    return "turn|" + source_tab


def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)


def load_tabs() -> dict[str, Any]:
    try:
        with open(os.path.join(state_dir(), "tabs.json")) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # 0.2.x stored one "diff" tab per workspace + checkout.
    for key in [k for k in data if k.count("|") == 1]:
        entry = data.pop(key)
        if isinstance(entry, dict) and entry.get("baseline"):
            entry.setdefault("command", baseline_command(entry["baseline"]))
            entry.setdefault("label", "diff")
        data.setdefault(key + "|diff", entry)
    return data


def save_tabs(tabs: dict[str, Any]) -> None:
    path = os.path.join(state_dir(), "tabs.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tabs, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def patch_path(root: str, kind: str) -> str:
    directory = os.path.join(state_dir(), "patches")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{kind}-{short_hash(root)}.patch")


def write_patch(path: str, content: str) -> None:
    """Rewrite only on change, so `hunk patch --watch` doesn't reload needlessly."""
    try:
        with open(path) as f:
            if f.read() == content:
                return
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


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

def current_branch(root: str) -> str | None:
    out = git(root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return out.strip() if out else None


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


def has_untracked(root: str) -> bool:
    status = git(root, ["status", "--porcelain=v1", "--untracked-files=all"]) or ""
    return any(line.startswith("??") for line in status.splitlines())


def worktree_dirty(root: str) -> bool:
    return bool((git(root, ["status", "--porcelain=v1", "--untracked-files=all"]) or "").strip())


# --- views -----------------------------------------------------------------
# A view is {"label", "command", "patch"?} describing what a tab should show,
# None when it has nothing to show, or KEEP when that can't be determined.

def baseline_command(baseline: str) -> str:
    return f"{hunk_prefix()} diff {baseline[:12]} --watch"


def patch_view(root: str, kind: str, label: str, content: str) -> dict[str, Any] | None:
    if not content.strip():
        return None
    path = patch_path(root, kind)
    return {
        "label": label,
        "command": f"{hunk_prefix()} patch {shlex.quote(path)} --watch",
        "patch": (path, content),
    }


def pr_view(root: str) -> Any:
    gh = find_bin("gh")
    if not gh:
        return None
    try:
        info = subprocess.run(
            [gh, "pr", "view", "--json", "number,state"],
            cwd=root, text=True, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return KEEP
    if info.returncode != 0:
        return None if "no pull requests found" in info.stderr.lower() else KEEP
    try:
        pr = json.loads(info.stdout)
    except json.JSONDecodeError:
        return KEEP
    if pr.get("state") != "OPEN":
        return None
    number = str(pr["number"])
    try:
        diff = subprocess.run(
            [gh, "pr", "diff", number, "--color", "never"],
            cwd=root, text=True, capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return KEEP
    if diff.returncode != 0:
        return KEEP
    return patch_view(root, "pr", f"pr #{number}", diff.stdout)


def diff_view(root: str, baseline: str | None, force: bool) -> dict[str, Any] | None:
    if not baseline:
        return None
    changed = git(root, ["diff", "--no-ext-diff", "--quiet", baseline]) is None
    if not (force or changed or has_untracked(root)):
        return None
    return {"label": "diff", "command": baseline_command(baseline)}


def unpushed_view(root: str) -> dict[str, Any] | None:
    if not rev(root, "@{upstream}"):
        return None
    ahead = (git(root, ["rev-list", "--count", "@{upstream}..HEAD"]) or "0").strip()
    if ahead in ("", "0"):
        return None
    content = git(root, ["diff", "--no-ext-diff", "--find-renames", "@{upstream}...HEAD"]) or ""
    return patch_view(root, "unpushed", "unpushed", content)


def uncommitted_view(root: str) -> dict[str, Any] | None:
    if not rev(root, "HEAD") or not worktree_dirty(root):
        return None
    return {"label": "uncommitted", "command": f"{hunk_prefix()} diff HEAD --watch"}


def snapshot_tree(root: str) -> str | None:
    """Tree object of the whole working tree (incl. untracked), via a temp index."""
    index = (git(root, ["rev-parse", "--path-format=absolute", "--git-path", "index"]) or "").strip()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_index = os.path.join(tmp, "index")
        if index and os.path.exists(index):
            shutil.copyfile(index, tmp_index)  # reuse stat info: only changed files get hashed
        env = {**os.environ, "GIT_INDEX_FILE": tmp_index}
        if git(root, ["add", "-A"], env=env) is None:
            return None
        out = git(root, ["write-tree"], env=env)
    return out.strip() if out else None


def turn_ref(source_tab: str) -> str:
    return TURN_REF_PREFIX + safe_name(source_tab)


def start_turn(root: str, source_tab: str) -> None:
    # Repeated `working` reports within a turn (e.g. after a permission
    # prompt) keep the snapshot from the turn's start.
    if rev(root, turn_ref(source_tab)):
        return
    tree = snapshot_tree(root)
    if not tree:
        return
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "hunk-autodiff", "GIT_AUTHOR_EMAIL": "hunk-autodiff@localhost",
        "GIT_COMMITTER_NAME": "hunk-autodiff", "GIT_COMMITTER_EMAIL": "hunk-autodiff@localhost",
    }
    commit = (git(root, ["commit-tree", tree, "-m", "hunk-autodiff turn start"], env=env) or "").strip()
    if commit:
        git(root, ["update-ref", turn_ref(source_tab), commit])


def turn_view(root: str, source_tab: str, source_label: str) -> Any:
    start = rev(root, turn_ref(source_tab))
    if not start:
        return KEEP  # no snapshot for this turn: keep showing the previous one
    now = snapshot_tree(root)
    if not now:
        return KEEP
    content = git(root, ["diff", "--no-ext-diff", "--find-renames", start, now]) or ""
    git(root, ["update-ref", "-d", turn_ref(source_tab)])
    return patch_view(root, "turn-" + safe_name(source_tab), f"{source_label} · turn", content)


def compute_views(
    workspace_id: str, root: str, branch: str, *, force_main: bool,
    source: tuple[str, str] | None = None,
) -> dict[str, Any]:
    baseline = ensure_baseline(root, branch)
    views: dict[str, Any] = {"pr": pr_view(root)}
    if views["pr"] is KEEP and tab_key(workspace_id, root, "pr") not in load_tabs():
        # gh failed and there's no PR tab to preserve (often: not a GitHub repo).
        views["pr"] = None
    if views["pr"] is KEEP:
        views["diff"] = KEEP
    elif views["pr"] is None:
        views["diff"] = diff_view(root, baseline, force_main)
    else:
        views["diff"] = None
    views["unpushed"] = unpushed_view(root)
    views["uncommitted"] = uncommitted_view(root)
    if source:
        views["turn"] = None  # 0.3.0 kept one shared turn tab per checkout
        views[turn_kind(source[0])] = turn_view(root, *source)
    return views


# --- herdr panes -----------------------------------------------------------

def foreground_processes(pane_id: str) -> list[dict[str, Any]]:
    info = herdr(["pane", "process-info", "--pane", pane_id], fatal=False)
    if not info:
        return []
    return info["result"]["process_info"].get("foreground_processes") or []


def hunk_running(pane_id: str) -> bool:
    return any("hunk" in (p.get("cmdline") or "") for p in foreground_processes(pane_id))


def pane_alive(pane_id: str) -> bool:
    return herdr(["pane", "get", pane_id], fatal=False) is not None


def launch_hunk(pane_id: str, command: str) -> None:
    # `pane run` types the command into the pane's shell; sending it before
    # the shell is ready silently drops the input, so wait, then verify and
    # retry once.
    for _ in range(20):
        if foreground_processes(pane_id):
            break
        time.sleep(0.25)
    time.sleep(0.3)
    for _ in range(2):
        herdr(["pane", "run", pane_id, command], fatal=False)
        for _ in range(10):
            time.sleep(0.5)
            if hunk_running(pane_id):
                return


def close_tab(entry: dict[str, Any]) -> None:
    if entry.get("tab_id"):
        herdr(["tab", "close", entry["tab_id"]], fatal=False)


def open_tab(workspace_id: str, root: str, label: str) -> dict[str, Any] | None:
    created = herdr([
        "tab", "create", "--workspace", workspace_id, "--cwd", root,
        "--label", label, "--no-focus",
    ], fatal=False)
    if not created:
        return None
    result = created["result"]
    entry = {"tab_id": result["tab"]["tab_id"], "pane_id": result["root_pane"]["pane_id"]}
    herdr(["pane", "rename", entry["pane_id"], PANE_NAME], fatal=False)
    return entry


def foreign_session(root: str, tabs: dict[str, Any]) -> bool:
    """A hunk session the user opened themselves for this repo."""
    hunk = find_hunk()
    if not hunk:
        return False
    listed = subprocess.run([hunk, "session", "list", "--json"], text=True, capture_output=True)
    try:
        sessions = json.loads(listed.stdout).get("sessions") or []
    except (json.JSONDecodeError, AttributeError):
        return False
    pids = [s.get("pid") for s in sessions if s.get("repoRoot") == root]
    if not pids:
        return False
    ours = {
        p.get("pid")
        for key, entry in tabs.items()
        if key.split("|")[1:2] == [root] and entry.get("pane_id")
        for p in foreground_processes(entry["pane_id"])
    }
    return any(pid not in ours for pid in pids)


def sync_checkout(workspace_id: str, root: str, views: dict[str, Any], *, focus: bool) -> None:
    tabs = load_tabs()
    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    prefix = tab_key(workspace_id, root, turn_kind(""))
    for key in [k for k in tabs if k.startswith(prefix)]:
        # The agent tab this turn tab belongs to is gone.
        if not herdr(["tab", "get", key[len(prefix):]], fatal=False):
            close_tab(tabs.pop(key))

    for kind, view in views.items():
        key = tab_key(workspace_id, root, kind)
        entry = tabs.get(key)
        if view is KEEP:
            continue
        alive = bool(entry) and pane_alive(entry["pane_id"])
        if view is None:
            if alive:
                close_tab(entry)
            tabs.pop(key, None)
            continue
        if "patch" in view:
            write_patch(*view["patch"])
        if alive and entry.get("command") != view["command"]:
            # hunk can't change its input in place; replace the tab.
            close_tab(entry)
            alive = False
        if alive:
            if entry.get("label") != view["label"]:
                herdr(["tab", "rename", entry["tab_id"], view["label"]], fatal=False)
                entry["label"] = view["label"]
            if not hunk_running(entry["pane_id"]):
                pending.append((key, entry, view))
            continue
        tabs.pop(key, None)
        pending.append((key, {}, view))

    if any(not entry for _, entry, _ in pending) and not focus and foreign_session(root, tabs):
        pending = [p for p in pending if p[1]]

    # Record tabs before the launch dance so a failure there can't orphan them.
    launches = []
    for key, entry, view in pending:
        if not entry:
            entry = open_tab(workspace_id, root, view["label"])
            if not entry:
                continue
        entry.update(label=view["label"], command=view["command"])
        tabs[key] = entry
        launches.append(entry)
    save_tabs(tabs)
    for entry in launches:
        launch_hunk(entry["pane_id"], entry["command"])

    if focus:
        for kind in MAIN_KINDS:
            entry = tabs.get(tab_key(workspace_id, root, kind))
            if entry:
                herdr(["tab", "focus", entry["tab_id"]], fatal=False)
                break


# --- entrypoints -----------------------------------------------------------

def repo_root(cwd: str | None) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    root = (git(cwd, ["rev-parse", "--show-toplevel"]) or "").strip()
    return root or None


def on_agent_status(data: dict[str, Any]) -> None:
    status = data.get("agent_status")
    if status not in TRIGGER_STATUSES and status != TURN_START_STATUS:
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
        if not branch:
            return
        source_tab = pane.get("tab_id")
        if not source_tab:
            return
        if status == TURN_START_STATUS:
            start_turn(root, source_tab)
            return
        tab = (herdr(["tab", "get", source_tab], fatal=False) or {}).get("result", {}).get("tab", {})
        source_label = tab.get("label") or str(tab.get("number") or source_tab)
        views = compute_views(
            workspace_id, root, branch, force_main=False, source=(source_tab, source_label),
        )
        sync_checkout(workspace_id, root, views, focus=False)


def on_worktree_created(data: dict[str, Any]) -> None:
    worktree = data.get("worktree") or {}
    root = repo_root(worktree.get("path"))
    if not root:
        return
    branch = current_branch(root)
    head = rev(root, "HEAD")
    if branch and head and not rev(root, baseline_ref(branch)):
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
        branch = current_branch(root)
        if not branch:
            return
        views = compute_views(workspace_id, root, branch, force_main=True)
        sync_checkout(workspace_id, root, views, focus=True)


def action_reset() -> None:
    root, workspace_id = action_context()
    if not root:
        return
    with Lock():
        branch = current_branch(root)
        head = rev(root, "HEAD")
        if not branch or not head:
            return
        set_baseline(root, branch, head)
        if workspace_id:
            tabs = load_tabs()
            prefix = tab_key(workspace_id, root, "")
            for key in [k for k in tabs if k.startswith(prefix)]:
                close_tab(tabs.pop(key))
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
