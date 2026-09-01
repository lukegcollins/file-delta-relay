#!/usr/bin/env bash
# Copy this branch's freshly generated evidence into the repo-root, per-branch
# snapshot directory a reviewer reads: <repo>/evidence/<branch>/.
#
# Why the snapshot lives at the repo root rather than under sync-demo/: sync-demo
# is a runnable harness whose paths are encoded in seven shell `cd` lines, five
# python HERE/ROOT chains, a compose file, and a Compose-derived network name
# (sync-demo_sync-network). Re-pathing it to nest results per branch would touch
# roughly fifteen sites for no gain. Publishing a copy upward touches none: the
# harness keeps writing exactly where it always did, and the snapshot is a
# read-only record with a commit stamp that makes two branches honestly
# comparable.
#
# Run:  ./evidence/publish.sh            (after a full evidence run)
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
DEST="../evidence/$BRANCH"

mkdir -p "$DEST"/{metrics,plots,logs,reports}

copied=0
copy_glob() {   # copy_glob <dest-subdir> <glob...>
  local sub="$1"; shift
  for f in "$@"; do
    [ -e "$f" ] || continue
    cp "$f" "$DEST/$sub/"
    copied=$((copied + 1))
  done
}

copy_glob metrics evidence/*.json
copy_glob plots   plots/*.png
copy_glob logs    evidence/logs/*.log
copy_glob reports reports/*.md

# The commit stamp is what makes two per-branch directories comparable rather
# than just adjacent: without it there is no way to tell, later, which code
# produced which figure.
{
  echo "branch:    $BRANCH"
  echo "commit:    $(git rev-parse HEAD)"
  echo "generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "dirty:     $(git status --porcelain | wc -l) uncommitted change(s) at publish time"
} > "$DEST/PROVENANCE"

echo "published $copied file(s) to $(cd "$DEST" && pwd)"
cat "$DEST/PROVENANCE"
