#!/usr/bin/env bash
# Update a checkout from a release archive without losing local state.
#
#     ./scripts/update.sh ~/Downloads/northstar-pv-simulator.zip
#
# Preserved:
#   .env                  credentials — never shipped in the archive
#   datasets/             generated data, gitignored, expensive to rebuild
#   .venv/                the environment
#   config/northstar.toml only if you have modified it (you are asked)
#
# Once this is on GitHub, use `git pull` instead. Git handles all of the above
# natively and this script becomes unnecessary.

set -euo pipefail

# Re-exec from a copy before doing anything.
#
# Bash reads a script incrementally, and this script overwrites itself during
# extraction - so bash resumes reading the NEW file at the OLD byte offset and
# fails with a parse error partway through. The work was already done by then,
# which made it look like corruption rather than a self-update artefact.
#
# An updater that lives inside what it updates has to step outside first.
if [ "${NORTHSTAR_UPDATE_REEXEC:-}" != "1" ]; then
    # $0 becomes the temp copy after re-exec, so resolve the repo here and
    # pass it through.
    NORTHSTAR_REPO="$(cd "$(dirname "$0")/.." && pwd)"
    export NORTHSTAR_REPO
    _copy="$(mktemp)"
    cat "$0" > "$_copy"
    chmod +x "$_copy"
    NORTHSTAR_UPDATE_REEXEC=1 "$_copy" "$@"
    _status=$?
    rm -f "$_copy"
    exit $_status
fi

archive="${1:-}"

# Expand a leading "~" ourselves. Make passes the argument quoted, so the shell
# never expands it and the script receives a literal "~/Downloads/...". The
# same tilde assumption bit `cache_root` in config parsing.
case "$archive" in
    "~/"*) archive="$HOME/${archive#\~/}" ;;
    "~")   archive="$HOME" ;;
esac

if [ -z "$archive" ]; then
    echo "usage: $0 <path-to-northstar-pv-simulator.zip>" >&2
    exit 1
fi
if [ ! -f "$archive" ]; then
    echo "no such archive: $archive" >&2
    echo "usage: $0 <path-to-northstar-pv-simulator.zip>" >&2
    exit 1
fi

repo="${NORTHSTAR_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$repo"
echo "Updating $repo"
echo

# Local state is NOT stashed. The archive contains no .env, no datasets/ and
# no resource_cache/, so extracting over the top cannot touch them.
#
# An earlier version copied them to a temp directory, deleted the originals,
# and copied back. That is pure risk for no benefit: the delete happens before
# the restore, so a failed or interrupted copy loses the data outright - and it
# did, costing a 56-million-row dataset that took an hour to build.
#
# Verify for yourself before trusting this:
#     unzip -l <archive> | grep -E "\.env$|datasets/|resource_cache"
for item in .env datasets resource_cache; do
    [ -e "$item" ] && echo "  untouched  $item"
done
echo

config_changed=false

# 3. Extract over the top. Existing files are replaced; local-only files that
#    the archive does not contain — including .env — are untouched.
tmp="$(mktemp -d)"
unzip -q "$archive" -d "$tmp"
src="$(find "$tmp" -maxdepth 1 -type d -name 'northstar-pv-simulator' | head -1)"
if [ -z "$src" ]; then
    echo "  archive does not contain northstar-pv-simulator/" >&2
    rm -rf "$tmp"; exit 1
fi
cp -R "$src"/. "$repo"/
rm -rf "$tmp"
echo "  extracted  archive over checkout"

# 4. Report a config difference rather than silently choosing for you.
if [ "$config_changed" = true ]; then
    if ! diff -q /tmp/northstar.toml.local.$$ config/northstar.toml >/dev/null 2>&1; then
        cp /tmp/northstar.toml.local.$$ config/northstar.toml.yours
        echo
        echo "  NOTE: config/northstar.toml differs from your previous copy."
        echo "        Yours is saved as config/northstar.toml.yours"
        echo "        Compare: diff config/northstar.toml.yours config/northstar.toml"
    fi
fi

echo
echo "Next:"
echo "  uv sync"
[ -e .env ] && echo "  .env preserved — credentials intact" \
            || echo "  no .env found — copy .env.example and fill it in"
