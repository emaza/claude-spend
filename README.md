# claude-spend

Visualize Claude Code token usage in simple tables, either through an interactive terminal view or a local web page. In both interactive views, you can click on a column header to sort the data. Claude Code token usage is tracked and displayed, broken down by agent, project, model, day, session, and task.

> **Unofficial.** This is an independent project. It is not affiliated with, endorsed by or supported by Anthropic. "Claude" and "Claude Code" are trademarks of their owners.

Everything runs on your machine: it reads the transcripts Claude Code already keeps in `~/.claude/projects` and writes only to `~/.claude/token-tracker/`. Nothing is sent anywhere.

![The claude-spend web page: a sortable table of estimated spend by agent, with tabs for project, model, day, session and task](docs/screenshot.png)

## Requirements
Python 3.9 or newer. No third-party packages. The interactive terminal view needs `curses`, which comes with Python on Linux and macOS but not on Windows.

## Platform support
So far claude-spend has only been tested on **Linux**, with Python 3.11, 3.12 and 3.14. It only uses the standard library, so it should work on macOS and Windows too, but I haven't been able to try it there.

If you run it on another system, I'd really appreciate your help testing it. Let me know how it goes, whether that's "it works" or a bug report, by opening an issue or a pull request. Improvements of any kind are welcome.

## Files
- `tracker.py`: the log and the report (standard library only).
- `prices.json`: list prices per model, in USD per million tokens, with the date they were taken. A model can have `tiers`: a different price for the requests whose prompt is longer than a number of tokens (for example Haiku 5.5 above 100,000). It is decided request by request and a model can have several tiers.
- `claude-spend`: wrapper script that runs the report.
- `hooks.settings.json`: the `Stop` and `SubagentStop` hooks to add to `~/.claude/settings.json`.

## Installation
```bash
git clone https://github.com/emaza/claude-spend.git
cd claude-spend
mkdir -p ~/.claude/token-tracker ~/.local/bin
cp tracker.py prices.json ~/.claude/token-tracker/
cp claude-spend ~/.local/bin/ && chmod +x ~/.local/bin/claude-spend
```
Then add the `hooks` block from `hooks.settings.json` to `~/.claude/settings.json`. Merge it with what you already have; do not replace the file. Always install `tracker.py` and `prices.json` together.

These steps are for Linux, and should also work on macOS (untested).

### Windows (untested)
The steps below have not been tested on Windows. Please open an issue if something doesn't work.

```powershell
git clone https://github.com/emaza/claude-spend.git
cd claude-spend
New-Item -ItemType Directory -Force "$HOME\.claude\token-tracker" | Out-Null
Copy-Item tracker.py, prices.json "$HOME\.claude\token-tracker\"
```

The `claude-spend` wrapper is a shell script, so on Windows run the tracker directly (use `py -3` instead of `python` if that's how you start Python):

```powershell
python "$HOME\.claude\token-tracker\tracker.py" --web
```

For the hooks, copy the `hooks` block from `hooks.settings.json` as above, but change the command so it works on your system, with the full path to the script. For example: `python "C:/Users/you/.claude/token-tracker/tracker.py" hook`.

The interactive terminal view (`--interactive`) needs `curses`. You can try `pip install windows-curses`. Without it, claude-spend prints the plain report instead, and `--web` works either way.

## Usage
```bash
claude-spend --import            # load the transcripts that are already on disk
claude-spend                     # tables by agent and by project
claude-spend --by all            # every table (the session table includes the start date, in local time, and the title; the title only appears in records made after it was added: `--import` fills it in for old sessions)
claude-spend --by day --since 2026-11-01 --project my-project
claude-spend --by session --sort usd:asc
claude-spend --interactive       # tables in the terminal; click a header to sort
claude-spend --web               # local web page; click a header to sort
```

| Option | Meaning |
|---|---|
| `report` (default), `hook`, `import` | what to do; `hook` is for the hooks, `import` is the same as `--import` and exits |
| `--by` | grouping: `agent`, `project`, `model`, `day`, `session`, `task` or `all`; can be repeated; default `agent` and `project` |
| `--since YYYY-MM-DD` | only records from that day on |
| `--project text` | only projects whose path contains the text |
| `--sort COLUMN[:asc\|desc]` | sort by `group` (or the grouping name), `date`, `title`, `runs`, `input`, `output`, `cache_read`, `cache_write` or `usd`. Without `:asc` or `:desc`, text sorts ascending and numbers and dates descending. Without `--sort`, `day` is sorted by date and everything else by USD, highest first |
| `--interactive` | terminal view (see below) |
| `--web` | web page on `127.0.0.1` |
| `--port N` | port for `--web` (default 8765) |
| `--no-open` | with `--web`, do not open the browser |

### Interactive terminal view
Click a column header to sort by it; click it again to reverse the order. A double click sorts once. Keyboard fallback for terminals without mouse support: `←` `→` choose a column, `Enter` or `s` sorts, `Tab` switches table, `↑` `↓` `PgUp` `PgDn` `Home` `End` scroll, `q` quits. `--sort` sets the initial order. If the output is not a terminal, it prints the plain report instead.

Mouse clicks depend on the terminal: most modern ones work, but some report double clicks differently, which is why a single click is enough.

### Web page
`claude-spend --web` serves the report on `http://127.0.0.1:8765` and opens it in your default browser. It only listens on your own machine and only reads the log; reloading the page (or pressing Refresh) recalculates it from the current data. The tables can be filtered by date and project. Press `Ctrl+C` to stop it.

## Data
The log is stored in `~/.claude/token-tracker/token-log.jsonl`. That file has your data and is not uploaded to the repository. Records written by earlier versions, which used Spanish keys, are read transparently.

The amounts are an estimate at list price. If you use a Pro or Max subscription, it is not what you pay. Prices change: check `prices.json` against [Anthropic's pricing page](https://platform.claude.com/docs/en/about-claude/pricing) and update it (and `_price_date`) when they do; the report warns when the prices are more than 90 days old.

## Tests
```bash
python3 -m unittest discover -s tests
```
The tests use a temporary home directory (they set both `HOME` and `USERPROFILE`, and stop if that doesn't take effect), so they never touch your real `~/.claude`.

## Contributing
Bug reports, ideas and pull requests are all welcome. Reports from macOS and Windows are especially useful, since that's where it hasn't been tested. If you send a pull request, please run the tests first.

## License
[MIT](LICENSE)
