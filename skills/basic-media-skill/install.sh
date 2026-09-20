#!/bin/sh
# Minimal installer: copy this skill into the current user's skills dir(s).
# Canonical install path remains the Claude Code marketplace (`/plugin
# marketplace add .`); this script is the fallback for manual copies.
set -eu

SKILL_NAME="basic-media-skill"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST="$HOME/.claude/skills/$SKILL_NAME"

printf "Copying %s -> %s\n" "$SCRIPT_DIR" "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$SCRIPT_DIR" "$DEST"

# Secondary: OpenCode convention when present.
if [ -d "$HOME/.config/opencode/skills" ]; then
  cp -R "$SCRIPT_DIR" "$HOME/.config/opencode/skills/$SKILL_NAME"
  printf "Also copied to %s\n" "$HOME/.config/opencode/skills/$SKILL_NAME"
fi

printf "Installed %s. Activate with: /%s\n" "$SKILL_NAME" "$SKILL_NAME"