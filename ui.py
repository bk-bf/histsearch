import os
import shutil
import subprocess

from kitty.constants import config_dir

HERE = os.path.join(config_dir, 'kittens', 'histsearch')


def load_hs():
    import importlib.util
    spec = importlib.util.spec_from_file_location('hs', os.path.join(HERE, 'hs.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hs = load_hs()


def herdr_bin():
    return shutil.which('herdr') or os.path.expanduser('~/.local/bin/herdr')


def fzf_args(py, me, st):
    tr = f'{py} {me} transform'
    rem = f'execute-silent({py} {me} remember {{q}})'
    return [
        'fzf', '--ansi', '--scheme=history', '--tiebreak=index', '--layout=reverse',
        '--delimiter=\t', '--with-nth=5', '--no-sort', '--exact',
        '--query', st['query'], '--history', os.path.join(os.path.dirname(hs.STATE), 'queries'),
        '--prompt', hs.prompt_for(st['mode'], st['here']), '--header', hs.header_for(st['mode']), '--header-first',
        '--info=inline-right', '--pointer=▌', '--no-scrollbar', '--border=none',
        '--color=hl:5:underline:bold,hl+:5:underline:bold,pointer:5,prompt:5,bg+:8,info:-1:dim',
        '--preview', f'{py} {me} preview {{1}} {{2}} {{3}} {{4}}',
        '--preview-window', hs.preview_window(st['mode']),
        '--footer', hs.footer_for(st['mode']), '--header-lines', str(hs.header_lines(st['mode'])),
        '--bind', f'tab:transform({tr} next)',
        '--bind', f'btab:transform({tr} prev)',
        '--bind', f'ctrl-s:transform({tr} sh)',
        '--bind', f'ctrl-c:transform({tr} cc)',
        '--bind', f'ctrl-d:transform({tr} here)',
        '--bind', f'load:pos({st["pos"]})+unbind(load)',
        '--bind', f'enter:{rem}+transform({py} {me} enter {{1}} {{2}} {{4}})',
        '--bind', f'ctrl-o:execute-silent({py} {me} browse {{1}} {{2}} {{4}})',
        '--bind', f'ctrl-t:transform({tr} kind)',
        '--bind', f'alt-s:transform({tr} flip)',
        '--bind', f'ctrl-x:transform({tr} closed)',
        '--bind', f'start:bg-transform({py} {me} ghsync)',
        '--bind', f'alt-enter:{rem}+print(alt-enter)+accept',
        '--bind', f'esc:{rem}+abort',
    ]


def main(args):
    cwd = args[1] if len(args) > 1 else ''
    pids = [p for p in (args[3] if len(args) > 3 else '').split(',') if p]
    try:
        hs.ghmode.open_popup(pids, cwd)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    py, me = shutil.which('python3') or '/usr/bin/python3', os.path.join(HERE, 'hs.py')
    st = hs.load_state()
    os.makedirs(os.path.dirname(hs.STATE), exist_ok=True)
    env = dict(os.environ, HS_CWD=cwd, HS_COLS=os.environ.get('OVERLAID_WINDOW_COLS', ''), FZF_DEFAULT_COMMAND=hs.list_command(py, me, st['mode'], st['here'], cwd))
    p = subprocess.run(fzf_args(py, me, st), env=env, stdout=subprocess.PIPE, text=True)
    lines = [l for l in p.stdout.split('\n') if l]
    if p.returncode != 0 or not lines:
        return None
    key = lines[0] if lines[0] == 'alt-enter' else ''
    line = lines[-1]
    src, ident, row_cwd, meta = line.split('\t')[:4]
    if src == 'gh':
        text = hs.ghmode.url(ident, meta)
    elif src == 'sh':
        text = hs.shell_command(ident)
    elif src == 'ses':
        text = f'claude --resume {ident}'
    else:
        _, rows = hs.read_session(ident)
        text = rows[int(meta.split('|')[1])][2]
    if key == 'alt-enter':
        return {'action': 'copy', 'text': text}
    if src == 'sh':
        return {'action': 'insert', 'text': text}
    return {'action': 'resume', 'sid': ident, 'cwd': row_cwd}


def resume_in_herdr(sid, cwd):
    import json
    h = herdr_bin()
    created = subprocess.run([h, 'tab', 'create', '--cwd', cwd, '--focus'],
                             capture_output=True, text=True, timeout=5)
    pane = json.loads(created.stdout)['result']['root_pane']['pane_id']
    subprocess.run([h, 'pane', 'run', pane, f'claude --resume {sid}'], capture_output=True, timeout=5)


def handle_result(args, answer, target_window_id, boss):
    if not answer:
        return
    w = boss.window_id_map.get(target_window_id)
    herdr_pane = args[2] if len(args) > 2 else ''
    action = answer['action']
    if action == 'copy':
        from kitty.clipboard import set_clipboard_string
        set_clipboard_string(answer['text'])
    elif action == 'insert':
        if herdr_pane:
            subprocess.run([herdr_bin(), 'pane', 'send-text', herdr_pane, answer['text']],
                           capture_output=True, timeout=5)
        elif w is not None:
            w.paste_text(answer['text'])
    elif action == 'resume':
        cwd = answer['cwd'] if os.path.isdir(answer['cwd']) else os.path.expanduser('~')
        if herdr_pane:
            resume_in_herdr(answer['sid'], cwd)
        else:
            shell = os.environ.get('SHELL', '/bin/zsh')
            boss.call_remote_control(w, ('launch', '--type=tab', '--cwd', cwd, shell, '-ic',
                                         f'claude --resume {answer["sid"]}; exec {shell}'))
