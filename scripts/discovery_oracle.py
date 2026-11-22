"""Validate discovery manifests and score submitted facts offline."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

PROMPT_FORMAT = """
Use the read-only navigation tools to locate the responsible code. Do not run tests or edit files.
Respond with one JSON object with keys "owners", "dependency_paths", and "rationale".
Each entry in "owners" has "path" and "symbols" (a list of declared names).
Use repository-relative paths. Give a one-sentence responsibility rationale.
"""


def clean_path(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and "\\" not in path and "\x00" not in path
            and not Path(path).is_absolute() and Path(path).as_posix() == path
            and not set(Path(path).parts) & {".", "..", ".git"})


def string_list(values: object, predicate, *, nonempty: bool = False) -> bool:
    return (isinstance(values, list) and (bool(values) or not nonempty)
            and all(predicate(value) for value in values) and len(values) == len(set(values)))


def symbol_name(symbol: object) -> bool:
    return isinstance(symbol, str) and bool(re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", symbol))


def owner_symbols(expected: dict) -> dict[str, list[str]]:
    owners = expected.get("owner_paths")
    if not string_list(owners, clean_path, nonempty=True):
        raise ValueError("Discovery needs unique repository-relative owner_paths")
    symbols = expected.get("symbols")
    if isinstance(symbols, list) and len(owners) == 1:
        symbols = {owners[0]: symbols}
    if (not isinstance(symbols, dict) or set(symbols) != set(owners)
            or any(not string_list(v, symbol_name, nonempty=True) for v in symbols.values())):
        raise ValueError("Discovery needs declared symbols for each owner")
    if not string_list(expected.get("dependency_paths", []), clean_path):
        raise ValueError("Discovery dependency_paths must be unique repository-relative paths")
    if expected.get("human_review", "pending") not in {"pending", "accepted", "rejected"}:
        raise ValueError("Invalid discovery human_review status")
    return symbols


def validate_task(task: dict) -> None:
    question, expected = task.get("question"), task.get("expected")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Discovery needs a natural-language question")
    if not isinstance(expected, dict) or set(expected) != {"before", "after"}:
        raise ValueError("Discovery needs evaluator-only before and after answers")
    if any(key in task for key in ("setup_commands", "success_checks", "regression_checks", "expected_failure")):
        raise ValueError("Discovery tasks accept no setup or test commands")
    for grading in expected.values():
        if not isinstance(grading, dict):
            raise ValueError("Invalid discovery expected-answer object")
        paths = [*owner_symbols(grading), *grading.get("dependency_paths", [])]
        if any(Path(path).name.casefold() in question.casefold() for path in paths):
            raise ValueError("Discovery question reveals an expected filename")


def resolved_file(root: Path, path: str) -> Path:
    if not clean_path(path):
        raise ValueError("Expected a safe repository-relative file path")
    candidate = root / path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("File is absent") from exc
    if resolved != candidate or not resolved.is_file() or root not in resolved.parents:
        raise ValueError("File must be a regular in-checkout path without symlinks")
    return resolved


def declared_symbols(file: Path) -> set[str]:
    content = file.read_text(encoding="utf-8")
    if file.suffix in {".py", ".pyi"}:
        found = set()

        def visit(body: list[ast.stmt], prefix: str = "") -> None:
            for node in body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + node.name
                    found.add(name)
                    if isinstance(node, ast.ClassDef):
                        visit(node.body, name + ".")

        visit(ast.parse(content).body)
        return found
    if file.suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".cs"}:
        # Recognize ordinary named declarations. Ambiguous language constructs require a different oracle.
        content = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*|"
                         r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\x60(?:\\.|[^\x60\\])*\x60',
                         lambda match: "".join("\\n" if c == "\\n" else " " for c in match[0]), content)
        return set(re.findall(
            r"(?m)^[ \t]*(?:(?:export|default|public|pub|async|static|declare|abstract)\s+)*"
            r"(?:function|func|fn|class|interface|enum|type|struct|const|let|var)\s+([A-Za-z_$][\w$]*)\b",
            content,
        ))
    return set()


def valid_expected(expected: dict, root: Path) -> bool:
    try:
        for path, symbols in owner_symbols(expected).items():
            if not set(symbols) <= declared_symbols(resolved_file(root, path)):
                return False
        return all(resolved_file(root, path) for path in expected.get("dependency_paths", []))
    except (OSError, ValueError, SyntaxError):
        return False


def score_answer(text: str | None, expected: dict, root: Path) -> dict:
    truth = owner_symbols(expected)
    score = {"passed": False, "owner_paths_match": False, "symbols_match": False, "dependency_paths_match": False,
             "human_review": expected.get("human_review", "pending"), "response_format_valid": False}
    try:
        answer = json.loads(text or "")
        entries = answer["owners"]
        dependencies = answer["dependency_paths"]
        rationale = answer["rationale"]
        if (not isinstance(entries, list) or not isinstance(rationale, str) or not rationale.strip()
                or not string_list(dependencies, clean_path)):
            return score
        submitted = {}
        for entry in entries:
            path, symbols = entry["path"], entry["symbols"]
            if not clean_path(path) or path in submitted or not string_list(symbols, symbol_name, nonempty=True):
                return score
            submitted[path] = set(symbols)
    except (ValueError, TypeError, KeyError):
        return score
    score["response_format_valid"] = True
    score["owner_paths_match"] = submitted.keys() == truth.keys()
    score["symbols_match"] = submitted == {path: set(symbols) for path, symbols in truth.items()} and valid_expected(expected, root)
    score["dependency_paths_match"] = set(dependencies) == set(expected.get("dependency_paths", []))
    score["passed"] = all(score[k] for k in ("owner_paths_match", "symbols_match", "dependency_paths_match"))
    return score


def score_discovery_facts(text: str | None, expected: dict, root: Path) -> dict:
    """Check source-backed declarations and path references in structured answers."""
    if not valid_expected(expected, root):
        return {"status": "unscorable", "reason": "expected_facts_not_in_snapshot"}
    try:
        answer = json.loads(text or "")
    except (ValueError, TypeError):
        return {"status": "unscorable", "reason": "no_structured_answer"}
    if not isinstance(answer, dict):
        return {"status": "unscorable", "reason": "no_structured_answer"}

    paths: set[str] = set()
    symbols: dict[str, set[str]] = {}
    for field in ("owners", "dependency_paths"):
        entries = answer.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if clean_path(entry):
                paths.add(entry)
            elif isinstance(entry, dict) and clean_path(entry.get("path")):
                path = entry["path"]
                paths.add(path)
                names = entry.get("symbols")
                if isinstance(names, list):
                    symbols.setdefault(path, set()).update(name for name in names if symbol_name(name))

    owners = owner_symbols(expected)
    facts = {
        "owner_paths_found": owners.keys() <= paths,
        "owner_symbols_found": all(set(names) <= symbols.get(path, set()) for path, names in owners.items()),
        "dependency_paths_found": set(expected.get("dependency_paths", [])) <= paths,
    }
    return {"status": "pass" if all(facts.values()) else "fail", **facts}
