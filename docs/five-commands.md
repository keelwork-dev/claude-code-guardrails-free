# Five shell commands I don't let an AI coding agent run - and the small hook that stops them

*Disclosure: this article was written with AI assistance. It was drafted by a Claude Code agent for the Keelwork account, and the account owner approved publishing it. The commands and outputs below were run and copied from real sessions; the hook messages are quoted verbatim.*

Coding agents are useful because they can run commands. They are risky for the same reason. Claude Code asks before it runs most things, but after the tenth harmless `ls` it is tempting to switch to auto-accept, and that is exactly when a single bad command can slip through.

Claude Code has a feature built for this: **hooks**. A `PreToolUse` hook is a program that Claude Code runs *before* a tool call. It receives the call as JSON on standard input. If the hook exits with code `2`, the call is cancelled and the hook's message is shown to the agent, which then has to find another way. Exit code `0` lets the call run.

That is the whole contract. Everything below is about what to put inside it.

## The five commands

These are the commands I treat as "never without a human":

1. **`rm -rf` outside the project** (or of `~`, `/`, the project root, `.git`). One wrong variable and a cleanup step becomes a disaster.
2. **`git push --force`** and deleting `main` on the remote. Rewrites history other people depend on.
3. **`git reset --hard`, `git clean -f`, `git checkout .`** - silently throws away uncommitted work, often the agent's own work from the last hour.
4. **`curl ... | sh`** - runs code nobody has read. Agents love copying install one-liners from READMEs.
5. **`sudo`** - an agent should never need root to edit your project.

## The minimal hook

Here is the core idea in a few lines of Python. `.claude/settings.json`:

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
  { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/guard.py\"" } ] } ] } }
```

`.claude/hooks/guard.py`:

```python
import json, re, sys

event = json.load(sys.stdin)
command = event.get("tool_input", {}).get("command", "")

RULES = [
    (r"\bgit\s+push\b.*(--force|-f\b|--mirror)", "force push rewrites shared history"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard throws away uncommitted work"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh)\b", "piping a download into a shell runs unread code"),
    (r"(^|[;&|]\s*)sudo\b", "sudo is not needed to edit a project"),
]
for pattern, reason in RULES:
    if re.search(pattern, command):
        print(f"BLOCKED - {reason}. Ask the human to run it.", file=sys.stderr)
        sys.exit(2)
sys.exit(0)
```

Test it without starting Claude Code at all - just pipe in the JSON that Claude Code would send:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"git reset --hard HEAD~3"}}' \
  | python3 .claude/hooks/guard.py; echo "exit=$?"
# BLOCKED - git reset --hard throws away uncommitted work. Ask the human to run it.
# exit=2
```

This is worth doing for every rule you add. A hook you have never seen block anything is a hook you are only hoping works.

## Why the minimal version is not enough

Regular expressions over the raw command string break quickly. Once you add rules for deletes and redirects, these are the cases a fuller version has to handle. Some slip past the minimal hook (for example `git -C x push --force` and `env sudo ls` both exit 0), and the last two cause wrong decisions in either direction:

- **`git -C other/dir push --force`** - git's global options come *before* the subcommand, so `git\s+push` never matches.
- **`env sudo ls`**, **`nice -n 10 sudo ...`** - prefixes hide the real command from any rule that expects it at the start of the line (the minimal hook's `sudo` rule lets `env sudo ls` through: exit 0). Unanchored rules have the opposite problem: they also match the words inside quoted text or file names.
- **`bash -c 'rm -rf ~/old'`** - the dangerous part is inside a quoted string.
- **`rm -rf build && rm -rf ../shared`** - the first part is fine, the second is not; you have to split at `&&`, `;`, `|` and check each piece.
- **`cat > notes.md <<'EOF' ... a > b ... EOF`** - the opposite problem: heredoc *text* looks like a redirect and causes false blocks, which trains people to turn the hook off.
- **Paths**: `rm -rf ./build` is fine, `rm -rf ./build/../../` is not. You have to resolve the path against the project folder, including `~` and symlinks, before deciding.

The robust approach is to tokenise the command like a shell does (Python's `shlex` gets you most of the way), split it into simple commands, strip known prefixes and git global options, expand `~` and wildcards, resolve paths, and only then apply rules. The hook used in the next section is about 680 lines of Python across two files, plus a 21-case test file that covers the cases above (each one is a single example, not proof that every variant is caught).

## What it looks like in a real session

I installed a hook built this way as a plugin (`claude -p --plugin-dir ...`, Claude Code 2.1.330, Haiku model) and asked the agent to run `git reset --hard HEAD` in a repository with one uncommitted line. The call never ran. Claude Code returned the hook's message to the agent:

> BLOCKED - `git reset --hard` throws away uncommitted work. Commit or stash first, or run it yourself.

The agent reported that the command was blocked, quoted that message, and did not try another way to discard the change; the uncommitted line was still in the file afterwards. That matters: a good block message tells the agent what to do instead.

## Limits you should know

- A hook reads the **command text**. A Python script that the agent writes and then runs can still delete whatever it likes; the hook only sees `python3 script.py`.
- An agent can edit the hook file itself unless you also block writes to `.claude/hooks/` and `.claude/settings.json`. Commit `.claude/` to git so any change shows up in `git status`.
- Hooks are a safety net for accidents, not a sandbox against an adversary. Backups and version control still do the heavy lifting.

## Source code

The free, MIT-licensed hook I used above - with the parsing described in this article and a 21-case self-test - is this repository (installable as a Claude Code plugin; see the [README](../README.md)).


It is the free part of a paid kit with three more hooks (project boundary, secret leaks, commit gate), subagents and an 85-check self-test: https://keelwork.gumroad.com/l/guardrail-team-kit

*Independent project; not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.*
