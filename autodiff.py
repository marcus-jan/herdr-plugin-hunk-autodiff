#!/usr/bin/env python3
"""Herdr event hook: open a hunk diff split when an agent finishes with changes.

Fires on pane.agent_status_changed. When the agent in a pane goes idle and
that pane's cwd is inside a git repo with working-tree changes, opens a split
on the right running `hunk diff --watch` (focus stays on the agent pane).

Skips when:
- a hunk session is already live for the repo (--watch keeps it fresh), or
- the diff is unchanged since the last time a pane was opened for it, so
  closing the hunk pane keeps it closed until new changes appear.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any


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


def find_hunk() -> str | None:
    # The herdr server runs event hooks with a minimal PATH; check the
    # usual install locations before giving up.
    found = shutil.which("hunk")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/hunk",
        "/usr/local/bin/hunk",
        os.path.expanduser("~/.local/bin/hunk"),
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def foreground_cmdlines(pane_id: str) -> list[str]:
    info = herdr(["pane", "process-info", "--pane", pane_id], fatal=False)
    if not info:
        return []
    procs = info["result"]["process_info"].get("foreground_processes") or []
    return [p.get("cmdline") or "" for p in procs]


def launch_hunk(pane_id: str) -> None:
    # `pane run` types the command into the pane's shell; sending it before
    # the shell is ready silently drops the input, so wait, then verify and
    # retry once.
    command = "hunk diff --watch" if find_hunk() else "bunx hunkdiff diff --watch"
    for _ in range(20):
        if foreground_cmdlines(pane_id):
            break
        time.sleep(0.25)
    time.sleep(0.3)
    for _ in range(2):
        herdr(["pane", "run", pane_id, command], fatal=False)
        for _ in range(10):
            time.sleep(0.5)
            if any("hunk" in c for c in foreground_cmdlines(pane_id)):
                return


def main() -> None:
    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except json.JSONDecodeError:
        return
    data = event.get("data") or {}
    if data.get("agent_status") != "idle":
        return
    pane_id = data.get("pane_id")
    if not pane_id:
        return

    pane = herdr(["pane", "get", pane_id])["result"]["pane"]
    cwd = pane.get("foreground_cwd") or pane.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return

    root = (git(cwd, ["rev-parse", "--show-toplevel"]) or "").strip()
    if not root:
        return
    status = git(root, ["status", "--porcelain"])
    if not status or not status.strip():
        return

    # Live hunk session for this repo -> its --watch already tracks changes.
    hunk = find_hunk()
    if hunk:
        probe = subprocess.run(
            [hunk, "session", "get", "--repo", root],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return

    # Same diff as last time we opened a pane -> user closed it on purpose.
    diff = git(root, ["diff", "HEAD"]) or git(root, ["diff"]) or ""
    signature = hashlib.sha1((status + diff).encode()).hexdigest()
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR") or "/tmp"
    sig_path = os.path.join(
        state_dir, "seen-" + hashlib.sha1(root.encode()).hexdigest()[:16]
    )
    try:
        with open(sig_path) as f:
            if f.read().strip() == signature:
                return
    except OSError:
        pass

    split = herdr([
        "pane", "split", pane_id,
        "--direction", "right", "--cwd", root, "--no-focus",
    ])
    hunk_pane = split["result"]["pane"]["pane_id"]
    # Record the signature before the launch dance so a transient failure
    # there can't leave this diff unrecorded (which would reopen panes).
    with open(sig_path, "w") as f:
        f.write(signature)
    herdr(["pane", "rename", hunk_pane, "hunk"], fatal=False)
    launch_hunk(hunk_pane)


if __name__ == "__main__":
    main()
