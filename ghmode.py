import base64
import calendar
import glob
import json
import os
import re
import select
import shutil
import subprocess
import textwrap
import time
import tomllib

HOME = os.path.expanduser('~')
XDG_CACHE = os.environ.get('XDG_CACHE_HOME', os.path.join(HOME, '.cache'))
XDG_STATE = os.environ.get('XDG_STATE_HOME', os.path.join(HOME, '.local/state'))
CACHE = os.path.join(XDG_CACHE, 'histsearch', 'gh')
STATE = os.path.join(XDG_STATE, 'histsearch', 'gh.json')
CONFIG = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(HOME, '.config'), 'histsearch', 'config.toml')
PROJECTS = os.path.join(HOME, '.claude', 'projects')
SESSIONS = os.path.join(HOME, '.claude', 'sessions')

STALE_S = 60
REPO_LIST_STALE_S = 86400
COMMITS_STALE_S = 120
SESSION_REPOS_MAX = 6
KINDS = ('all', 'issue', 'pr', 'commit')
SORTS = ('age', 'type', 'id', 'title', 'repo')
DESC_FIRST = ('age', 'id')
FIELDS = 'repository,number,title,labels,updatedAt,createdAt,commentsCount,url,state,isPullRequest,author'

DIM, RESET, BOLD, ACCENT = '\x1b[2m', '\x1b[0m', '\x1b[1m', '\x1b[35m'
COLOURS = {'bug': '\x1b[31m', 'feature': '\x1b[32m', 'chore': '\x1b[34m', 'docs': '\x1b[36m',
           'decision': '\x1b[33m', 'pr': '\x1b[38;5;141m', 'commit': '\x1b[36m', 'issue': ''}
ICONS = {'issue': '', 'pr': '', 'commit': ''}
GITHUB_URL = re.compile(r'github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')
PATH_IN_TEXT = re.compile(r'(?<![\w.:/-])(~?/[\w.@+-]+(?:/[\w.@+-]+)*)')
LINK = re.compile(r'\[([^\]]+)\]\((?:[^)\s]+)\)')
HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
SUMMARY = re.compile(r'<summary>(.*?)</summary>', re.I)
HTML_TAG = re.compile(r'</?[a-zA-Z][^>]*>')
ISSUE_REF = re.compile(r'(?<![\w/.-])((?:[\w.-]+/)?[\w.-]+)?#(\d+)\b')


def path(*parts):
    return os.path.join(CACHE, *parts)


def read_json(p, default=None):
    try:
        with open(p, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def epoch(iso):
    try:
        return calendar.timegm(time.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return 0


def available():
    return shutil.which('gh') is not None


def run_gh(args, timeout=20):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f'gh {args[0]} failed')
    return r.stdout


def owners():
    try:
        with open(CONFIG, 'rb') as fh:
            configured = tomllib.load(fh).get('gh_owners')
    except (OSError, tomllib.TOMLDecodeError):
        configured = None
    if configured:
        return list(configured)
    cached = read_json(path('owner.json'))
    if cached:
        return [cached]
    login = run_gh(['api', 'user', '-q', '.login'], timeout=10).strip()
    write_json(path('owner.json'), login)
    return [login]


def load_state():
    st = read_json(STATE, {}) or {}
    return {'repos': st.get('repos', []), 'roots': st.get('roots', {}), 'repo': st.get('repo', 'all'),
            'kind': st.get('kind', 'all'), 'sort': st.get('sort', 'age'), 'desc': st.get('desc', True),
            'closed': bool(st.get('closed'))}


def save_state(st):
    write_json(STATE, st)


def normalise(raw):
    return {'repo': raw['repository']['nameWithOwner'], 'num': raw['number'], 'title': raw['title'],
            'labels': [l['name'] for l in raw.get('labels') or []], 'updated': epoch(raw.get('updatedAt')),
            'comments': raw.get('commentsCount') or 0, 'url': raw['url'], 'state': raw.get('state', 'open').lower(),
            'pr': bool(raw.get('isPullRequest')), 'author': (raw.get('author') or {}).get('login', '')}


def fetch_items(state):
    items = []
    for owner in owners():
        out = run_gh(['search', 'issues', '--owner', owner, '--include-prs', '--state', state,
                      '--sort', 'updated', '--limit', '300' if state == 'open' else '100', '--json', FIELDS])
        items += [normalise(r) for r in json.loads(out)]
    write_json(path(f'items-{state}.json'), {'at': time.time(), 'items': items})
    return items


def cached_items(state):
    data = read_json(path(f'items-{state}.json'))
    return (data['at'], data['items']) if data else (0, None)


def fetch_repo_list():
    names = []
    for owner in owners():
        out = run_gh(['repo', 'list', owner, '--limit', '300', '--json', 'nameWithOwner'])
        names += [r['nameWithOwner'] for r in json.loads(out)]
    write_json(path('repos.json'), {'at': time.time(), 'repos': names})


def sync(closed=False):
    fetched = sync_commits(load_state())
    for state in ('open', 'closed') if closed else ('open',):
        at, _ = cached_items(state)
        if time.time() - at < STALE_S:
            continue
        fetch_items(state)
        fetched = True
    repos = read_json(path('repos.json'))
    if not repos or time.time() - repos['at'] > REPO_LIST_STALE_S:
        fetch_repo_list()
    return fetched


def active_repos(its):
    out = []
    for it in sorted(its, key=lambda i: -i['updated']):
        if it['repo'] not in out:
            out.append(it['repo'])
    return out


def repo_choices(st):
    _, its = cached_items('open')
    return ['all', *st['repos'], *(r for r in active_repos(its or []) if r not in st['repos'])]


def commit_repos(st, its):
    if st['repo'] != 'all':
        return [st['repo']]
    return st['repos'] or active_repos(its)[:SESSION_REPOS_MAX]


def sync_commits(st):
    if st['repos']:
        return False
    _, its = cached_items('open')
    fetched = False
    for repo in commit_repos(dict(st, repo='all'), its or []):
        data = read_json(path('commits', repo.replace('/', '_') + '.json'))
        if data and time.time() - data['at'] < COMMITS_STALE_S:
            continue
        try:
            remote_commits(repo)
        except (RuntimeError, subprocess.SubprocessError, OSError, ValueError):
            continue
        fetched = True
    return fetched


def items(closed=False):
    at, open_items = cached_items('open')
    if open_items is None and available():
        open_items = fetch_items('open')
        at = time.time()
    out = list(open_items or [])
    if closed:
        _, closed_items = cached_items('closed')
        if closed_items is None and available():
            closed_items = fetch_items('closed')
        out += closed_items or []
    return at, out


def type_of(item):
    if item['pr']:
        return 'pr'
    return next((l for l in item['labels'] if l in COLOURS), 'issue')


def git_root(p):
    p = os.path.abspath(p)
    while p not in ('/', '') and not os.path.exists(p):
        p = os.path.dirname(p)
    while p not in ('/', ''):
        if p != HOME and os.path.exists(os.path.join(p, '.git')):
            return p
        p = os.path.dirname(p)
    return None


def remote_of(root):
    cfg = os.path.join(root, '.git', 'config')
    if os.path.isfile(os.path.join(root, '.git')):
        with open(os.path.join(root, '.git'), encoding='utf-8') as fh:
            gitdir = fh.read().split(':', 1)[1].strip()
        cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.join(root, gitdir))), 'config')
    try:
        with open(cfg, encoding='utf-8') as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text)
    g = GITHUB_URL.search(m.group(1)) if m else None
    return f'{g.group(1)}/{g.group(2)}' if g else None


def known_repos():
    data = read_json(path('repos.json'))
    return {r.split('/')[1].lower(): r for r in (data or {}).get('repos', [])}


def ignored(root, p):
    return subprocess.run(['git', '-C', root, 'check-ignore', '-q', p], capture_output=True, timeout=5).returncode == 0


def resolve(p, known, roots):
    p = os.path.expanduser(p)
    if os.path.exists(p) or os.path.exists(os.path.dirname(p)):
        real = os.path.realpath(p)
        root = git_root(real)
        if root and real != root and ignored(root, real):
            return None
        if root:
            repo = remote_of(root)
            if repo:
                roots[repo] = root
            return repo
        return None
    for part in reversed(p.split('/')):
        if part.lower() in known:
            return known[part.lower()]
    return None


def transcript_paths(transcript):
    found, cwd = [], ''
    with open(transcript, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            m = re.search(r'"cwd":"([^"]+)"', line)
            if m:
                cwd = m.group(1)
            if '"tool_use"' not in line:
                continue
            try:
                content = (json.loads(line).get('message') or {}).get('content') or []
            except ValueError:
                continue
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                    continue
                inp = block.get('input') or {}
                for k in ('file_path', 'notebook_path', 'path'):
                    if isinstance(inp.get(k), str):
                        found.append(inp[k])
                if isinstance(inp.get('command'), str):
                    found += PATH_IN_TEXT.findall(inp['command'])
    return found, cwd


def session_ids(pids):
    sids = []
    for pid in pids:
        meta = read_json(os.path.join(SESSIONS, f'{pid}.json'))
        if meta and meta.get('sessionId'):
            sids.append(meta['sessionId'])
    return sids


def session_repos(pids, cwd):
    known, roots, repos = known_repos(), {}, []
    for sid in session_ids(pids):
        for transcript in glob.glob(os.path.join(PROJECTS, '*', f'{sid}.jsonl')):
            st = os.stat(transcript)
            stamp = f'{st.st_mtime_ns} {st.st_size}'
            cache = read_json(path('sessions', f'{sid}.json'))
            if cache and cache.get('stamp') == stamp and 'cwd' in cache:
                found, last_cwd = cache['paths'], cache['cwd']
            else:
                found, last_cwd = transcript_paths(transcript)
                write_json(path('sessions', f'{sid}.json'), {'stamp': stamp, 'paths': found, 'cwd': last_cwd})
            for p in ([last_cwd] if last_cwd else []) + list(reversed(found)):
                repo = resolve(p, known, roots)
                if repo and repo not in repos:
                    repos.append(repo)
                if len(repos) >= SESSION_REPOS_MAX:
                    break
    if not repos and cwd:
        root = git_root(cwd)
        repo = root and remote_of(root)
        if repo:
            roots[repo] = root
            repos.append(repo)
    return repos, roots


def open_popup(pids, cwd):
    st = load_state()
    st['repos'], st['roots'] = session_repos(pids, cwd)
    st['repo'] = st['repos'][0] if st['repos'] else 'all'
    save_state(st)


def cycle(st, action):
    if action == 'repo':
        choices = repo_choices(st)
        st['repo'] = choices[(choices.index(st['repo']) + 1) % len(choices)] if st['repo'] in choices else 'all'
    elif action == 'kind':
        st['kind'] = KINDS[(KINDS.index(st['kind']) + 1) % len(KINDS)]
    elif action == 'sort':
        st['sort'] = SORTS[(SORTS.index(st['sort']) + 1) % len(SORTS)]
        st['desc'] = st['sort'] in DESC_FIRST
    elif action == 'flip':
        st['desc'] = not st['desc']
    elif action == 'closed':
        st['closed'] = not st['closed']
    save_state(st)


def local_commits(repo, root):
    out = subprocess.run(['git', '-C', root, 'log', '-n', '300', '--format=%H%x1f%ct%x1f%an%x1f%s'],
                         capture_output=True, text=True, timeout=10).stdout
    rows = []
    for line in out.splitlines():
        sha, ts, author, subject = line.split('\x1f', 3)
        rows.append({'sha': sha, 'updated': int(ts), 'author': author, 'title': subject})
    return rows


def remote_commits(repo):
    p = path('commits', repo.replace('/', '_') + '.json')
    data = read_json(p)
    if data and time.time() - data['at'] < COMMITS_STALE_S:
        return data['rows']
    raw = json.loads(run_gh(['api', f'repos/{repo}/commits?per_page=100']))
    rows = [{'sha': c['sha'], 'updated': epoch(c['commit']['author']['date']), 'author': c['commit']['author']['name'],
             'title': c['commit']['message'].split('\n', 1)[0]} for c in raw]
    write_json(p, {'at': time.time(), 'rows': rows})
    return rows


def commit_rows(st, its):
    out = []
    for repo in commit_repos(st, its):
        root = st['roots'].get(repo)
        try:
            if root and os.path.isdir(root):
                commits = local_commits(repo, root)
            elif st['repo'] == 'all':
                commits = (read_json(path('commits', repo.replace('/', '_') + '.json')) or {}).get('rows', [])
            else:
                commits = remote_commits(repo)
        except (RuntimeError, subprocess.SubprocessError, OSError):
            continue
        for c in commits:
            out.append({'kind': 'commit', 'type': 'commit', 'id': c['sha'][:7], 'num': c['updated'],
                        'title': c['title'], 'repo': repo, 'updated': c['updated'], 'comments': 0,
                        'state': 'open', 'key': f'{repo}@{c["sha"]}', 'cwd': root or ''})
    return out


def item_row(it):
    kind = 'pr' if it['pr'] else 'issue'
    return {'kind': kind, 'type': type_of(it), 'id': f'#{it["num"]}', 'num': it['num'], 'title': it['title'],
            'repo': it['repo'], 'updated': it['updated'], 'comments': it['comments'], 'state': it['state'],
            'key': f'{it["repo"]}#{it["num"]}', 'cwd': ''}


def rows(st):
    at, its = items(st['closed'])
    out = commit_rows(st, its) if st['kind'] in ('all', 'commit') else []
    if st['kind'] != 'commit':
        out += [item_row(i) for i in its
                if (st['repo'] == 'all' or i['repo'] == st['repo'])
                and (st['kind'] == 'all' or (st['kind'] == 'pr') == i['pr'])]
    keys = {'age': lambda r: r['updated'], 'type': lambda r: (r['type'], -r['updated']),
            'id': lambda r: (r['repo'], r['num']), 'title': lambda r: r['title'].lower(),
            'repo': lambda r: (r['repo'].lower(), -r['updated'])}
    out.sort(key=keys[st['sort']], reverse=st['desc'])
    return at, out


def short(repo):
    return repo.split('/', 1)[1] if '/' in repo else repo


def filter_line(st, synced_at):
    def group(key, options, on):
        return f'{DIM}{key}{RESET} ' + ' '.join(f'{ACCENT}{BOLD}{o}{RESET}' if o == on else f'{DIM}{o}{RESET}'
                                                for o in options)
    repos = ['all'] + [f'⌂ {short(r)}' for r in st['repos']]
    on = 'all' if st['repo'] == 'all' else f'⌂ {short(st["repo"])}' if st['repo'] in st['repos'] else short(st['repo'])
    if on not in repos:
        repos.append(on)
    rest = len(repo_choices(st)) - len(repos)
    if rest > 0:
        repos.append(f'+{rest}')
    parts = [group('ctrl-t', KINDS, st['kind'])]
    if st['closed']:
        parts.append(f'{ACCENT}closed too{RESET}')
    if not available():
        parts.append(f'{DIM}gh is not installed{RESET}')
    elif synced_at:
        ago = int(time.time() - synced_at)
        parts.append(f'{DIM}synced {ago // 60}m ago{RESET}' if ago >= 60 else f'{DIM}synced {ago}s ago{RESET}')
    return group('ctrl-d', repos, on), '   '.join(parts)


def url(key, kind):
    if kind == 'commit':
        repo, sha = key.split('@', 1)
        return f'https://github.com/{repo}/commit/{sha}'
    repo, num = key.split('#', 1)
    return f'https://github.com/{repo}/{"pull" if kind == "pr" else "issues"}/{num}'


def browse(key, kind):
    subprocess.Popen(['xdg-open', url(key, kind)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def find_item(key):
    for state in ('open', 'closed'):
        _, its = cached_items(state)
        for it in its or ():
            if f'{it["repo"]}#{it["num"]}' == key:
                return it
    return None


def detail(key, kind):
    if kind == 'commit':
        return commit_detail(key)
    repo, num = key.split('#', 1)
    it = find_item(key)
    p = path('detail', f'{repo.replace("/", "_")}_{num}.json')
    cached = read_json(p)
    if cached and it and cached.get('_updated') == it['updated'] and (kind != 'pr' or 'commits' in cached):
        return cached
    fields = 'title,body,labels,author,createdAt,updatedAt,state,comments,url'
    if kind == 'pr':
        fields += ',isDraft,headRefName,baseRefName,additions,deletions,changedFiles,statusCheckRollup,commits'
    data = json.loads(run_gh([kind if kind == 'pr' else 'issue', 'view', num, '-R', repo, '--json', fields]))
    data['_updated'] = epoch(data.get('updatedAt'))
    write_json(p, data)
    return data


def commit_detail(key):
    repo, sha = key.split('@', 1)
    root = load_state()['roots'].get(repo)
    if root and os.path.isdir(root):
        fmt = '%an%x1f%at%x1f%B'
        head = subprocess.run(['git', '-C', root, 'show', '-s', f'--format={fmt}', sha],
                              capture_output=True, text=True, timeout=10).stdout
        author, ts, message = head.split('\x1f', 2)
        stat = subprocess.run(['git', '-C', root, 'show', '--format=', '--numstat', sha],
                              capture_output=True, text=True, timeout=10).stdout
        files = []
        for line in stat.splitlines():
            a, d, name = line.split('\t', 2)
            files.append({'filename': name, 'additions': a, 'deletions': d})
        return {'repo': repo, 'sha': sha, 'author': author, 'ts': int(ts), 'message': message.strip(),
                'files': files, 'root': root}
    p = path('detail', f'{repo.replace("/", "_")}_{sha}.json')
    cached = read_json(p)
    if cached:
        return cached
    raw = json.loads(run_gh(['api', f'repos/{repo}/commits/{sha}']))
    data = {'repo': repo, 'sha': sha, 'author': raw['commit']['author']['name'],
            'ts': epoch(raw['commit']['author']['date']), 'message': raw['commit']['message'].strip(),
            'files': [{'filename': f['filename'], 'additions': f.get('additions', 0),
                       'deletions': f.get('deletions', 0), 'patch': f.get('patch', '')} for f in raw.get('files', [])],
            'root': ''}
    write_json(p, data)
    return data


def when(ts):
    return time.strftime('%d %b %H:%M', time.localtime(ts)) if ts else ''


def styled(lines):
    out, inside = [], False
    for line in lines:
        parts = line.split('`')
        text = (ACCENT if inside else '') + parts[0]
        for part in parts[1:]:
            inside = not inside
            text += (ACCENT if inside else RESET) + part
        out.append(text + RESET)
    return out


def markdown(text, width):
    text = HTML_COMMENT.sub('', (text or '').replace('\r\n', '\n'))
    out, fence = [], False
    for raw in text.split('\n'):
        if not fence:
            raw = HTML_TAG.sub('', SUMMARY.sub(r'▸ \1', LINK.sub(r'\1', raw)))
        if raw.lstrip().startswith('```'):
            fence = not fence
            continue
        if fence:
            out.append(f'{DIM}  {raw[:width - 2]}{RESET}')
            continue
        if raw.startswith('#'):
            out.append(f'{DIM}{raw}{RESET}')
            continue
        if raw.lstrip().startswith('|'):
            out += styled([raw[:width]])
            continue
        m = re.match(r'(\s*)([-*]|\d+\.)\s+(\[[ xX]\]\s+)?(.*)', raw)
        if m:
            indent, box, rest = m.group(1), m.group(3), m.group(4)
            bullet = ('☑' if 'x' in box.lower() else '☐') if box else '·'
            lead = f'{indent}{bullet} '
            wrapped = textwrap.wrap(rest, max(10, width - len(lead))) or ['']
            wrapped = styled(wrapped)
            out.append(f'{indent}{DIM}{bullet}{RESET} ' + wrapped[0])
            out += [' ' * len(lead) + w for w in wrapped[1:]]
            continue
        if not raw.strip():
            if out and out[-1] != '':
                out.append('')
            continue
        out += styled(textwrap.wrap(raw, width))
    while out and out[-1] == '':
        out.pop()
    return out


def checks_line(rollup):
    marks = []
    for c in rollup or []:
        name = c.get('name') or c.get('context') or '?'
        result = (c.get('conclusion') or c.get('state') or c.get('status') or '').upper()
        if result in ('SUCCESS', 'NEUTRAL'):
            marks.append(f'\x1b[32m✓ {name}{RESET}')
        elif result in ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED'):
            marks.append(f'\x1b[31m✗ {name}{RESET}')
        elif result in ('SKIPPED', 'STALE'):
            marks.append(f'{DIM}– {name}{RESET}')
        else:
            marks.append(f'\x1b[33m… {name}{RESET}')
    return '  '.join(marks)


def head_lines(d, key, kind):
    if kind == 'commit':
        return [f'{ACCENT}{BOLD}{d["message"].splitlines()[0]}{RESET}',
                f'{DIM}{short(d["repo"])} · {d["sha"][:7]} · {d["author"]} · {when(d["ts"])}{RESET}']
    repo, num = key.split('#', 1)
    labels = [l['name'] for l in d.get('labels') or []]
    bits = [short(repo), f'{"PR " if kind == "pr" else ""}#{num}', d['state'].lower()]
    bits += [f'{COLOURS.get(l, "")}{l}{RESET}{DIM}' for l in labels]
    if kind == 'pr':
        bits += [f'{d["headRefName"]} → {d["baseRefName"]}', f'+{d["additions"]} −{d["deletions"]}',
                 f'{d["changedFiles"]} files'] + (['draft'] if d.get('isDraft') else [])
    bits += [f'by {(d.get("author") or {}).get("login", "")}', when(epoch(d.get('createdAt')))]
    done = len(re.findall(r'^\s*[-*] \[[xX]\]', d.get('body') or '', re.M))
    todo = done + len(re.findall(r'^\s*[-*] \[ \]', d.get('body') or '', re.M))
    if todo:
        bits.append(f'{done}/{todo} done')
    lines = [f'{ACCENT}{BOLD}{d["title"]}{RESET}', f'{DIM}{" · ".join(bits)}{RESET}']
    if kind == 'pr' and d.get('statusCheckRollup'):
        lines.append(checks_line(d['statusCheckRollup']))
    return lines


def comment_lines(comments, width):
    if not comments:
        return [f'{DIM}no comments{RESET}']
    out = []
    for c in comments:
        out.append(f'{ACCENT}@{(c.get("author") or {}).get("login", "")}{RESET} {DIM}{when(epoch(c.get("createdAt")))}{RESET}')
        out += ['  ' + l for l in markdown(c.get('body', ''), width - 2)]
        out.append('')
    return out[:-1]


def file_lines(files):
    return [f'{f["filename"]} \x1b[32m+{f["additions"]}{RESET} \x1b[31m−{f["deletions"]}{RESET}' for f in files]


def preview(key, kind, width):
    try:
        d = detail(key, kind)
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as e:
        print(f'{DIM}{e}{RESET}')
        return
    for l in head_lines(d, key, kind):
        print(l)
    print()
    if kind == 'commit':
        body = d['message'].split('\n', 1)[1].strip() if '\n' in d['message'] else ''
        for l in markdown(body, width):
            print(l)
        print()
        for l in file_lines(d['files']):
            print(l)
        return
    for l in markdown(d.get('body') or '', width)[:24]:
        print(l)
    comments = d.get('comments') or []
    if comments:
        last = comments[-1]
        print(f'\n{DIM}{len(comments)} comments · latest{RESET} {ACCENT}@{(last.get("author") or {}).get("login", "")}{RESET}'
              f' {DIM}{when(epoch(last.get("createdAt")))}{RESET}')
        for l in markdown(last.get('body', ''), width)[:6]:
            print(l)


def entry(kind, kind_label, ident, title, repo):
    c = COLOURS.get(kind_label, '')
    return f'{c}{ICONS[kind]} {kind_label:<8}{RESET} {DIM}{ident:<8}{RESET} {title}  {DIM}{short(repo)}{RESET}'


def commit_subject(repo, sha):
    root = load_state()['roots'].get(repo)
    if root and os.path.isdir(root):
        out = subprocess.run(['git', '-C', root, 'log', '-1', '--format=%s', sha], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return commit_detail(f'{repo}@{sha}')['message'].split('\n', 1)[0]


def issue_entry(repo, num):
    it = find_item(f'{repo}#{num}')
    if it:
        return entry('pr' if it['pr'] else 'issue', type_of(it), f'#{num}', it['title'], repo), f'{repo}#{num}', 'pr' if it['pr'] else 'issue'
    raw = json.loads(run_gh(['api', f'repos/{repo}/issues/{num}']))
    kind = 'pr' if raw.get('pull_request') else 'issue'
    labels = [l['name'] for l in raw.get('labels') or []]
    label = 'pr' if kind == 'pr' else next((l for l in labels if l in COLOURS), 'issue')
    return entry(kind, label, f'#{num}', raw['title'], repo), f'{repo}#{num}', kind


def linked(key, kind):
    if kind == 'commit':
        repo, sha = key.split('@', 1)
        known, out = known_repos(), []
        for prefix, num in dict.fromkeys(ISSUE_REF.findall(commit_detail(key)['message'])):
            target = prefix if '/' in prefix else known.get(prefix.lower()) if prefix else repo
            if not target:
                continue
            try:
                out.append(issue_entry(target, num))
            except RuntimeError:
                continue
        return list(dict.fromkeys(out))
    repo, num = key.split('#', 1)
    events = json.loads(run_gh(['api', f'repos/{repo}/issues/{num}/timeline?per_page=100']))
    out, seen = [], set()
    for e in events:
        if e.get('commit_id') and e.get('event') in ('referenced', 'closed', 'committed'):
            m = re.search(r'/repos/([^/]+/[^/]+)/commits/', e.get('commit_url') or '')
            r, sha = (m.group(1) if m else repo), e['commit_id']
            if f'{r}@{sha}' not in seen:
                seen.add(f'{r}@{sha}')
                out.append((entry('commit', 'commit', sha[:7], commit_subject(r, sha), r), f'{r}@{sha}', 'commit'))
        elif e.get('event') == 'cross-referenced':
            src = (e.get('source') or {}).get('issue') or {}
            r = (src.get('repository') or {}).get('full_name') or repo
            k = 'pr' if src.get('pull_request') else 'issue'
            labels = [l['name'] for l in src.get('labels') or []]
            label = 'pr' if k == 'pr' else next((l for l in labels if l in COLOURS), 'issue')
            ref = f'{r}#{src.get("number")}'
            if src.get('number') and ref not in seen and ref != key:
                seen.add(ref)
                out.append((entry(k, label, f'#{src["number"]}', src.get('title', ''), r), ref, k))
    return out


def pages(key, kind):
    d = detail(key, kind)
    head = head_lines(d, key, kind)
    if kind == 'commit':
        def message(width):
            return markdown(d['message'], width) + [''] + file_lines(d['files'])

        def diff(width):
            if d['root']:
                out = subprocess.run(['git', '-C', d['root'], 'show', '--color=always', '--format=', d['sha']],
                                     capture_output=True, text=True, timeout=10).stdout
                return out.splitlines()
            out = []
            for f in d['files']:
                out.append(f'{BOLD}{f["filename"]}{RESET}')
                for l in f.get('patch', '').splitlines():
                    colour = '\x1b[32m' if l.startswith('+') else '\x1b[31m' if l.startswith('-') else '\x1b[36m' if l.startswith('@@') else ''
                    out.append(f'{colour}{l}{RESET}')
                out.append('')
            return out
        return head, [('message', 'text', message), ('diff', 'text', diff), ('linked', 'list', lambda: linked(key, kind))]
    repo, num = key.split('#', 1)
    comments = d.get('comments') or []
    tabs = [('body', 'text', lambda width: markdown(d.get('body') or '', width) or [f'{DIM}no description{RESET}']),
            (f'comments {len(comments)}', 'text', lambda width: comment_lines(comments, width))]
    if kind == 'pr':
        commits = d.get('commits') or []
        tabs.append((f'commits {len(commits)}', 'list', lambda: [
            (entry('commit', 'commit', c['oid'][:7], c.get('messageHeadline', ''), repo), f'{repo}@{c["oid"]}', 'commit')
            for c in commits]))
        tabs.append(('diff', 'text', lambda width: run_gh(['pr', 'diff', num, '-R', repo, '--color', 'always']).splitlines()))
    tabs.append(('linked', 'list', lambda: linked(key, kind)))
    return head, tabs


def read_key(fd):
    data = os.read(fd, 64)
    while select.select([fd], [], [], 0.02)[0]:
        data += os.read(fd, 64)
    return data


KEYMAP = {b'\x1b[A': 'up', b'\x1bOA': 'up', b'k': 'up', b'\x1b[B': 'down', b'\x1bOB': 'down', b'j': 'down',
          b'\x1b[5~': 'pgup', b'\x1b[6~': 'pgdn', b' ': 'pgdn', b'\x1b[H': 'home', b'\x1b[1~': 'home',
          b'g': 'home', b'\x1b[F': 'end', b'\x1b[4~': 'end', b'G': 'end', b'\t': 'tab', b'\x1b[Z': 'btab',
          b'\x1b': 'back', b'q': 'back', b'\x03': 'back', b'\x0f': 'browse', b'\x1b\r': 'copy', b'\x1b\n': 'copy',
          b'\r': 'enter', b'\n': 'enter'}


def view(key, kind):
    import termios
    import tty
    fd = os.open('/dev/tty', os.O_RDWR)
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        os.write(fd, b'\x1b[?1049h\x1b[?25l')
        viewer(fd, key, kind)
    finally:
        os.write(fd, b'\x1b[?25h\x1b[?1049l')
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        os.close(fd)


def viewer(fd, key, kind):
    out = lambda s: os.write(fd, s.encode())
    try:
        head, tabs = pages(key, kind)
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as e:
        out(f'\x1b[H\x1b[2J {e}\r\n {DIM}any key{RESET}')
        read_key(fd)
        return
    page, top, note, cache, cursor = 0, 0, '', {}, {}
    while True:
        cols, rows = os.get_terminal_size(fd)
        width = min(cols - 2, 110)
        name, shape, produce = tabs[page]
        error = ''
        if (page, width) not in cache:
            try:
                cache[(page, width)] = produce() if shape == 'list' else produce(width)
            except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as e:
                cache[(page, width)], error = [], str(e)
        content = cache[(page, width)]
        tab_line = '   '.join(f'{ACCENT}{BOLD}{n}{RESET}' if i == page else f'{DIM}{n}{RESET}'
                              for i, (n, _, _) in enumerate(tabs))
        top_lines = head + ['', tab_line, '']
        space = max(1, rows - len(top_lines) - 1)
        if shape == 'list':
            sel = cursor.get(page, 0)
            sel = max(0, min(sel, len(content) - 1))
            cursor[page] = sel
            top = max(0, min(top, sel), sel - space + 1)
            body = [(f'\x1b[7m{label}{RESET}' if i == sel else label) for i, (label, _, _) in enumerate(content)]
            body = body or [f'{DIM}{error or "nothing linked"}{RESET}']
        else:
            body = content or [f'{DIM}{error or "empty"}{RESET}']
            top = max(0, min(top, len(body) - space))
        pct = 100 if len(body) <= space else round(100 * min(len(body), top + space) / len(body))
        keys = [('esc', 'back'), ('tab', 'page')] + ([('↵', 'open')] if shape == 'list' else []) + [
            ('↑↓ pgup pgdn', 'scroll' if shape == 'text' else 'move'), ('ctrl-o', 'open in browser'), ('alt-↵', 'copy url')]
        footer = '   '.join(f'{BOLD}{k}{RESET}{DIM} {v}{RESET}' for k, v in keys) + f'{DIM}   {pct}%{RESET}  {ACCENT}{note}{RESET}'
        screen = top_lines + body[top:top + space]
        screen += [''] * (rows - 1 - len(screen))
        out('\x1b[H' + ''.join(f'\x1b[2K {l}{RESET}\r\n' for l in screen[:rows - 1]) + f'\x1b[2K {footer}')
        k = KEYMAP.get(read_key(fd))
        note = ''
        if k == 'back':
            return
        step = {'up': -1, 'down': 1, 'pgup': -space, 'pgdn': space}.get(k)
        if step and shape == 'list':
            cursor[page] = cursor.get(page, 0) + step
        elif step:
            top += step
        elif k == 'home':
            top, cursor[page] = 0, 0
        elif k == 'end':
            top, cursor[page] = len(body), len(content)
        elif k in ('tab', 'btab'):
            page, top = (page + (1 if k == 'tab' else -1)) % len(tabs), 0
        elif k == 'enter' and shape == 'list' and content:
            _, sub_key, sub_kind = content[cursor[page]]
            viewer(fd, sub_key, sub_kind)
        elif k in ('browse', 'copy'):
            target = content[cursor[page]][1:] if shape == 'list' and content else (key, kind)
            if k == 'browse':
                browse(*target)
            else:
                out('\x1b]52;c;' + base64.b64encode(url(*target).encode()).decode() + '\x07')
            note = 'opened' if k == 'browse' else 'copied'
