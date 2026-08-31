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

archive="${1:-}"
if [ -z "$archive" ] || [ ! -f "$archive" ]; then
    echo "usage: $0 <path-to-northstar-pv-simulator.zip>" >&2
    exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
echo "Updating $repo"
echo

stash="$(mktemp -d)"
trap 'rm -rf "$stash"' EXIT

# 1. Set aside anything local.
for item in .env datasets resource_cache; do
    if [ -e "$item" ]; then
        cp -R "$item" "$stash/" && echo "  preserved  $item"
    fi
done

# 2. The shipped config is a starting point. If yours differs, keep it and put
#    the new one alongside so the difference is visible rather than silent.
config_changed=false
if [ -f config/northstar.toml ]; then
    cp config/northstar.toml "$stash/northstar.toml.local"
    config_changed=true
fi

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

# 4. Restore.
for item in .env datasets resource_cache; do
    if [ -e "$stash/$item" ]; then
        rm -rf "$repo/$item"
        cp -R "$stash/$item" "$repo"/ && echo "  restored   $item"
    fi
done

# 5. Report a config difference rather than silently choosing for you.
if [ "$config_changed" = true ]; then
    if ! diff -q "$stash/northstar.toml.local" config/northstar.toml >/dev/null 2>&1; then
        cp "$stash/northstar.toml.local" config/northstar.toml.yours
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
