#!/usr/bin/env python3
import calendar
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghmode

HOME = os.path.expanduser('~')
ATUIN_DB = os.path.join(HOME, '.local/share/atuin/history.db')
PROJECTS = os.path.join(HOME, '.claude/projects')
STATE = os.path.join(os.environ.get('XDG_STATE_HOME', os.path.join(HOME, '.local/state')), 'histsearch', 'last.json')
CACHE = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.join(HOME, '.cache')), 'histsearch', 'cc')
TEXT_CAP = 400
MODES = ('all', 'sh', 'cc', 'ses', 'gh')
ICONS = {'all': '', 'sh': '\ue957 ', 'cc': '\ueac7 ', 'ses': '\uec82 ', 'gh': '\uf408 '}
FOOTERS = {'gh': '↵ view  ctrl-o browser  alt-↵ copy url  ctrl-d repo  ctrl-t kind  ctrl-s sort  alt-s ▲▼  ctrl-x closed  tab mode'}
FOOTER = '↵ open  alt-↵ copy  tab mode  ctrl-d here'
PREVIEW_PCT = 44
ANSI = re.compile(r'\x1b\[[0-9;]*m')
EDITOR_NOISE = re.compile(r'^\s*(<|Stop hook feedback|\[Request interrupted|This session is being continued from a previous)')
INDEX_VERSION = 3

DIM, RESET, BOLD, RED, ACCENT = '\x1b[2m', '\x1b[0m', '\x1b[1m', '\x1b[31m', '\x1b[35m'


def flat(s):
    return ' '.join(s.split())[:TEXT_CAP]


def tilde(p):
    return '~' + p[len(HOME):] if p == HOME or p.startswith(HOME + '/') else p


def short_loc(p):
    p = tilde(p)
    parts = p.split('/')
    if len(parts) > 3:
        p = '/'.join([parts[0]] + [s[:1] for s in parts[1:-1]] + [parts[-1]])
    return p


def age(epoch, now):
    d = max(0, now - epoch)
    for unit, n in (('y', 31536000), ('mo', 2592000), ('w', 604800), ('d', 86400), ('h', 3600), ('m', 60)):
        if d >= n:
            return f'{int(d // n)}{unit}'
    return 'now'


def shell_rows(cwd_filter=None):
    if not os.path.exists(ATUIN_DB):
        return []
    db = sqlite3.connect(f'file:{ATUIN_DB}?mode=ro', uri=True)
    q = ('select id, max(timestamp), duration, exit, command, cwd, hostname, count(*) from history '
         'where deleted_at is null {} group by command')
    args = ()
    if cwd_filter:
        q = q.format('and cwd = ?')
        args = (cwd_filter,)
    else:
        q = q.format('')
    out = []
    for hid, ts, dur, ex, cmd, cwd, host, n in db.execute(q, args):
        out.append((ts / 1e9, 'sh', hid, cwd, f'{ex}|{dur}|{host}|{n}', flat(cmd.replace('\n', ' ↵ ')), ex > 0))
    db.close()
    return out


def parse_ts(s):
    try:
        return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return 0.0


def index_session(path):
    rows, title, cwd, sid = [], '', '', os.path.basename(path)[:-6]
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if '"type":"attachment"' in line[:40]:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get('type')
            if t == 'ai-title':
                title = r.get('aiTitle') or title
                continue
            if t not in ('user', 'assistant') or r.get('isSidechain') or r.get('isMeta'):
                continue
            cwd = cwd or r.get('cwd', '')
            ts = parse_ts(r.get('timestamp'))
            content = (r.get('message') or {}).get('content')
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}]
            for b in content or ():
                bt = b.get('type')
                if t == 'user' and bt == 'text' and not EDITOR_NOISE.match(b.get('text', '')):
                    rows.append((ts, 'you', flat(b['text'])))
                elif t == 'assistant' and bt == 'text' and b.get('text', '').strip():
                    rows.append((ts, 'claude', flat(b['text'])))
                elif t == 'assistant' and bt == 'tool_use' and b.get('name') == 'Bash':
                    rows.append((ts, 'bash', flat(b.get('input', {}).get('command', ''))))
    if title and rows:
        rows.insert(0, (rows[0][0], 'title', flat(title)))
    return sid, cwd, title, rows


def refresh_index():
    os.makedirs(CACHE, exist_ok=True)
    live = set()
    for path in glob.glob(os.path.join(PROJECTS, '*', '*.jsonl')):
        st = os.stat(path)
        stamp = f'v{INDEX_VERSION} {st.st_mtime_ns} {st.st_size}'
        sid = os.path.basename(path)[:-6]
        live.add(sid)
        cache = os.path.join(CACHE, sid + '.tsv')
        try:
            with open(cache, encoding='utf-8') as fh:
                if fh.readline().rstrip('\n') == stamp:
                    continue
        except OSError:
            pass
        sid, cwd, title, rows = index_session(path)
        tmp = cache + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(stamp + '\n')
            fh.write(json.dumps({'cwd': cwd, 'title': title}) + '\n')
            for ts, kind, text in rows:
                fh.write(f'{ts:.0f}\t{kind}\t{text}\n')
        os.replace(tmp, cache)
    for f in glob.glob(os.path.join(CACHE, '*.tsv')):
        if os.path.basename(f)[:-4] not in live:
            os.unlink(f)


def read_session(sid):
    with open(os.path.join(CACHE, sid + '.tsv'), encoding='utf-8') as fh:
        fh.readline()
        meta = json.loads(fh.readline())
        rows = [l.rstrip('\n').split('\t', 2) for l in fh]
    return meta, rows


def claude_rows(cwd_filter=None):
    out = []
    for f in glob.glob(os.path.join(CACHE, '*.tsv')):
        sid = os.path.basename(f)[:-4]
        try:
            meta, rows = read_session(sid)
        except (OSError, ValueError):
            continue
        if cwd_filter and meta['cwd'] != cwd_filter:
            continue
        for i, (ts, kind, text) in enumerate(rows):
            if kind == 'title':
                continue
            shown = f'{DIM}${RESET} {text}' if kind == 'bash' else f'{DIM}{text}{RESET}' if kind == 'claude' else text
            out.append((float(ts), 'cc', sid, meta['cwd'], f'{kind}|{i}', shown, False))
    return out


def session_rows(cwd_filter=None):
    newest = {}
    for f in glob.glob(os.path.join(CACHE, '*.tsv')):
        sid = os.path.basename(f)[:-4]
        try:
            meta, rows = read_session(sid)
        except (OSError, ValueError):
            continue
        if not rows or (cwd_filter and meta['cwd'] != cwd_filter):
            continue
        prompts = [r[2] for r in rows if r[1] == 'you']
        label = meta['title'] or (prompts[0] if prompts else sid[:8])
        row = (max(float(r[0]) for r in rows), 'ses', sid, meta['cwd'], 'session|0', label, False)
        k = (label, meta['cwd'])
        if k not in newest or row[0] > newest[k][0]:
            newest[k] = row
    return list(newest.values())


def cells(s):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 0 if unicodedata.combining(c) else 1
               for c in ANSI.sub('', s))


def fit(s, width):
    if cells(s) <= width:
        return s + ' ' * (width - cells(s))
    out, used, i = [], 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = cells(s[i])
        if used + w > width - 1:
            break
        out.append(s[i])
        used += w
        i += 1
    return ''.join(out) + RESET + '…' + ' ' * (width - used - 1)


def list_width(mode):
    cols = int(os.environ.get('HS_COLS') or 0) or 120
    if has_preview(mode):
        cols -= -(-cols * PREVIEW_PCT // 100) + 1
    return cols - 3


def has_preview(mode):
    return mode != 'ses'


def place(src, cwd):
    if cwd in ('', 'unknown'):
        return ''
    if cwd == HOME:
        return '~'
    return os.path.basename(cwd.rstrip('/')) or cwd


def gh_rows():
    _, items = ghmode.cached_items('open')
    out = []
    for it in items or ():
        r = ghmode.item_row(it)
        out.append((r['updated'], 'gh', r['key'], '', r['kind'], f'{DIM}{r["id"]}{RESET} {r["title"]}', False))
    return out


def gh_column(name, width, st, right=False):
    if st['sort'] != name:
        return DIM + (name.rjust(width) if right else name.ljust(width)) + RESET
    arrow = '▼' if st['desc'] else '▲'
    return ACCENT + (f'{arrow} {name}'.rjust(width) if right else f'{name} {arrow}'.ljust(width)) + RESET


def gh_list():
    st = ghmode.load_state()
    try:
        at, rows = ghmode.rows(st)
        status = ghmode.filter_line(st, at)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        rows, status = [], f'{ghmode.filter_line(st, 0)}   {RED}{e}{RESET}'
    tw = max(10, list_width('gh') - 41)
    now = time.time()
    print(f'gh\t\t\t\t{status}')
    print(f'gh\t\t\t\t  {gh_column("type", 8, st)} {gh_column("id", 8, st)} {gh_column("title", tw, st)} '
          f'{gh_column("repo", 14, st, True)} {gh_column("age", 5, st, True)}')
    for r in rows:
        c = ghmode.COLOURS.get(r['type'], '')
        title = f'{DIM}{r["title"]}{RESET}' if r['state'] != 'open' else r['title']
        if r['comments']:
            title += f' {DIM}[{r["comments"]}]{RESET}'
        repo = ghmode.short(r['repo'])[:14]
        print(f'gh\t{r["key"]}\t{r["cwd"]}\t{r["kind"]}\t{c}{ghmode.ICONS[r["kind"]]}{RESET} {c}{r["type"]:<8}{RESET} '
              f'{DIM}{r["id"]:<8}{RESET} {fit(title, tw)} {DIM}{repo:>14} {age(r["updated"], now):>5}{RESET}')


def cmd_list(mode, cwd_filter):
    if mode == 'gh':
        gh_list()
        return
    rows = []
    if mode in ('all', 'sh'):
        rows += shell_rows(cwd_filter)
    if mode != 'sh':
        refresh_index()
    if mode in ('all', 'cc'):
        rows += claude_rows(cwd_filter)
    if mode in ('all', 'ses'):
        rows += session_rows(cwd_filter)
    if mode == 'all' and not cwd_filter:
        rows += gh_rows()
    rows.sort(key=lambda r: r[0], reverse=True)
    now = time.time()
    width = list_width(mode)
    w = sys.stdout.write
    for ts, src, key, cwd, meta, text, failed in rows:
        colour = RED if failed else ACCENT if src != 'sh' else ''
        tag = f'{colour}{ICONS[src]}{src:<3}{RESET}'
        loc = ghmode.short(key.split('#')[0]) if src == 'gh' else place(src, cwd)
        right = f'{DIM}{loc[:14]:>14} {age(ts, now):>3}{RESET}'
        body = fit(text, max(10, width - cells(tag) - cells(right) - 2))
        w(f'{src}\t{key}\t{cwd}\t{meta}\t{tag} {body} {right}\n')


def shell_command(hid):
    db = sqlite3.connect(f'file:{ATUIN_DB}?mode=ro', uri=True)
    row = db.execute('select command from history where id = ?', (hid,)).fetchone()
    db.close()
    return row[0] if row else ''


def fmt_dur(ns):
    s = int(ns) / 1e9
    if s < 1:
        return f'{s * 1000:.0f}ms'
    if s < 60:
        return f'{s:.1f}s'
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f'{h}h{m}m' if h else f'{m}m{s}s'


def cmd_preview(src, key, cwd, meta):
    if src == 'gh':
        if key:
            ghmode.preview(key, meta, int(os.environ.get('FZF_PREVIEW_COLUMNS') or 60) - 1)
        return
    if src == 'sh':
        ex, dur, host, n = meta.split('|')
        db = sqlite3.connect(f'file:{ATUIN_DB}?mode=ro', uri=True)
        last = db.execute('select max(timestamp) from history where id = ?', (key,)).fetchone()[0]
        db.close()
        exs = f'{RED}{ex}{RESET}' if int(ex) > 0 else ex
        print(shell_command(key))
        print()
        fields = (('dir', tilde(cwd), cwd != 'unknown'), ('exit', exs, int(ex) >= 0),
                  ('took', fmt_dur(dur) if int(dur) > 0 else '', int(dur) > 0),
                  ('when', time.strftime('%Y-%m-%d %H:%M', time.localtime(last / 1e9)), True),
                  ('host', host, True), ('runs', n, True))
        for k, v, known in fields:
            if known:
                print(f'{DIM}{k:<5}{RESET} {v}')
        return
    kind, idx = meta.split('|')
    idx = int(idx)
    m, rows = read_session(key)
    if src == 'ses':
        session_preview(key, m, rows)
        return
    print(f'{ACCENT}{BOLD}{m["title"] or "untitled"}{RESET}')
    ts = float(rows[idx][0]) if rows else 0
    print(f'{DIM}{tilde(m["cwd"])} · {key[:8]} · {age(ts, time.time())} ago{RESET}\n')
    labels = {'you': f'{ACCENT}{BOLD}you   {RESET}', 'claude': f'{DIM}claude{RESET}',
              'bash': f'{DIM}bash  {RESET}', 'title': f'{DIM}title {RESET}'}
    lo, hi = max(0, idx - 3), min(len(rows), idx + 5)
    for i in range(lo, hi):
        _, k, text = rows[i]
        mark = '\x1b[7m' if i == idx else ''
        print(f'{labels[k]} {mark}{text}{RESET}\n')


def session_preview(sid, m, rows):
    first, last = float(rows[0][0]), max(float(r[0]) for r in rows)
    fmt = lambda t: time.strftime('%Y-%m-%d %H:%M', time.localtime(t))
    print(f'{ACCENT}{BOLD}{m["title"] or "untitled"}{RESET}')
    print(f'{DIM}{tilde(m["cwd"])}{RESET}\n')
    for k, v in (('start', fmt(first)), ('last', fmt(last)), ('id', sid)):
        print(f'{DIM}{k:<5}{RESET} {v}')


def preview_window(mode):
    return f'right,{PREVIEW_PCT}%,wrap,border-left' if has_preview(mode) else 'hidden'


def prompt_for(mode, here):
    return f'{mode}{" ⌂" if here and mode != "gh" else ""} ❯ '


def footer_for(mode):
    return FOOTERS.get(mode, FOOTER)


def header_lines(mode):
    return 2 if mode == 'gh' else 0


def header_for(mode):
    return '  '.join(f'{ACCENT}{BOLD}{ICONS[m]}{m}{RESET}' if m == mode else f'{DIM}{ICONS[m]}{m}{RESET}' for m in MODES)


def parse_prompt(prompt):
    words = prompt.split()
    return (words[0] if words and words[0] in MODES else 'all'), '⌂' in prompt


def load_state():
    try:
        with open(STATE, encoding='utf-8') as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    return {'query': st.get('query', ''), 'mode': st.get('mode', 'all'),
            'here': bool(st.get('here')), 'pos': int(st.get('pos') or 1)}


def cmd_remember(query):
    mode, here = parse_prompt(os.environ.get('FZF_PROMPT', ''))
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'query': query, 'mode': mode, 'here': here, 'pos': os.environ.get('FZF_POS', '1')}, fh)
    os.replace(tmp, STATE)


def list_command(py, me, mode, here, cwd):
    return f'{py} {me} list {mode}' + (f' --dir {quote(cwd)}' if here and cwd else '')


GH_ACTIONS = {'here': 'repo', 'sh': 'sort', 'kind': 'kind', 'flip': 'flip', 'closed': 'closed'}


def cmd_transform(action):
    mode, here = parse_prompt(os.environ.get('FZF_PROMPT', 'all ❯ '))
    was = mode
    if mode == 'gh' and action in GH_ACTIONS:
        ghmode.cycle(ghmode.load_state(), GH_ACTIONS[action])
    elif action in ('kind', 'flip', 'closed'):
        return
    elif action == 'refresh':
        pass
    elif action in ('next', 'prev'):
        mode = MODES[(MODES.index(mode) + (1 if action == 'next' else -1)) % len(MODES)]
    elif action in MODES and action != 'all':
        mode = 'all' if mode == action else action
    elif action == 'here':
        here = not here
    cmd = list_command(sys.executable, os.path.abspath(__file__), mode, here, os.environ.get('HS_CWD', ''))
    me = f'{sys.executable} {os.path.abspath(__file__)}'
    sync = f'+bg-transform({me} ghsync)' if mode == 'gh' and was != 'gh' else ''
    keep = '' if action == 'refresh' else '+first'
    print(f'change-prompt({prompt_for(mode, here)})+change-header({header_for(mode)})'
          f'+change-footer({footer_for(mode)})+change-header-lines({header_lines(mode)})'
          f'+change-preview-window({preview_window(mode)})+reload({cmd}){keep}{sync}')


def cmd_ghsync():
    mode, _ = parse_prompt(os.environ.get('FZF_PROMPT', 'all ❯ '))
    if not ghmode.available():
        return
    try:
        fetched = ghmode.sync(ghmode.load_state()['closed'])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        return
    if fetched and mode in ('gh', 'all'):
        print(f'transform({sys.executable} {os.path.abspath(__file__)} transform refresh)')


def cmd_enter(src, key, kind):
    if src == 'gh' and key:
        print(f'execute({sys.executable} {os.path.abspath(__file__)} view {quote(key)} {quote(kind)})')
    elif src != 'gh':
        print('accept')


def quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'list'
    if cmd == 'list':
        mode = argv[2] if len(argv) > 2 else 'all'
        cwd = argv[argv.index('--dir') + 1] if '--dir' in argv else None
        try:
            cmd_list(mode, cwd)
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    elif cmd == 'preview':
        cmd_preview(*argv[2:6])
    elif cmd == 'remember':
        cmd_remember(argv[2] if len(argv) > 2 else '')
    elif cmd == 'transform':
        cmd_transform(argv[2])
    elif cmd == 'ghsync':
        cmd_ghsync()
    elif cmd == 'enter':
        cmd_enter(*argv[2:5])
    elif cmd == 'view':
        ghmode.view(argv[2], argv[3])
    elif cmd == 'browse':
        if argv[2] == 'gh' and argv[3]:
            ghmode.browse(argv[3], argv[4])
    elif cmd == 'index':
        t = time.time()
        refresh_index()
        print(f'{len(glob.glob(os.path.join(CACHE, "*.tsv")))} sessions in {time.time() - t:.1f}s')


if __name__ == '__main__':
    main(sys.argv)
