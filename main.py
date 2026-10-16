import json
import os
import shutil
import subprocess

from kittens.tui.handler import result_handler
from kitty.constants import config_dir

EDITORS = {'vim', 'nvim', 'vi', 'view', 'vimdiff', 'gvim', 'emacs', 'emacsclient'}
UI = os.path.join(config_dir, 'kittens', 'histsearch', 'ui.py')


def names(procs):
    out = set()
    for p in procs:
        argv = p.get('cmdline') or p.get('argv') or [p.get('name', '')]
        if argv:
            out.add(os.path.basename(argv[0]).lstrip('-'))
        if p.get('name'):
            out.add(p['name'])
    return out


def herdr_focus():
    h = shutil.which('herdr') or os.path.expanduser('~/.local/bin/herdr')
    try:
        r = subprocess.run([h, 'pane', 'process-info'], capture_output=True, text=True, timeout=1)
        info = json.loads(r.stdout)['result']['process_info']
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    procs = info.get('foreground_processes') or []
    cwd = next((p.get('cwd') for p in procs if p.get('cwd')), '')
    return info.get('pane_id', ''), names(procs), cwd


def main(args):
    pass


@result_handler(no_ui=True)
def handle_result(args, answer, target_window_id, boss):
    w = boss.window_id_map.get(target_window_id)
    if w is None:
        return
    fg = names(w.child.foreground_processes)
    pane, cwd = '', w.cwd_of_child or ''
    if 'herdr' in fg:
        focus = herdr_focus()
        if focus:
            pane, fg, cwd = focus[0], focus[1], focus[2] or cwd
    if fg & EDITORS:
        w.send_key('ctrl+f')
        return
    boss.run_kitten_with_metadata(UI, args=[cwd, pane], window=w)
