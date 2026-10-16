# histsearch

A kitty kitten that searches your shell history and your Claude Code sessions together. Ctrl+F opens it over the current window, and Enter either puts a command on your prompt or resumes a Claude session.

![histsearch searching for "rebase" across shell history, Claude messages and sessions](docs/screenshot.png)

## What it searches

| Tag | Source | Enter does |
|---|---|---|
| `sh` | your zsh history, from [atuin](https://atuin.sh)'s database | types the command on your prompt, unrun |
| `cc` | your prompts, Claude's replies and the Bash commands it ran, from `~/.claude/projects/*/*.jsonl` | resumes that session |
| `ses` | one row per Claude session, by title, with continuations merged | resumes that session |

Rows are newest first. Search words match exactly, not fuzzily, so prose doesn't drown out results. A red `sh` tag means the command exited non-zero.

## Keys

| Key | |
|---|---|
| Enter | insert the command, or resume the session in a new tab |
| Alt+Enter | copy it instead |
| Tab / Shift+Tab | cycle mode: `all` → `sh` → `cc` → `ses` |
| Ctrl+S / Ctrl+C | toggle shell-only / Claude-only |
| Ctrl+D | only entries from the directory you opened it in |
| Ctrl+P / Ctrl+N | earlier searches |
| Esc | close |

Each new open restores your last query, mode and cursor row.

## Configuration

`~/.config/histsearch/config.toml`, read on every press. Every key is optional; these are the defaults ([`config.example.toml`](config.example.toml)):

```toml
key = "ctrl+f"

passthrough = ["vim", "nvim", "vi", "view", "vimdiff", "gvim", "emacs", "emacsclient"]

herdr = true
```

- `key` opens the popup. After changing it, rerun `./install.sh` and reload kitty's config (Ctrl+Shift+F5).
- `passthrough` lists programs that get the key instead of the popup. Use `[]` to always open the popup.
- `herdr` makes the popup look inside herdr panes: it checks the focused pane against `passthrough`, types commands into that pane, and resumes sessions in a new herdr tab. With `false`, herdr is treated like any other program.

## Install

Requires kitty, fzf (tested with 0.74), Python 3, and atuin hooked into zsh. Claude Code is optional.

```sh
./install.sh
```

That links this checkout to `~/.config/kitty/kittens/histsearch` and adds the key binding to `kitty.conf`, or updates the existing one.

For atuin, put this in `.zshrc`. The `CLAUDECODE` check keeps commands that Claude Code's agents run out of your history:

```sh
[[ -z $CLAUDECODE ]] && eval "$(atuin init zsh --disable-up-arrow --disable-ctrl-r)"
```

## How it works

| File | Runs in | Does |
|---|---|---|
| `main.py` | kitty itself (a no-UI kitten) | checks the foreground process; passes Ctrl+F through to editors or opens `ui.py` |
| `ui.py` | the overlay window | runs fzf, then acts on the choice back inside kitty (`handle_result`) |
| `hs.py` | plain `python3`, called by fzf | builds the index, prints rows and previews, switches modes |

Transcripts are indexed into one TSV per session under `~/.cache/histsearch/cc/`. A session is only re-read when its transcript's size or mtime changes, so after the first build an open takes well under 100 ms. The last search lives in `~/.local/state/histsearch/`. atuin's database is opened read-only.
