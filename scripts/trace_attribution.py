"""Conservative classification of the shell forms visible in Codex CLI events."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

SOURCE_SUFFIXES = frozenset("""
.py .pyi .ts .tsx .js .jsx .mjs .cjs .java .kt .kts .go .rs .c .cc .cpp .h .hpp
.cs .rb .php .swift .scala .ex .exs .erl .hs .lua .sql .sh .bash .zsh .vue .svelte
.css .scss .html .xml .proto
""".split())
NAVIGATION = {"source_search", "source_read", "file_listing"}
SEARCH = {"rg", "ripgrep", "grep", "ag"}
READ = {"cat", "sed", "head", "tail", "nl", "bat", "more", "less", "awk"}
SEPARATORS = frozenset(";&|<>()\n")
SAFE_PATH = re.compile(r"[\w./@+-]+")
PATH_MENTION = re.compile(r"(?<![\w/~$.-])(?:~?/)?(?:[\w@.+-]+/)*[\w@+-][\w@.+-]*\.[a-zA-Z0-9]+(?=$|[\s:'\",;()\]}])")
ENV_PREFIX = re.compile(r"[A-Za-z_]\w*=.*")


def relative_path(raw: str, root: Path | None, cwd: Path | None) -> str | None:
    path = Path(raw)
    if raw.startswith("~") or ".." in path.parts:
        return None
    if root is None:
        return None if path.is_absolute() else path.as_posix()
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    try:
        return path.resolve().relative_to(root).as_posix()
    except (ValueError, OSError, RuntimeError):
        return None


def path_category(raw: str, root: Path | None, cwd: Path | None) -> str:
    name = Path(raw).name
    path = relative_path(raw, root, cwd)
    if (name in {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md"} or
            "/skills/" in raw and (path is None or ".agents" in Path(path).parts or ".codex" in Path(path).parts)):
        return "agent_instructions"
    if path is None or root is None:
        return "other"
    parts = Path(path).parts
    if {"docs", "documentation"} & set(parts) or Path(name).suffix.lower() in {".md", ".rst", ".adoc"}:
        return "project_docs"
    if (
        {"test", "tests", "__tests__", "testing", "spec", "specs", "fixtures", "testdata", "vendor",
         "node_modules", "site-packages", ".git", ".venv", "venv", ".github", ".gitlab", "ci"} & set(parts)
        or name.startswith("test_") or re.search(r"(?:[_.-](?:test|spec))\.", name)
    ):
        return "other"
    return "source_read" if Path(name).suffix in SOURCE_SUFFIXES else "other"


def tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def shell_body(command: str) -> str:
    """Unquote a fixed shell wrapper, leaving the shell body inert."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if len(words) == 3 and Path(words[0]).name in {"bash", "sh", "zsh"} and words[1] in {"-c", "-lc", "-cl"}:
        return words[2]
    return command


def split_operations(body: str) -> tuple[list[list[str]], bool]:
    operations = []
    current: list[str] = []
    compound = False
    for word in tokens(body.strip()):
        if word and set(word) <= SEPARATORS:
            compound = True
            if current:
                operations.append(current)
                current = []
        else:
            current.append(word)
    if current:
        operations.append(current)
    return operations, compound or len(operations) != 1


def read_targets(program: str, args: list[str]) -> list[str] | None:
    """Accept fixed, output-only forms with a single unexpanded source operand."""
    if program == "sed":
        if len(args) != 3 or args[0] != "-n" or not re.fullmatch(
            r"(?:[0-9]+(?:,[0-9]+|\$)?p)(?:;[0-9]+(?:,[0-9]+|\$)?p)*", args[1]
        ):
            return None
        args = args[2:]
    elif program in {"head", "tail"} and args and args[0] != "--":
        if len(args) >= 2 and re.fullmatch(r"-[0-9]+", args[0]):
            args = args[1:]
        elif len(args) >= 3 and args[0] == "-n" and args[1].isdigit():
            args = args[2:]
    elif program == "nl" and args[:1] == ["-ba"]:
        args = args[1:]
    elif program not in {"cat", "head", "tail", "nl"}:
        return None
    if args[:1] == ["--"]:
        args = args[1:]
    if not args or any(not SAFE_PATH.fullmatch(arg) or arg.startswith("-") for arg in args):
        return None
    return args


def classify_operation(words: list[str], root: Path | None, cwd: Path | None) -> tuple[str, list[str]]:
    if words[:1] in (["do"], ["then"], ["else"]):
        words = words[1:]
    while words and ENV_PREFIX.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return "other", []
    executable = Path(words[0])
    program, args = executable.name, words[1:]
    if program in (READ | SEARCH | {"git"}) and "/" in words[0] and (
        executable.parent.as_posix() not in {"/bin", "/usr/bin", "/usr/local/bin"}
    ):
        return "other", []
    if program == "timeout" and len(args) > 1:
        return classify_operation(args[1:], root, cwd)
    if program in SEARCH or (program == "git" and args[:1] == ["grep"]):
        listing = any(a in {"--files", "--files-with-matches", "--files-without-match"} or
                      re.fullmatch(r"-[a-zA-Z]*[lL][a-zA-Z]*", a) for a in args)
        return ("file_listing" if listing else "source_search"), []
    if program in {"ls", "find", "fd", "tree"}:
        return "file_listing", []
    if program == "git":
        return "git_inspection", []
    if (
        program in {"pytest", "py.test", "jest", "vitest", "unittest", "tox", "nox"}
        or program.startswith("python") and any(
            a in {"pytest", "unittest"} or re.search(r"\b(?:import pytest|pytest\.main)\b", a) for a in args)
        or program in {"npm", "pnpm", "yarn", "bun", "go", "cargo", "uv"} and any(
            a in {"test", "pytest", "vitest", "jest"} or a.startswith("test:") for a in args[:4])
    ):
        return "test_execution", []
    if (
        program in {"pwd", "which", "command", "type", "env", "printenv", "ps", "date", "pip", "pip3",
                    "ruff", "wc", "timeout", "uname", "df", "du", "ulimit"}
        or program.startswith("python") and any(a in {"-V", "--version", "-X"} for a in args)
        or "/ci/" in words[0] or program in READ and any(
            a.endswith((".out", ".log", ".stderr", ".stdout")) for a in args)
    ):
        return "environment_diagnostics", []
    targets = read_targets(program, args)
    if targets:
        categories = {path_category(target, root, cwd) for target in targets}
        if len(categories) == 1 and (len(targets) == 1 or categories <= {"agent_instructions", "project_docs"}):
            return categories.pop(), targets
    if program in READ and any(a.endswith("/SKILL.md") for a in args):
        return "agent_instructions", []
    return "other", []


def inspect_command(command: str, root: Path | None, cwd: Path | None) -> tuple[str, list[str], list[str]]:
    body = shell_body(command)
    # This fixed loop has precisely one output producer, reading external skill docs.
    instruction_loop = re.fullmatch(
        r'for (\w+) in [\w -]+;\s*do\s+cat\s+"?(/[\w./-]+/skills/)\$\1/SKILL\.md"?;\s*done', body.strip())
    if instruction_loop and (not root or relative_path(instruction_loop[2], root, cwd) is None):
        return "agent_instructions", ["agent_instructions"], []
    try:
        operations, compound = split_operations(body)
    except ValueError:
        return "mixed_unattributed", ["other"], []
    detected = [classify_operation(parts, root, cwd) for parts in operations]
    categories = [kind for kind, _ in detected]
    if compound:
        return "mixed_unattributed", categories, []
    category, paths = detected[0]
    if category in NAVIGATION and any(marker in body for marker in ("$(", "\x60", "$" + "{", "\\\n")):
        return "mixed_unattributed", categories, []
    if category in NAVIGATION and (
        ENV_PREFIX.fullmatch(operations[0][0]) or category == "source_search" and any(
            arg.startswith(("--pre", "--replace")) or re.fullmatch(r"-[a-zA-Z]*[rz][a-zA-Z]*", arg)
            for arg in operations[0][1:]
        )
    ):
        return "other", categories, []
    if category == "agent_instructions" and not paths:
        return "mixed_unattributed", categories, []
    return category, categories, paths


def git_metadata_only(command: str) -> bool:
    """Some Git commands display patches or blobs even when used for navigation."""
    try:
        words = tokens(shell_body(command))
    except ValueError:
        return False
    if len(words) < 2 or Path(words[0]).name != "git":
        return False
    verb, args = words[1], words[2:]
    if verb in {"status", "rev-parse", "ls-files", "branch"}:
        return True
    if verb in {"log", "show", "diff"}:
        safe_flags = {"--stat", "--shortstat", "--name-only", "--name-status", "--numstat", "--check", "--oneline"}
        return bool(safe_flags & set(args)) and all(a in safe_flags or re.fullmatch(r"(?:HEAD|-[0-9]+)", a)
                                                    for a in args)
    return False


def scoped_search(output: str, root: Path | None, cwd: Path | None) -> bool:
    # Accept only filename:line:match records. Bare excerpts, headings,
    # diagnostics and mixed documentation cannot qualify.
    lines = output.splitlines()
    matches = [re.fullmatch(r"([^:\r\n]+):[1-9][0-9]*:.*", line) for line in lines]
    return bool(lines) and all(m and path_category(m[1], root, cwd) == "source_read" for m in matches)


def mentions(text: str, root: Path | None, cwd: Path | None) -> set[str]:
    return {path for match in PATH_MENTION.finditer(text)
            if (path := relative_path(match[0], root, cwd)) is not None}
