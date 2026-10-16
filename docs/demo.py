#!/usr/bin/env python3
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HS = os.path.join(ROOT, 'hs.py')
COLS, ROWS = 150, 17
QUERY = 'rebase'
CURSOR = 4

THEME = {'bg': '#07050e', 'fg': '#abb2bf', 'accent': '#cb6fa1', 'red': '#bd2c40', 'sel': '#1a1623'}

SHELL = [
    (3, 'webapp', 0, 41, 'git rebase -i origin/main'),
    (95, 'webapp', 1, 0.2, 'git rebase --continue'),
    (180, 'webapp', 0, 0.1, 'git status'),
    (900, 'dotfiles', 0, 2.1, 'git pull --rebase'),
    (3000, 'game', 0, 74, 'pnpm test -- --filter worldgen'),
    (26000, 'webapp', 0, 0.4, 'git log --oneline --graph main..feature/seed'),
    (90000, 'api-server', 0, 12, 'git rebase --onto main old-base feature/auth'),
    (200000, 'dotfiles', 128, 0.1, 'git rebase --abort'),
    (400000, '', 0, 0.3, 'ssh-add -l'),
]

SESSIONS = [
    ('webapp', 'Rebase drops worldgen seed commit', 60, [
        ('you', 'why does the rebase drop the worldgen seed commit'),
        ('bash', 'git log --oneline --graph main..feature/seed'),
        ('claude', 'It was already upstream as a squashed commit, so git skips it as empty during the rebase.'),
        ('you', 'ok, can we keep it as a separate commit anyway'),
        ('bash', 'git rebase -i --empty=keep origin/main'),
        ('claude', 'Done. The seed commit is kept as its own empty commit on top of main.'),
    ]),
    ('api-server', 'Auth branch onto new main', 70000, [
        ('you', 'move feature/auth onto the new main without the old base commits'),
        ('bash', 'git rebase --onto main old-base feature/auth'),
        ('claude', 'Rebased 7 commits; the two from old-base are gone.'),
    ]),
    ('dotfiles', 'Pull conflicts in zshrc', 950, [
        ('you', 'git pull --rebase keeps conflicting on .zshrc'),
        ('claude', 'The local copy diverged from the tracked one; a rebase replays your edit on top each time.'),
    ]),
    ('blog', 'Draft post on interactive rebase', 300000, [
        ('you', 'outline a short post explaining interactive rebase for beginners'),
        ('claude', 'Sure: pick, reword, squash and drop cover most day-to-day rebase use.'),
    ]),
]


def make_home(home, now):
    proj = lambda name: os.path.join(home, 'src', name) if name else home
    db_dir = os.path.join(home, '.local/share/atuin')
    os.makedirs(db_dir)
    db = sqlite3.connect(os.path.join(db_dir, 'history.db'))
    db.execute('create table history (id text primary key, timestamp integer not null, duration integer not null, '
               'exit integer not null, command text not null, cwd text not null, session text not null, '
               'hostname text not null, deleted_at integer)')
    for ago, where, ex, dur, cmd in SHELL:
        db.execute('insert into history values (?,?,?,?,?,?,?,?,null)',
                   (uuid.uuid4().hex, int((now - ago) * 1e9), int(dur * 1e9), ex, cmd, proj(where), 's', 'laptop:demo'))
    db.commit()
    db.close()
    for name, title, ago, msgs in SESSIONS:
        d = os.path.join(home, '.claude/projects', '-' + proj(name).strip('/').replace('/', '-'))
        os.makedirs(d, exist_ok=True)
        sid = str(uuid.uuid4())
        with open(os.path.join(d, sid + '.jsonl'), 'w') as fh:
            fh.write(json.dumps({'type': 'ai-title', 'aiTitle': title, 'sessionId': sid}) + '\n')
            for i, (kind, text) in enumerate(msgs):
                ts = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(now - ago - (len(msgs) - i) * 40))
                if kind == 'you':
                    rec = {'type': 'user', 'message': {'content': text}}
                elif kind == 'bash':
                    rec = {'type': 'assistant', 'message': {'content': [
                        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': text}}]}}
                else:
                    rec = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}
                rec.update(timestamp=ts, cwd=proj(name), sessionId=sid)
                fh.write(json.dumps(rec) + '\n')


SGR = re.compile(r'\x1b\[([0-9;]*)m')


def spans(s):
    style, out, pos = set(), [], 0
    for m in SGR.finditer(s):
        if m.start() > pos:
            out.append((frozenset(style), s[pos:m.start()]))
        for code in (m.group(1) or '0').split(';'):
            style = set() if code == '0' else style | {code}
        pos = m.end()
    if pos < len(s):
        out.append((frozenset(style), s[pos:]))
    return out


def render(segs, highlight=''):
    parts = []
    for style, text in segs:
        cls = ' '.join({'1': 'b', '2': 'd', '7': 'inv', '31': 'red', '35': 'acc'}[c] for c in style if c in
                       ('1', '2', '7', '31', '35'))
        chunks = re.split(f'({re.escape(highlight)})', text, flags=re.I) if highlight else [text]
        body = ''.join(f'<span class="hl">{html.escape(c)}</span>' if highlight and c.lower() == highlight.lower()
                       else html.escape(c) for c in chunks)
        parts.append(f'<span class="{cls}">{body}</span>' if cls else body)
    return ''.join(parts)


def main(out_path):
    now = time.time()
    with tempfile.TemporaryDirectory() as home:
        make_home(home, now)
        env = dict(os.environ, HOME=home, XDG_CACHE_HOME=os.path.join(home, '.cache'),
                   XDG_STATE_HOME=os.path.join(home, '.local/state'), HS_COLS=str(COLS))
        run = lambda *a: subprocess.run([sys.executable, HS, *a], env=env, capture_output=True, text=True).stdout
        lines = [l for l in run('list', 'all').splitlines() if QUERY in SGR.sub('', l.split('\t')[4]).lower()]
        total = len(run('list', 'all').splitlines())
        src, key, cwd, meta = lines[CURSOR].split('\t')[:4]
        preview = run('preview', src, key, cwd, meta)
        header = subprocess.run([sys.executable, '-c', f'import sys; sys.path.insert(0, {ROOT!r}); import hs; '
                                 'print(hs.header_for("all"))'], env=env, capture_output=True, text=True).stdout.strip()
    rows = []
    for i, l in enumerate(lines):
        body = render(spans(l.split('\t')[4]), QUERY)
        cur = i == CURSOR
        rows.append(f'<div class="row{" cur" if cur else ""}"><span class="ptr">{"▌" if cur else " "}</span> {body}</div>')
    prev_lines = ''.join(f'<div>{render(spans(l)) or "&nbsp;"}</div>' for l in preview.rstrip('\n').split('\n'))
    t = THEME
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>histsearch</title>
<style>
@font-face {{ font-family: term; src: url(term.ttf); }}
:root {{ --bg:{t['bg']}; --fg:{t['fg']}; --acc:{t['accent']}; --red:{t['red']}; --sel:{t['sel']}; }}
html,body {{ margin:0; background:transparent; }}
.term {{ font: 14px/1.5 term, monospace; color:var(--fg); background:var(--bg); width:{COLS}ch; height:{ROWS * 1.5}em;
  padding:14px 18px; display:flex; white-space:pre; overflow:hidden; border-radius:10px; margin:0; }}
.left {{ flex:0 0 {COLS - (COLS * 44 + 99) // 100}ch; display:flex; flex-direction:column; overflow:hidden; }}
.list {{ flex:1; }}
.right {{ flex:1; border-left:1px solid #2a2433; padding-left:1ch; white-space:pre-wrap; overflow:hidden; }}
.row.cur {{ background:var(--sel); }}
.ptr {{ color:var(--acc); }}
.b {{ font-weight:600; }} .d {{ opacity:.5; }} .red {{ color:var(--red); }} .acc {{ color:var(--acc); }}
.inv {{ background:var(--sel); }}
.hl {{ color:var(--acc); font-weight:600; text-decoration:underline; }}
.prompt {{ display:flex; }} .prompt .p {{ color:var(--acc); }} .info {{ margin-left:auto; opacity:.5; }}
.cursor {{ background:var(--acc); color:var(--bg); }}
.foot {{ opacity:.5; }}
</style></head><body>
<div class="term" id="shot">
  <div class="left">
    <div>{render(spans(header))}</div>
    <div class="prompt"><span class="p">all ❯ </span>{QUERY}<span class="cursor"> </span><span class="info">{len(lines)}/{total}</span></div>
    <div class="list">{''.join(rows)}</div>
    <div class="foot">↵ open  alt-↵ copy  tab mode  ctrl-d here</div>
  </div>
  <div class="right">{prev_lines}</div>
</div>
</body></html>
'''
    with open(out_path, 'w') as fh:
        fh.write(page)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'docs', 'mockup.html'))
