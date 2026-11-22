"""Shared code for the Guardrail Team Kit hooks.

Contract (Claude Code PreToolUse hooks):
  stdin  : JSON event with tool_name, tool_input, cwd
  exit 0 : allow the tool call
  exit 2 : block it; the text written to stderr is shown to Claude
Settings live in .claude/guardrails.json next to this folder.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HOOKS_DIR.parent.parent).resolve()
CONFIG_PATH = HOOKS_DIR.parent / "guardrails.json"

DEFAULT_CONFIG = {
    "extra_writable_paths": [],
    "protected_branches": ["main", "master"],
    "allow_force_push": False,
    "allow_sudo": False,
    "protected_files": [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519"],
}

SECRET_PATTERNS = [
    ("Anthropic / OpenAI style key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe secret key", re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hard-coded credential", re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)\b\s*[:=]\s*['\"]([^'\"\s$<{]{8,})['\"]")),
]


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            fail(f"{CONFIG_PATH} is not valid JSON; fix it so the guardrails can load")
    return config


def read_event() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fail("hook received malformed input; refusing to guess")
    return {}


def fail(reason: str) -> None:
    name = Path(sys.argv[0]).stem
    sys.stderr.write(f"[guardrail:{name}] BLOCKED - {reason}\n")
    sys.exit(2)


def ok() -> None:
    sys.exit(0)


def resolve(raw: str, cwd: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


def writable_roots(config: dict) -> list[Path]:
    roots = [PROJECT_DIR]
    for extra in config.get("extra_writable_paths", []):
        roots.append(resolve(extra, PROJECT_DIR))
    return roots


def is_within(p: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def find_secret(text: str) -> str | None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            return label
    return None


def words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n(.*?)\n\s*\2\s*(?=\n|$)", re.DOTALL)


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: they are data written to a file, not shell syntax."""
    return HEREDOC_RE.sub(lambda m: m.group(0).split("\n", 1)[0], command)


SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\n|\||&|\$\(|`|\(|\)|\{|\}")
RESERVED_WORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "case", "esac", "in"}
# Prefix commands that run another command, with the options of each that take a value.
PREFIX_COMMANDS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C"}, "env": {"-u", "-C", "-S"}, "nice": {"-n"}, "nohup": set(),
    "time": set(), "command": set(), "exec": {"-a"}, "timeout": {"-s", "-k"}, "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-n", "-I", "-P", "-L", "-d", "-s", "-E", "-a", "--max-args", "--max-procs", "--delimiter"},
}


def shell_segments(command: str) -> list[str]:
    """Split a command line into simple commands at && || ; | & newlines, subshells,
    command substitutions and braces. Text inside single or double quotes is not split,
    and redirections such as 2>&1 or &> stay intact."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):  # escaped character, e.g. `-exec rm {} \;`
            buf.append(command[i:i + 2])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        is_redirect_amp = (ch == "&" and ((i > 0 and command[i - 1] == ">") or command[i + 1:i + 2] == ">")) or (
            ch == "|" and i > 0 and command[i - 1] == ">")  # `>|` (clobber) is a redirection, not a pipe
        # `{ cmd; }` is a group only when the braces stand alone; `{}` (xargs, find) and `a{b,c}` are words
        prev_ch = command[i - 1] if i > 0 else " "
        next_ch = command[i + 1] if i + 1 < len(command) else " "
        is_brace_word = (ch == "{" and not next_ch.isspace()) or (ch == "}" and not (prev_ch.isspace() or prev_ch == ";"))
        m = None if (is_redirect_amp or is_brace_word) else SEGMENT_SPLIT_RE.match(command, i)
        if m:
            parts.append("".join(buf))
            buf = []
            i = m.end()
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [s.strip() for s in parts if s.strip()]


def simple_command(segment: str) -> tuple[list[str], list[str]]:
    """Return (argv, prefixes): argv of the command that actually runs, after removing
    reserved words (if/then/do...), VAR=value assignments and prefix commands such as
    sudo/env/nice/xargs together with their options; prefixes lists the removed commands."""
    argv = words(segment)
    prefixes: list[str] = []
    changed = True
    while argv and changed:
        changed = False
        while argv and (argv[0] in RESERVED_WORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0])):
            argv = argv[1:]
            changed = True
        if argv and Path(argv[0]).name in PREFIX_COMMANDS:
            name = Path(argv[0]).name
            prefixes.append(name)
            opts = PREFIX_COMMANDS[name]
            argv = argv[1:]
            while argv and argv[0].startswith("-"):
                flag = argv[0]
                argv = argv[1:]
                if flag in opts and argv:
                    argv = argv[1:]
            if name == "timeout" and argv:
                argv = argv[1:]  # the duration
            changed = True
    return argv, prefixes


GLOB_CHARS = re.compile(r"[*?\[]")
SIMPLE_BRACE_RE = re.compile(r"^(.*?)\{([^{}]*,[^{}]*)\}(.*)$")


def expand_braces(word: str) -> list[str]:
    """Shell brace expansion for the common one-level form: a{,.off} -> a, a.off."""
    m = SIMPLE_BRACE_RE.match(word)
    if not m:
        return [word]
    head, body, tail = m.groups()
    out: list[str] = []
    for part in body.split(","):
        out += expand_braces(head + part + tail)
    return out
BRACE_RE = re.compile(r"\{[^}]*\}")


def expand_paths(raw: str, cwd: Path, include_parent: bool = False) -> list[Path]:
    """Resolve a path argument; expand shell wildcards and drop brace groups so that
    `.claude/*`, `.clau*` and `settings.json{,.off}` are checked against real paths."""
    import glob as _glob
    candidates = {raw, BRACE_RE.sub("", raw)}
    result: list[Path] = []
    for c in candidates:
        if GLOB_CHARS.search(c):
            pattern = str(resolve(c, cwd)) if not Path(c).is_absolute() else c
            # like the shell, `*` does not match names starting with a dot unless the pattern does
            result += [Path(m).resolve() for m in _glob.glob(pattern)]
            if include_parent:  # the folder the pattern lives in, e.g. `.claude` for `.claude/*`
                result.append(resolve(GLOB_CHARS.split(c)[0].rsplit("/", 1)[0] or ".", cwd))
        else:
            result.append(resolve(c, cwd))
    return result


# --- shared shell helpers -------------------------------------------------
REDIRECT_RE = re.compile(r"""(?:^|[^<0-9&])(?:\d?>[>|]?|&>>?)\s*(?:"([^"]+)"|'([^']+)'|([^\s;|&'"]+))""")
SAFE_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}


def redirect_targets(command: str) -> list[str]:
    """Files that `>`, `>>`, `2>`, `&>` would write to (quoted paths with spaces included)."""
    found = []
    for m in REDIRECT_RE.finditer(command):
        target = m.group(1) or m.group(2) or m.group(3) or ""
        if target and target not in SAFE_TARGETS and not target.startswith("/dev/fd/"):
            found.append(target)
    return found


# Files that make up the guardrails themselves. Claude must not edit or delete them,
# otherwise it could switch its own safety net off. Change them yourself, outside Claude.
GUARDRAIL_PATHS = (".claude/hooks", ".claude/settings.json", ".claude/settings.local.json", ".claude/guardrails.json")


def is_guardrail_file(p: Path) -> bool:
    for rel in GUARDRAIL_PATHS:
        target = (PROJECT_DIR / rel).resolve()
        if p == target or is_within(p, [target]):
            return True
    return False


def touches_guardrails(p: Path) -> bool:
    """True if modifying, moving or deleting p would change a guardrail file:
    p is a guardrail file, lies inside one, or is a folder that contains one (e.g. `.claude`)."""
    if is_guardrail_file(p):
        return True
    if p == PROJECT_DIR or not is_within(p, [PROJECT_DIR]):
        return False  # the project root and folders above it are handled by other checks
    for rel in GUARDRAIL_PATHS:
        target = (PROJECT_DIR / rel).resolve()
        if is_within(target, [p]):
            return True
    return False


def is_git_internal(p: Path) -> bool:
    git_dir = (PROJECT_DIR / ".git").resolve()
    return p == git_dir or is_within(p, [git_dir])


GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    """Skip git's global options (-C <path>, -c k=v, --git-dir=..., --no-pager ...) and
    return (subcommand, remaining arguments)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in GIT_OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    return "", []
