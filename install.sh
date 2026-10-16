#!/usr/bin/env bash
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
kittens="$config_home/kitty/kittens"
target="$kittens/histsearch"
conf="$config_home/kitty/kitty.conf"

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

python3 - "$config_home/histsearch/config.toml" "$conf" <<'PY'
import os, re, sys, tomllib
cfg_path, conf = sys.argv[1:3]
try:
    with open(cfg_path, 'rb') as fh:
        key = tomllib.load(fh).get('key', 'ctrl+f')
except FileNotFoundError:
    key = 'ctrl+f'
if not re.fullmatch(r'[\w+\-]+', key):
    sys.exit(f'bad key in {cfg_path}: {key!r}')
line = f'map {key} kitten kittens/histsearch/main.py'
os.makedirs(os.path.dirname(conf), exist_ok=True)
try:
    lines = open(conf).read().splitlines()
except FileNotFoundError:
    lines = []
mine = [i for i, l in enumerate(lines) if 'kittens/histsearch/main.py' in l and l.lstrip().startswith('map ')]
if mine and all(lines[i].strip() == line for i in mine):
    print(f'{conf}: already {line}')
    sys.exit()
if mine:
    lines[mine[0]] = line
    lines = [l for i, l in enumerate(lines) if i not in mine[1:]]
else:
    lines.append(line)
with open(conf, 'w') as fh:
    fh.write('\n'.join(lines) + '\n')
print(f'{conf}: {line}  (reload kitty config: ctrl+shift+f5)')
PY
