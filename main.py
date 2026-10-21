import json
import os
import shutil
import subprocess
import tomllib

from kittens.tui.handler import result_handler
from kitty.constants import config_dir

UI = os.path.join(config_dir, 'kittens', 'histsearch', 'ui.py')
CONFIG = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'histsearch', 'config.toml')
DEFAULTS = {
    'key': 'ctrl+f',
    'passthrough': ['vim', 'nvim', 'vi', 'view', 'vimdiff', 'gvim', 'emacs', 'emacsclient'],
    'herdr': True,
}


def load_config():
    try:
        with open(CONFIG, 'rb') as fh:
            user = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        user = {}
    return {k: user.get(k, v) for k, v in DEFAULTS.items()}


def names(procs):
    out = set()
    for p in procs:
        argv = p.get('cmdline') or p.get('argv') or [p.get('name', '')]
        if argv:
            out.add(os.path.basename(argv[0]).lstrip('-'))
        if p.get('name'):
            out.add(p['name'])
    return out


def pids(procs):
    return ','.join(str(p['pid']) for p in procs if p.get('pid'))


def herdr_focus():
    h = shutil.which('herdr') or os.path.expanduser('~/.local/bin/herdr')
    try:
        r = subprocess.run([h, 'pane', 'process-info'], capture_output=True, text=True, timeout=1)
        info = json.loads(r.stdout)['result']['process_info']
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    procs = info.get('foreground_processes') or []
    cwd = next((p.get('cwd') for p in procs if p.get('cwd')), '')
    return info.get('pane_id', ''), names(procs), cwd, pids(procs)


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    w = boss.window_id_map.get(target_window_id)
    if w is None:
        return
    cfg = load_config()
    procs = w.child.foreground_processes
    fg, pid_list = names(procs), pids(procs)
    pane, cwd = '', w.cwd_of_child or ''
    if cfg['herdr'] and 'herdr' in fg:
        focus = herdr_focus()
        if focus:
            pane, fg, cwd, pid_list = focus[0], focus[1], focus[2] or cwd, focus[3]
    if fg & set(cfg['passthrough']):
        w.send_key(cfg['key'])
        return
    boss.run_kitten_with_metadata(UI, args=[cwd, pane, pid_list, boss.listening_on or ''], window=w)
