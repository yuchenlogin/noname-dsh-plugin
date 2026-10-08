#!/usr/bin/env bash
# Sync the vendored kernel snapshot from the kernel submodule.
#
# Two directories, one contract:
#
#   vendor/noname-harness        <- the REAL source: a git submodule pinned to
#                                   an exact kernel commit.  What you change
#                                   here is what gets vendored.
#   vendor/noname_harness_pkg    <- the SHIPPED snapshot: a clean export of
#                                   that submodule commit, because npm/git
#                                   installs do not reliably init submodules.
#
# The old failure this guards against is silent: the snapshot is a hand-made
# copy, so a kernel fix lands in the kernel repo and nobody notices the plugin
# is still shipping the old one.  This script makes "snapshot is stale" a
# command that exits non-zero instead of a bug you find in production.
#
#   npm run sync:kernel                 re-export from the submodule's HEAD
#   npm run sync:kernel -- --commit X   first move the submodule to X
#                                        (fetches it from the kernel remote)
#   npm run sync:kernel -- --dry-run    show what would change, write nothing
#   npm run check:kernel                fail when out of sync / behind upstream
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUBMODULE="vendor/noname-harness"
SNAPSHOT="vendor/noname_harness_pkg"
PKG_DIR="$SNAPSHOT/noname_harness"
VERSION_FILE="$SNAPSHOT/KERNEL_VERSION.txt"
# The submodule's own origin URL (https://github.com/.../NoNameAgentHarness.git),
# read from git so a fork does not have to edit this script.
KERNEL_URL="$(git -C "$REPO_ROOT" config -f .gitmodules submodule."$SUBMODULE".url || true)"
[ -n "$KERNEL_URL" ] || { echo "sync:kernel: no url for $SUBMODULE in .gitmodules" >&2; exit 2; }
# Optional local checkout override for offline development and CI fixtures.
# The public URL remains the source recorded in KERNEL_VERSION.txt.
KERNEL_REMOTE="${NONAME_KERNEL_REPO:-$KERNEL_URL}"

MODE_COMMIT=""
MODE_DRY_RUN=0
MODE_CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) MODE_COMMIT="${2:?--commit needs a value}"; shift 2 ;;
    --commit=*) MODE_COMMIT="${1#*=}"; shift ;;
    --dry-run) MODE_DRY_RUN=1; shift ;;
    --check) MODE_CHECK=1; shift ;;
    -h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "sync:kernel: unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'sync:kernel: %s\n' "$*" >&2; exit 1; }

# --- resolve the target kernel commit ---------------------------------------
# Where the commit comes from, in order of honesty:
#   1. an explicit --commit (fetched into the submodule from the kernel remote)
#   2. the submodule's HEAD, when it is initialised on disk
#   3. the recorded gitlink, when the submodule was never initialised
target_commit=""
if [ -n "$MODE_COMMIT" ]; then
  [ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ] || \
    git -C "$REPO_ROOT" -c "submodule.$SUBMODULE.url=$KERNEL_REMOTE" submodule update --init "$SUBMODULE"
  git -C "$REPO_ROOT/$SUBMODULE" fetch --quiet "$KERNEL_REMOTE" "$MODE_COMMIT" \
    || fail "cannot fetch commit/ref $MODE_COMMIT from $KERNEL_REMOTE"
  # FETCH_HEAD is exactly what that fetch resolved (including a moving ref
  # such as main); checking out the local name could silently select an old
  # branch tip.
  git -C "$REPO_ROOT/$SUBMODULE" checkout --quiet --detach FETCH_HEAD
  target_commit="$(git -C "$REPO_ROOT/$SUBMODULE" rev-parse HEAD)"
elif [ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ]; then
  target_commit="$(git -C "$REPO_ROOT/$SUBMODULE" rev-parse HEAD)"
else
  # Never initialised: the gitlink in the index is the intended pin.
  target_commit="$(git -C "$REPO_ROOT" ls-tree HEAD "$SUBMODULE" | awk '{print $3}')"
fi
[ -n "$target_commit" ] || fail "could not resolve a kernel commit"

if [ "$MODE_DRY_RUN" -eq 1 ] && [ -n "$MODE_COMMIT" ]; then
  fail "--dry-run cannot be combined with --commit (fetching/checking out a commit mutates the submodule); use --dry-run on the current submodule HEAD"
fi
if [ "$MODE_DRY_RUN" -eq 1 ] \
   && [ ! -f "$REPO_ROOT/$SUBMODULE/.git" ] \
   && [ ! -d "$REPO_ROOT/$SUBMODULE/.git" ]; then
  fail "--dry-run requires an initialized submodule and will not initialize it; run git submodule update --init $SUBMODULE first"
fi

# Where the snapshot claims to be.
snapshot_claim() {
  # KERNEL_VERSION.txt is a CLAIM, not proof: `git archive` below is what makes
  # the snapshot's content actually match the named commit.
  sed -n '1s/^NoNameAgentHarness @ //p' "$REPO_ROOT/$VERSION_FILE" 2>/dev/null || true
}

# --- the check mode ----------------------------------------------------------
if [ "$MODE_CHECK" -eq 1 ]; then
  if [ ! -f "$REPO_ROOT/$SUBMODULE/.git" ] && [ ! -d "$REPO_ROOT/$SUBMODULE/.git" ]; then
    fail "check requires initialized submodule $SUBMODULE; run: git submodule update --init $SUBMODULE"
  fi
  status=0
  claim="$(snapshot_claim)"

  # (a) The snapshot's claim must name a real commit that exists somewhere
  #     reachable.  A claim for a commit nobody has is worse than no claim:
  #     it means the file was written by hand, not by this script.
  if [ -z "$claim" ]; then
    say "STALE  no $VERSION_FILE (snapshot has no version claim)"
    status=1
  else
    claim_found=0
    git -C "$REPO_ROOT" cat-file -e "$claim" 2>/dev/null && claim_found=1 || true
    if [ "$claim_found" -eq 0 ] && { [ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ]; }; then
      git -C "$REPO_ROOT/$SUBMODULE" cat-file -e "$claim" 2>/dev/null && claim_found=1 || true
    fi
    if [ "$claim_found" -eq 0 ]; then
      say "STALE  snapshot claims $claim, but that commit is not present locally"
      say "       the version file was not written by sync:kernel; re-export: npm run sync:kernel"
      status=1
    fi
  fi

  # (b) The claim must equal the commit this sync would export.  Comparing
  #     against the resolved target (not the claim) is what catches "somebody
  #     edited the snapshot without re-running the export".  Two directions of
  #     drift get two different remedies:
  if [ -n "$claim" ] && [ "$claim" != "$target_commit" ]; then
    say "STALE  snapshot claims $claim, submodule is at $target_commit"
    # Which side is newer?  If the claim is an ancestor of the submodule HEAD,
    # the snapshot is behind -> re-export.  If the submodule HEAD is an
    # ancestor of the claim (typical: kernel moved on, pin never bumped), the
    # remedy is to move the submodule pin.
    claim_is_ancestor=0
    if [ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ]; then
      git -C "$REPO_ROOT/$SUBMODULE" merge-base --is-ancestor "$claim" "$target_commit" 2>/dev/null && claim_is_ancestor=1 || true
    fi
    if [ "$claim_is_ancestor" -eq 1 ]; then
      say "       snapshot is behind; run: npm run sync:kernel"
    else
      say "       submodule pin is behind the snapshot; run: npm run sync:kernel -- --commit $claim"
    fi
    status=1
  fi

  # (c) Content check: does the snapshot on disk match the claimed commit
  #     byte-for-byte?  Cheap guard against hand edits the claim cannot see.
  #     Skipped when the submodule is not initialised (nothing to diff against).
  if { [ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ]; } \
     && [ -d "$REPO_ROOT/$PKG_DIR" ]; then
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    git -C "$REPO_ROOT/$SUBMODULE" archive --format=tar "$target_commit" noname_harness \
      | tar -x -C "$tmp"
    if ! diff -r --exclude=__pycache__ -q "$tmp/noname_harness" "$REPO_ROOT/$PKG_DIR" >/dev/null 2>&1; then
      say "STALE  snapshot content differs from the kernel at $target_commit"
      say "       run: npm run sync:kernel"
      status=1
    fi
  fi

  # (d) Behind upstream?  Report only -- a plugin may deliberately pin an
  #     older kernel.  Warn, but do not fail, so `check:kernel` stays green on
  #     an intentional pin while still surfacing drift.
  if [ -n "$MODE_COMMIT" ] || [ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ]; then
    if git -C "$REPO_ROOT/$SUBMODULE" fetch --quiet "$KERNEL_REMOTE" main 2>/dev/null; then
      upstream="$(git -C "$REPO_ROOT/$SUBMODULE" rev-parse FETCH_HEAD)"
      if [ "$upstream" != "$target_commit" ]; then
        behind="$(git -C "$REPO_ROOT/$SUBMODULE" rev-list --count "$target_commit..$upstream" 2>/dev/null || echo '?')"
        say "NOTE   kernel upstream main is $behind commit(s) ahead ($upstream)"
        say "       consider: npm run sync:kernel -- --commit main"
      fi
    fi
  fi

  if [ "$status" -eq 0 ]; then
    say "OK     snapshot matches kernel at $target_commit"
  fi
  exit "$status"
fi

# --- the export --------------------------------------------------------------
[ -f "$REPO_ROOT/$SUBMODULE/.git" ] || [ -d "$REPO_ROOT/$SUBMODULE/.git" ] || \
  git -C "$REPO_ROOT" -c "submodule.$SUBMODULE.url=$KERNEL_REMOTE" submodule update --init "$SUBMODULE"

# Export exactly the committed tree -- never the working tree -- so an
# uncommitted experiment cannot leak into a published package.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$REPO_ROOT/$SUBMODULE" archive --format=tar "$target_commit" noname_harness \
  | tar -x -C "$tmp"

if [ "$MODE_DRY_RUN" -eq 1 ]; then
  say "would vendor noname_harness @ $target_commit into $SNAPSHOT"
  if [ -d "$REPO_ROOT/$PKG_DIR" ]; then
    diff -r --exclude=__pycache__ -q "$tmp/noname_harness" "$REPO_ROOT/$PKG_DIR" >/dev/null 2>&1 \
      && say "  (no content change)" \
      || say "  (content WOULD change)"
  else
    say "  (snapshot does not exist yet; would be created)"
  fi
  exit 0
fi

mkdir -p "$SNAPSHOT"
# --delete makes the snapshot a faithful mirror: files removed upstream must
# not linger in the copy.
rsync -a --delete --exclude='__pycache__' "$tmp/noname_harness/" "$REPO_ROOT/$PKG_DIR/"
printf 'NoNameAgentHarness @ %s\n%s/tree/%s\n' \
  "$target_commit" "${KERNEL_URL%.git}" "$target_commit" > "$REPO_ROOT/$VERSION_FILE"

say "vendored noname_harness @ $target_commit"
# Second pair of eyes: the claim must now equal the export source.
[ "$(snapshot_claim)" = "$target_commit" ] || fail "version file mismatch after export"
say "verified: KERNEL_VERSION.txt matches the export"

# The submodule pin moved, but git does not stage that on its own.  Say so,
# because forgetting the gitlink is exactly how the two sources drift apart.
if ! git -C "$REPO_ROOT" diff --quiet HEAD -- "$SUBMODULE" 2>/dev/null \
   || ! git -C "$REPO_ROOT" diff --cached --quiet HEAD -- "$SUBMODULE" 2>/dev/null; then
  say ""
  say "next: git add $SUBMODULE $SNAPSHOT && git commit -m 'chore: 内核快照 -> $target_commit'"
fi
