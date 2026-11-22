#!/usr/bin/env python3
"""Guardrail 2 - stop shell commands that are hard to undo.

Blocks Bash commands that:
  - delete recursively outside the project, or delete /, the home folder, the project root or .git
  - force-push (including --mirror), or delete a protected branch (locally or on the remote)
  - run `git reset --hard`, `git clean -f`, `git checkout .`, `git restore .` (discard all work)
  - pipe a downloaded script straight into a shell (curl ... | sh)
  - use sudo (unless allow_sudo is true), chmod -R 777, mkfs, dd to a device
  - write (redirect, tee, dd) into a file outside the project
  - modify, move, overwrite or delete the guardrail files themselves, or the .claude folder
Commands inside ( ), { }, $( ), if/then, for/do, after a single &, behind prefixes such as
env, nice, nohup, timeout or xargs, and inside literal `bash -c '...'` / `eval '...'` strings
are checked too. Wildcards and brace expansions are expanded before checking.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # do not leave __pycache__ inside the project
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guardrail_lib import (GUARDRAIL_PATHS, PROJECT_DIR, expand_braces, expand_paths, fail,  # noqa: E402
                           git_subcommand, is_git_internal, is_within, load_config, ok, read_event,
                           redirect_targets, resolve, shell_segments, simple_command, strip_heredocs,
                           touches_guardrails, writable_roots)

PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")
DELETERS = {"rm", "rmdir", "unlink", "shred", "truncate", "chmod", "chown", "touch", "tee", "patch"}
COPIERS = {"cp", "ln", "install", "rsync", "ditto"}
STASH_VERBS = {"push", "save", "pop", "apply", "drop", "list", "show", "clear", "branch"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
DESTRUCTIVE_EXEC = {"rm", "mv", "shred", "unlink", "truncate", "chmod", "chown", "cp", "sed", "perl", "tee",
                    "sh", "bash", "zsh", "dash", "xargs"}
MAX_DEPTH = 3
GLOB_RE = re.compile(r"[*?\[]")


def operands(args: list[str]) -> list[str]:
    return [w for a in args if not a.startswith("-") for w in expand_braces(a)]


def guard(paths: list[Path], what: str) -> None:
    for p in paths:
        if touches_guardrails(p):
            fail(f"{what} would change the guardrails ({p.name}). "
                 "Change hook files and settings yourself, outside Claude.")


def check_rm(args: list[str], cwd: Path, roots: list[Path], via_xargs: bool) -> None:
    flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
    recursive = "r" in flags.lower() or "--recursive" in args
    if via_xargs:
        fail("`xargs rm` deletes paths the hook cannot see. List the paths explicitly, or run it yourself.")
    for a in operands(args):
        for target in expand_paths(a, cwd):
            if target in {Path("/"), Path.home().resolve(), PROJECT_DIR}:
                fail(f"rm would delete {target}, which is the root, your home folder or the whole project.")
            if is_git_internal(target):
                fail("rm would delete the .git folder (your whole history).")
            if recursive and not is_within(target, roots):
                fail(f"recursive delete outside the project: {target}")


def split_target(args: list[str]) -> tuple[str | None, list[str]]:
    """Handle `-t DIR` / `--target-directory=DIR` (GNU cp/mv/ln/install): returns (target, sources)."""
    target, rest, skip = None, [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if (a in {"-t", "--target-directory"} or re.fullmatch(r"-[a-zA-Z]*t", a)) and i + 1 < len(args):
            target, skip = args[i + 1], True
        elif a.startswith("--target-directory="):
            target = a.split("=", 1)[1]
        else:
            rest.append(a)
    return target, rest


def expand_sources(sources: list[str], cwd: Path) -> list[str]:
    import glob as _glob
    out: list[str] = []
    for src in sources:
        if GLOB_RE.search(src):
            matches = _glob.glob(str(resolve(src, cwd)) if not src.startswith("/") else src)
            out += matches or [src]
        else:
            out.append(src)
    return out


def destination_paths(args: list[str], cwd: Path, content_copy: bool = False) -> list[Path]:
    """Paths a copy/move would create or overwrite. For an existing folder (or a path ending in /)
    that is <folder>/<name of each source>; when a source ends in `/` or `/.` its contents are
    copied, so the destination folder itself counts as changed. Source wildcards are expanded."""
    target, rest = split_target(args)
    ops = operands(rest)
    if target is None:
        if len(ops) < 2:
            return []
        dest_raw, sources = ops[-1], ops[:-1]
    else:
        dest_raw, sources = target + "/", ops
    dest = resolve(dest_raw, cwd)
    paths: list[Path] = []
    for src in expand_sources(sources, cwd):
        if src.endswith("/") or src.endswith("/.") or content_copy:
            # contents of src land in dest (BSD cp, rsync, ditto); GNU cp copies the folder itself
            paths.append(dest)
            src_dir = resolve(src.rstrip("/").removesuffix("/."), cwd)
            paths.append(dest / src_dir.name)
            if src_dir.is_dir():
                paths += [dest / child.name for child in src_dir.iterdir()]
        elif dest.is_dir() or dest_raw.endswith("/"):
            paths.append(dest / Path(src.rstrip("/")).name)
        else:
            paths.append(dest)
    return paths


def guardrail_files() -> list[str]:
    """Paths (relative to the project) of the real guardrail files."""
    rels: list[str] = []
    for g in GUARDRAIL_PATHS:
        p = PROJECT_DIR / g
        if p.is_dir():
            rels += [f.relative_to(PROJECT_DIR).as_posix() for f in p.rglob("*") if f.is_file()]
        rels.append(g)
    return rels


def archive_members(path: Path, strip: int = 0) -> list[str]:
    """File (not folder) names inside a zip or tar archive, with --strip-components applied."""
    import tarfile
    import zipfile
    names: list[str] = []
    try:
        if path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
        elif path.is_file() and tarfile.is_tarfile(path):
            with tarfile.open(path) as tf:
                names = [m.name for m in tf.getmembers() if not m.isdir()]
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile):
        return []
    if strip:
        names = ["/".join(Path(n).parts[strip:]) for n in names if len(Path(n).parts) > strip]
    return names


def option_value(args: list[str], short: str, long: str) -> str | None:
    for i, a in enumerate(args):
        if a in {short, long} and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
    return None


def check_extract(prog: str, args: list[str], cwd: Path) -> None:
    """unzip / tar -x / ditto -x into a folder: block when the archive would write guardrail files."""
    strip = 0
    if prog == "unzip":
        if any(a in {"-l", "-t", "-v", "-Z", "-z", "-p"} for a in args):
            return  # listing, testing or printing to stdout only
        dest = option_value(args, "-d", "--dest") or "."
        ops = [a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] != "-d")]
        archives = ops[:1]
    elif prog == "ditto":
        if "-x" not in args:
            return
        ops = operands(args)
        if len(ops) < 2:
            return
        archives, dest = ops[:1], ops[-1]
    else:
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
        old_style = bool(args) and not args[0].startswith("-") and "x" in args[0]
        if "x" not in flags and "--extract" not in args and "--get" not in args and not old_style:
            return
        dest = option_value(args, "-C", "--directory") or "."
        archives = [args[i + 1] for i, a in enumerate(args[:-1])
                    if (a.startswith("-") and not a.startswith("--") and a.endswith("f")) or a == "--file"]
        archives += [a.split("=", 1)[1] for a in args if a.startswith("--file=")]
        if old_style and "f" in args[0] and len(args) > 1:
            archives.append(args[1])  # old-style `tar xf archive.tar`
        strip_raw = option_value(args, "", "--strip-components")
        strip = int(strip_raw) if strip_raw and strip_raw.isdigit() else 0
    dest_dir = resolve(dest, cwd)
    for arc in archives:
        members = archive_members(resolve(arc, cwd), strip)
        guard([(dest_dir / m).resolve() for m in members], f"`{prog}` extracting {Path(arc).name}")


def find_parts(args: list[str]) -> tuple[list[str], list[str]]:
    roots: list[str] = []
    for i, a in enumerate(args):
        if a.startswith("-") or a in {"(", "!"}:
            return roots, args[i:]
        roots.append(a)
    return roots, []


PROBE_PATHS = ("./.claude", "./.claude/settings.json", "./.claude/guardrails.json", "./.claude/hooks/x.py")


def find_excludes_claude(expr: list[str]) -> bool:
    """True only for a top-level `-not -path PATTERN` / `! -path PATTERN` whose pattern really matches
    every guardrail path as find prints it (./.claude/...). `-prune` is not accepted (it has no effect
    together with -delete/-depth), and expressions with -o/-or branches are not trusted."""
    if any(a in {"-o", "-or", ","} for a in expr):
        return False
    for i, a in enumerate(expr[:-2]):
        if a in {"-not", "!"} and expr[i + 1] in {"-path", "-wholename"}:
            pattern = expr[i + 2]
            if all(fnmatch.fnmatch(probe, pattern) for probe in PROBE_PATHS):
                return True
    return False


def check_find(args: list[str], cwd: Path, roots_ok: list[Path]) -> None:
    roots_raw, expr = find_parts(args)
    exec_cmds = [Path(expr[i + 1]).name for i, a in enumerate(expr[:-1]) if a in {"-exec", "-execdir", "-ok", "-okdir"}]
    destructive = "-delete" in expr or any(c in DESTRUCTIVE_EXEC for c in exec_cmds)
    if not destructive:
        return
    roots = [p for a in (roots_raw or ["."]) for p in expand_paths(a, cwd)]
    for r in roots:
        if not is_within(r, roots_ok) or r in {Path("/"), Path.home().resolve()}:
            fail(f"`find ... -delete/-exec` would change files outside the project ({r}).")
        if is_git_internal(r):
            fail("`find ... -delete/-exec` inside .git would damage your history.")
        inside_claude = touches_guardrails(r) or is_within(r, [(PROJECT_DIR / ".claude").resolve()])
        if inside_claude:
            fail("`find ... -delete/-exec` inside .claude would change the guardrail files. Run it yourself.")
        if r == PROJECT_DIR and not find_excludes_claude(expr):
            fail("`find . ... -delete/-exec` from the project root can reach the guardrail files in .claude. "
                 "Add  -not -path './.claude*'  (or start from a subfolder), or run it yourself.")


def check_guardrails(prog: str, args: list[str], cwd: Path, roots_ok: list[Path]) -> None:
    ops = operands(args)
    if prog in DELETERS:
        guard([p for a in ops for p in expand_paths(a, cwd, include_parent=True)], f"`{prog}`")
        if prog == "tee":
            for a in ops:
                if not a.startswith("/dev/") and not is_within(resolve(a, cwd), roots_ok):
                    fail(f"`tee` writes outside the project: {a}")
    elif prog == "mv":
        target, rest = split_target(args)
        sources = operands(rest) if target else ops[:-1]
        guard([p for a in sources for p in expand_paths(a, cwd, include_parent=True)], "`mv`")
        guard(destination_paths(args, cwd), "`mv`")
    elif prog in COPIERS:
        guard(destination_paths(args, cwd, content_copy=prog == "ditto"), f"`{prog}`")
        if prog == "rsync" and any(a.startswith("--delete") for a in args) and ops:
            dest = resolve(ops[-1], cwd)
            if dest == PROJECT_DIR or touches_guardrails(dest) or is_within(PROJECT_DIR, [dest]):
                fail("`rsync --delete` into the project root or .claude could delete the guardrail files. Run it yourself.")
    elif prog in {"sed", "gsed", "perl"} and any(a.startswith("-i") or a == "-pi" for a in args):
        guard([p for a in ops[1:] for p in expand_paths(a, cwd)], f"`{prog} -i`")
    elif prog == "find":
        check_find(args, cwd, roots_ok)
    elif prog in {"unzip", "tar", "bsdtar", "gtar"}:
        check_extract("tar" if prog not in {"unzip"} else "unzip", args, cwd)
    if prog == "ditto":
        check_extract("ditto", args, cwd)
    elif prog == "dd":
        for a in args:
            if a.startswith("of="):
                target = resolve(a[3:], cwd)
                guard([target], "`dd`")
                if not a.startswith("of=/dev/") and not is_within(target, roots_ok):
                    fail(f"`dd` writes outside the project: {a[3:]}")


def untracked_guardrails() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", ".claude"],
                             cwd=PROJECT_DIR, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return any(line.startswith("??") and touches_guardrails((PROJECT_DIR / line[3:].strip()).resolve())
               for line in out.splitlines())


def check_git(args: list[str], config: dict, cwd: Path) -> None:
    sub, rest = git_subcommand(args)
    protected = config.get("protected_branches", [])
    if sub in {"checkout", "restore", "rm", "mv"}:
        guard([p for a in operands(rest) for p in expand_paths(a, cwd)], f"`git {sub}`")
    if sub == "stash":
        guard([p for a in operands(rest) if a not in STASH_VERBS for p in expand_paths(a, cwd)], "`git stash`")
        if any(a in {"-u", "-a", "--include-untracked", "--all"} for a in rest) and untracked_guardrails():
            fail("`git stash -u` would stash away the untracked guardrail files and switch the hooks off.")
    if sub == "push":
        forced = any(a == "-f" or a.startswith("--force") or a == "--mirror" for a in rest) or any(
            a.startswith("+") for a in operands(rest))
        if forced and not config.get("allow_force_push"):
            fail("force push (or --mirror) rewrites shared history. Push normally, or set allow_force_push.")
        deleting = any(a in {"--delete", "-d"} for a in rest)
        if deleting and any("$" in a for a in rest):
            fail("push --delete with a branch name from a variable; the hook cannot check it against protected branches.")
        for branch in protected:
            if (deleting and branch in rest) or f":{branch}" in rest or f":refs/heads/{branch}" in rest:
                fail(f"push would delete protected branch '{branch}' on the remote.")
    elif sub == "reset" and "--hard" in rest:
        fail("`git reset --hard` throws away uncommitted work. Commit or stash first, or run it yourself.")
    elif sub == "clean" and (any("f" in a for a in rest if a.startswith("-") and not a.startswith("--"))
                             or "--force" in rest):
        fail("`git clean -f` permanently deletes untracked files. Review with `git clean -n` and run it yourself.")
    elif sub in {"checkout", "restore"} and "." in rest and (
            sub == "checkout" or "--staged" not in rest or "--worktree" in rest):
        fail(f"`git {sub} .` discards every local change.")
    elif sub == "branch" and any(a in {"-D", "-d", "--delete"} for a in rest):
        for branch in protected:
            if branch in rest:
                fail(f"deleting protected branch '{branch}'.")


def check_command(command: str, cwd: Path, config: dict, roots: list[Path], depth: int = 0) -> None:
    if PIPE_TO_SHELL.search(command):
        fail("piping a downloaded script into a shell runs code you have not read. Download it, read it, then run it.")
    for segment in shell_segments(command):
        for target in redirect_targets(segment):  # resolved against the folder after earlier `cd`s
            resolved = resolve(target, cwd)
            if not is_within(resolved, roots):
                fail(f"output redirection writes outside the project: {target}")
            if touches_guardrails(resolved):
                fail(f"output redirection would overwrite a guardrail file: {target}")
        argv, prefixes = simple_command(segment)
        if "sudo" in prefixes and not config.get("allow_sudo"):
            fail("sudo is disabled for Claude. Run privileged commands yourself.")
        if not argv:
            continue
        prog = Path(argv[0]).name
        args = argv[1:]
        if prog in {"cd", "pushd"}:
            cwd = resolve(args[0], cwd) if args else Path.home()
            continue
        c_flag = next((i for i, a in enumerate(args) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a)), None)
        if depth < MAX_DEPTH and prog in SHELLS and c_flag is not None and c_flag + 1 < len(args):
            script_idx = c_flag + 2 if args[c_flag + 1] == "--" else c_flag + 1
            if script_idx < len(args):
                check_command(args[script_idx], cwd, config, roots, depth + 1)
            continue
        if depth < MAX_DEPTH and prog == "eval":
            check_command(" ".join(args), cwd, config, roots, depth + 1)
            continue
        if prog == "rm":
            check_rm(args, cwd, roots, via_xargs="xargs" in prefixes)
        check_guardrails(prog, args, cwd, roots)
        if prog == "git":
            check_git(args, config, cwd)
        elif prog == "chmod" and "-R" in args and "777" in args:
            fail("chmod -R 777 makes everything world-writable.")
        elif prog.startswith("mkfs") or (prog == "dd" and any(a.startswith("of=/dev/") for a in args)):
            fail(f"{prog} can erase a disk.")


def main() -> None:
    event = read_event()
    if event.get("tool_name") != "Bash":
        ok()
    config = load_config()
    command = strip_heredocs(str((event.get("tool_input") or {}).get("command") or ""))
    cwd = Path(event.get("cwd") or PROJECT_DIR)
    check_command(command, cwd, config, writable_roots(config))
    ok()


if __name__ == "__main__":
    main()
