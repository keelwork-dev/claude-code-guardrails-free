# How the guardrail hooks work

> **This free repository contains only one of these hooks: `block_risky_shell.py`.** This document comes from the paid Guardrail Team Kit and also describes its other three hooks (`protect_project_boundary.py`, `stop_secret_leaks.py`, `commit_gate.py`) and `.claude/guardrails.json`, which are not included here.

Claude Code runs a **PreToolUse hook** before every tool call that matches the hook's `matcher`. The hook receives a JSON description of the call on standard input. If the hook exits with code `2`, Claude Code cancels the call and shows the hook's error message to Claude, which then has to choose another approach. Exit code `0` lets the call run.

All four hooks in this kit are short Python scripts with no third-party dependencies. They share `guardrail_lib.py` and read their settings from `.claude/guardrails.json`.

## protect_project_boundary.py
- Matcher: `Write|Edit|MultiEdit|NotebookEdit`
- Resolves the target path (including `..` and symbolic links) and blocks it unless it is inside the project folder or one of `extra_writable_paths`.
- Blocks edits to files that match `protected_files` (for example `.env`, `*.pem`) and to anything inside `.git/`. Files ending in `.example`, `.sample` or `.template` are allowed so that Claude can maintain `.env.example`.
- Blocks edits to the guardrails themselves: `.claude/hooks/`, `.claude/settings.json`, `.claude/settings.local.json` and `.claude/guardrails.json`. Otherwise an agent could switch its own safety net off. You change these files yourself.

## block_risky_shell.py
- Matcher: `Bash`
- Splits the command at `&&`, `||`, `;`, `|`, `&`, new lines, subshells `( )`, groups `{ }` and command substitutions `$( )` (text inside quotes is left alone), drops `if/then/do/...` keywords, and checks each simple command.
- Wildcards are expanded like the shell does (`.claude/*`, `.clau*`), and brace expansions such as `settings.json{,.off}` are expanded into both names.
- Literal command strings passed to `bash -c '...'`, `sh -c "..."` or `eval '...'` are checked the same way.
- Blocks: recursive `rm` outside the project, `rm` of `/`, the home folder, the project root or `.git`; `git push --force` (and `+branch`); deleting a protected branch locally or on the remote (`git push origin --delete main`, `git push origin :main`); `git reset --hard`; `git clean -f`; `git checkout .` / `git checkout -- .` and `git restore .`; `curl`/`wget` piped into a shell; `sudo` (unless `allow_sudo`); `chmod -R 777`; `mkfs`; `dd of=/dev/...`; `>` or `>>` redirects to files outside the project (quoted paths with spaces included); `rm`/`mv`/`cp`/`tee`/`sed -i` on the guardrail files or on the `.claude` folder that holds them; `git checkout`/`restore`/`rm`/`mv` of guardrail paths; `xargs rm` (its targets are invisible to the hook).
- Prefixes such as `env`, `sudo`, `command`, `nohup`, `time`, `nice -n N`, `timeout N` and `xargs -n N` / `-I {}` are stripped (with their option values) before the command is checked, so `env git reset --hard` or `nice -n 10 git reset --hard` are caught too.
- Guardrail files are also protected from `unlink`, `shred`, `truncate`, `patch`, `find ... -delete/-exec`, `rsync --delete`, copying a folder over `.claude` (`cp -r other/.claude .`) and `git stash -u` while the guardrail files are untracked. Copying ordinary files into `.claude/` or `.claude/agents/` is allowed; copying the *contents* of another folder over `.claude` (`cp -r tpl/.claude/ .claude`, `rsync -a tpl/.claude/ .claude/`, `ditto tpl .claude`) is blocked.
- Copy and move sources are wildcard-expanded (`cp tpl/.claude/* .claude/`), and `-t DIR` / `--target-directory=DIR` is understood.
- `unzip ... -d DIR` and `tar -x ... -C DIR` are blocked when the archive contains files that would land on the guardrail files (the archive is opened and its file list is checked).
- `find ... -delete` or `find ... -exec rm/mv/sh/...` is blocked when it starts outside the project or inside `.claude`, and when it starts at the project root without the explicit exclusion `-not -path './.claude*'` (or `! -path './.claude*'`). `-prune` is not accepted as an exclusion because it has no effect together with `-delete`, and expressions with `-o` branches are not trusted. The pattern must also match the `.claude` folder itself (`'./.claude*'`), so that `-exec rm -rf` cannot remove the folder as a whole. Starting from a subfolder (`find src ... -delete`) is allowed. This rule is deliberately strict: filters such as `-name` are not trusted to keep away from the hooks.
  - Example: `find . -type d -name __pycache__ -exec rm -rf {} +` is blocked; `find . -type d -name __pycache__ -not -path './.claude*' -exec rm -rf {} +` is allowed.
- `xargs rm` (with or without `-r`) is blocked because its targets are invisible to the hook. `ditto -x` and `unzip` / `tar -x` (including `--file`, `--directory` and `--strip-components`) are checked against the archive's file list. `find . -name '*.pyc' -not -path './.claude*' -delete`, `find src -name '*.pyc' -delete` and read-only `find ... -exec grep` are allowed.
- `tee` and `dd of=` into files outside the project are blocked like `>` redirects; `git push --mirror` counts as a force push; `git push --delete $branch` with a branch from a variable is blocked because it cannot be checked.
- Git global options are skipped before the subcommand is read, so `git -C other/dir push --force` or `git -c k=v reset --hard` are caught too.
- Heredoc bodies (`cat > file <<'EOF' ... EOF`) are treated as data, so text such as `a > b` inside them does not trigger a block.

## stop_secret_leaks.py
- Matchers: `Write|Edit|MultiEdit|NotebookEdit`, `Read|Grep`, `Bash`
- Detects common secret formats: Anthropic/OpenAI-style keys, GitHub tokens, AWS access key IDs, Slack tokens, Google API keys, Stripe live keys, private key blocks, and assignments such as `password = "..."`.
- Blocks writing them into files, pasting them into shell commands, reading or searching protected files with the Read / Grep tools, printing protected files with `cat`/`head`/`grep`..., and printing the whole environment (`env`, `printenv`). Template files (`.env.example`) stay readable.
- A Grep over a whole folder is not blocked; ripgrep skips files listed in `.gitignore`, so keep `.env` in `.gitignore`.

## commit_gate.py
- Matcher: `Bash` (only acts on commands that contain `git commit`)
- Runs before the command, so it inspects what the commit is going to contain: the staged diff, plus all tracked changes for `git commit -a`, plus changes and new files in the paths the same command adds first (`git add . && git commit -m ...`), plus the paths named on the commit itself (`git commit app.py -m ...`).
- Blocks the commit if that content includes a protected file, a secret-looking line, or merge-conflict markers. If git fails while inspecting a repository, the commit is blocked rather than allowed.

## Known limits
The guardrails cannot fully protect themselves. They block direct edits and the common shell ways of deleting, moving or overwriting the guardrail files, but not every possible way. Commit `.claude` to git and check `git status` from time to time; if the hooks were changed, `git diff .claude` shows how.

Hooks inspect the tool call text. A determined program can still reach files in ways the hooks cannot see: for example a Python or Node script that builds a path at run time, `patch` reading a diff from standard input, `eval` of a string built from variables, or a Makefile target. The shell checks cover the common commands and options described above; other tools that write files on their own (for example another archiver, a package manager or a custom script) are not inspected. The kit is designed to stop the common accidental cases; it does not replace operating-system permissions, backups or code review.
