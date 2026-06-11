#!/bin/bash
#
# Persistent Claude Code "Remote Control" session for the PAOS workspace
# (Obsidian vault / second brain). Single-session (interactive) mode, reachable
# from the Claude app (Code tab). Sub-projects such as Areas/Health & Fitness or
# Noesis are reached by navigating down - their CLAUDE.md files load on demand
# and inherit trust from this root.
#
# Why the single-session FLAG (`--remote-control`) and not server mode
# (`claude remote-control`): server mode is built for spawning many sessions
# from the *web*, but the mobile app cannot reliably create a session in an
# environment. When server mode's session dropped (network blip), the process
# sat at "Capacity 0/32" with no session and no way to recreate one from the
# phone -> messages hung. The flag gives exactly ONE session that IS the
# process, so it is always present, and KeepAlive self-heals: if the session
# ends, the process exits and launchd respawns a fresh one. For fresh context
# from the phone, type /clear in the session.
#
# Why script(1): launchd has no controlling terminal; without a TTY on stdin
# `claude` falls back to --print mode and exits. script(1) gives it a PTY so the
# interactive session runs in the background.
#
# Managed by launchd:  ~/Library/LaunchAgents/paos.claude-remote.plist
# Session name in the Claude app:  "paos"

set -euo pipefail

export PATH="/Users/fink/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd /Users/fink/PAOS

exec /usr/bin/script -q /dev/null \
  /Users/fink/.local/bin/claude --remote-control paos
