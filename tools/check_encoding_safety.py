from __future__ import annotations

import ast
import re
import sys
import tokenize
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", ".codex_recovery"}
CJK_NAME_RE = re.compile(r"[\u3400-\u9fff]")
NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")
CODE_AFTER_TEXT_RE = re.compile(
    r"\S.{0,160}\s{12,}"
    r"(if|elif|else|for|while|def|class|return|self\.|[A-Za-z_]\w*\s*=)\b"
)


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


def check_file(path: Path) -> list[str]:
    issues: list[str] = []
    rel = path.relative_to(ROOT)
    try:
        with path.open("rb") as f:
            tokens = list(tokenize.tokenize(f.readline))
    except Exception as exc:
        return [f"{rel}: TOKENIZE_ERROR {exc}"]

    for tok in tokens:
        if tok.type == tokenize.NAME and CJK_NAME_RE.search(tok.string):
            issues.append(
                f"{rel}:{tok.start[0]}:{tok.start[1]} 中文标识符: {tok.string}"
            )
        elif tok.type == tokenize.COMMENT:
            body = tok.string[1:]
            stripped = body.lstrip()
            # Ignore ordinary commented-out code blocks such as "#     if ...".
            if body[: len(body) - len(stripped)].count(" ") >= 4:
                continue
            if CODE_AFTER_TEXT_RE.search(body):
                issues.append(
                    f"{rel}:{tok.start[0]}:{tok.start[1]} 注释里疑似藏了代码: {tok.string[:220]}"
                )
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
    except Exception as exc:
        issues.append(f"{rel}: AST_PARSE_ERROR {exc}")
        return issues

    for node in ast.walk(tree):
        names: list[tuple[str, str, int]] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(("定义名", node.name, node.lineno))
        elif isinstance(node, ast.Name):
            names.append(("标识符", node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            names.append(("属性名", node.attr, node.lineno))
        elif isinstance(node, ast.arg):
            names.append(("参数名", node.arg, node.lineno))
        for kind, name, lineno in names:
            if NON_ASCII_RE.search(name):
                issues.append(f"{rel}:{lineno}: 非 ASCII {kind}: {name}")

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr", "hasattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            attr_name = node.args[1].value
            if attr_name.startswith("_") and NON_ASCII_RE.search(attr_name):
                issues.append(
                    f"{rel}:{node.lineno}: 动态私有属性名含非 ASCII: {attr_name}"
                )
    return issues


def main() -> int:
    issues: list[str] = []
    for path in iter_python_files():
        issues.extend(check_file(path))

    if issues:
        print("发现编码/注释安全问题：")
        for issue in issues:
            print(issue)
        return 1

    print("编码/注释安全检查通过：未发现非 ASCII 标识符/属性名或注释吞代码。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
