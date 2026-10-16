#!/usr/bin/env bash
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
kittens="${XDG_CONFIG_HOME:-$HOME/.config}/kitty/kittens"
target="$kittens/histsearch"

for dep in kitty fzf python3; do
	command -v "$dep" >/dev/null || { echo "missing: $dep" >&2; exit 1; }
done
command -v atuin >/dev/null || echo "atuin not found: shell history will be empty" >&2

mkdir -p "$kittens"
if [[ -e $target && ! -L $target ]]; then
	echo "$target exists and is not a symlink; move it away first" >&2
	exit 1
fi
ln -sfn "$here" "$target"
echo "linked $target -> $here"

conf="${XDG_CONFIG_HOME:-$HOME/.config}/kitty/kitty.conf"
grep -q 'kittens/histsearch/main.py' "$conf" 2>/dev/null ||
	echo "add to $conf:  map ctrl+f kitten kittens/histsearch/main.py"
