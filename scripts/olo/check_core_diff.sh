#!/usr/bin/env bash
# Olo M1 core-diff guard: fail on ANY change outside the permitted Olo paths.
#
# Exit codes:
#   0  OK — no Hermes core changes since the base commit
#   1  FAIL — changes outside permitted Olo paths detected
#   2  ERROR — pinned base not in this clone, or HEAD does not descend from base
#
# Usage:
#   scripts/olo/check_core_diff.sh           # check HEAD (committed + staged + unstaged + untracked)
#   scripts/olo/check_core_diff.sh <REF>     # check a specific ref (committed changes only)
#
# Permitted Olo paths (M1):
#   olo/
#   tests/olo/
#   docs/olo/
#   scripts/olo/
#   .github/workflows/olo-guard.yml  (exactly this file, no other workflows)
#
# NOTE: olo/CORE_CHANGES.md is documentation only. This guard never reads it.
# A future milestone that needs a core change will revise this guard under
# architect approval.
set -euo pipefail

BASE_SHA="f65af53f28ebec14c89b7ae603c701cff5faf6f0"   # pinned; the tag is a convenience only
HEAD_REF="${1:-HEAD}"

# ── 1. Verify the base commit is present in this clone ───────────────────────
if ! git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
    echo "ERROR: pinned Hermes base ${BASE_SHA} is not in this clone." \
         "(fetch full history: fetch-depth 0)" >&2
    exit 2
fi

# ── 2. Verify the annotated tag (if present) points at the right commit ──────
if tag_sha=$(git rev-parse -q --verify "upstream-base/f65af53f^{commit}" 2>/dev/null); then
    if [ "$tag_sha" != "$BASE_SHA" ]; then
        echo "ERROR: tag upstream-base/f65af53f points at $tag_sha, not $BASE_SHA" >&2
        exit 2
    fi
fi

# ── 3. Verify HEAD descends from base ────────────────────────────────────────
if ! git merge-base --is-ancestor "$BASE_SHA" "$HEAD_REF" 2>/dev/null; then
    echo "ERROR: $HEAD_REF does not descend from $BASE_SHA" >&2
    exit 2
fi

# ── 4. Collect all changed paths ─────────────────────────────────────────────
# Committed changes since base, plus (when checking HEAD) staged, unstaged, untracked.
changed=$(
    {
        git diff --name-only "$BASE_SHA" "$HEAD_REF"
        if [ "$HEAD_REF" = "HEAD" ]; then
            git diff --name-only --cached
            git diff --name-only
            git ls-files --others --exclude-standard
        fi
    } | sort -u
)

# ── 5. Filter: keep only paths OUTSIDE the permitted Olo paths ───────────────
bad=$(printf '%s\n' "$changed" \
    | grep -v -E '^(olo/|tests/olo/|docs/olo/|scripts/olo/|\.github/workflows/olo-guard\.yml$)' \
    | sed '/^$/d' \
    || true)

# ── 6. Report ─────────────────────────────────────────────────────────────────
if [ -n "$bad" ]; then
    echo "FAIL: changes outside permitted Olo paths (M1 allows none):"
    printf '  %s\n' $bad
    exit 1
fi

echo "OK: no Hermes core changes since ${BASE_SHA}"
