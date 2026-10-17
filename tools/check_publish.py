#!/usr/bin/env python
"""上传 GitHub 之前的体检：扫描项目里有没有不该公开的东西。

用法（项目根目录）：
    .\\.venv\\Scripts\\python.exe tools\\check_publish.py
    .\\.venv\\Scripts\\python.exe tools\\check_publish.py --verbose

退出码：0 = 可以发布；1 = 有需要先处理的问题。
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 不扫描的目录（本地产物 / 依赖）
SKIP_DIRS = {
    ".venv", "venv", "data", "logs", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".git", ".idea", ".vscode",
}

# 应当被 .gitignore 排除、但如果在仓库里出现就很危险的文件名
FORBIDDEN_NAMES = {
    ".env": "里面有真实 AppSecret / API Key",
    "sessions.db": "含所有用户 openid 与聊天记录",
    "dsh_localhost_run": "SSH 私钥",
    "id_ed25519": "SSH 私钥",
    "id_rsa": "SSH 私钥",
}

PATTERNS = [
    ("DeepSeek/OpenAI 风格的 API Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("疑似 AppSecret（32 位十六进制）", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")),
    ("微信 AppID（wx + 16 位十六进制）", re.compile(r"\bwx[0-9a-f]{16}\b")),
    ("疑似 openid（o 开头 20+ 位）", re.compile(r"\bo[A-Za-z0-9_\-]{20,}\b")),
    ("EncodingAESKey（43 位 base64）", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/])")),
    ("私钥内容", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
    ("本机绝对路径（含用户名）", re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9_.\-]+")),
    ("公网 IP", re.compile(r"\b(?!10\.|127\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)(?:\d{1,3}\.){3}\d{1,3}\b")),
]

SAFE_PLACEHOLDERS = ("your_", "xxx", "XXXX", "change_me", "<", "example", "sk-xxxx", "1234567890", "abcdef", "Invalid", "Test")


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def masked(line: str, match: re.Match) -> str:
    """命中的内容打码后回显，避免体检工具自己把密钥打出来。"""
    value = match.group(0)
    if len(value) <= 8:
        return value
    return value[:4] + "…" + value[-4:]


def load_env_secrets() -> dict[str, str]:
    """从 .env 读取真实敏感值，用来反查它们有没有被抄进其它文件。

    这比正则猜要靠谱得多：不管密钥长什么样，只要它出现在别处就能抓到。
    """
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return {}
    keywords = ("TOKEN", "SECRET", "API_KEY", "AES", "OPENID", "SUBDOMAIN", "PASSWORD")
    secrets: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not any(word in key.upper() for word in keywords):
            continue
        for part in value.replace(";", ",").replace(" ", ",").split(","):
            part = part.strip()
            if len(part) >= 8:
                secrets[part] = key
    return secrets


def load_gitignore() -> list[str]:
    path = ROOT / ".gitignore"
    if not path.is_file():
        return []
    patterns: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and not line.startswith("!"):
            patterns.append(line.rstrip("/"))
    return patterns


def is_ignored(rel: Path, patterns: list[str]) -> bool:
    """粗略判断某个相对路径是否被 .gitignore 排除（够用即可）。"""
    posix = rel.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(rel.name, pattern) or fnmatch.fnmatch(posix, pattern):
            return True
        for part in rel.parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="上传前体检")
    parser.add_argument("--verbose", action="store_true", help="显示全部命中行")
    args = parser.parse_args()

    files = iter_files()
    problems: dict[str, list[str]] = {}
    secrets = load_env_secrets()
    ignore_patterns = load_gitignore()
    ignored_hits: list[str] = []

    def add(path: Path, message: str) -> None:
        rel = str(path.relative_to(ROOT))
        problems.setdefault(rel, []).append(message)

    print("=" * 62)
    print(" 上传前体检  (项目根目录: %s)" % ROOT)
    print("=" * 62)
    print("扫描文件数：%d     .env 里的敏感值：%d 个    .gitignore 规则：%d 条\n"
          % (len(files), len(secrets), len(ignore_patterns)))

    for path in files:
        name = path.name
        rel = path.relative_to(ROOT)
        if name in FORBIDDEN_NAMES or name.endswith((".pem", ".key")):
            reason = FORBIDDEN_NAMES.get(name, "密钥文件后缀")
            if is_ignored(rel, ignore_patterns):
                ignored_hits.append("%s（%s，已被 .gitignore 排除 ✓）" % (rel.as_posix(), reason))
            else:
                add(path, "禁止上传：%s —— 而且 .gitignore 没排除它！" % reason)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text or len(text) > 2_000_000:
            continue

        # 最有力的一步：.env 里的真实值是否出现在这个文件里
        for value, key_name in secrets.items():
            if value in text:
                line_no = text.count("\n", 0, text.index(value)) + 1
                add(path, "出现了 .env 中 %s 的真实值（第 %d 行）← 必须换成占位符" % (key_name, line_no))

        is_template = name.endswith(".example") or name == "README.md"
        for label, regex in PATTERNS:
            for match in regex.finditer(text):
                value = match.group(0)
                if any(token in value or token in text[max(0, match.start() - 40):match.start()] for token in SAFE_PLACEHOLDERS):
                    continue
                if label == "公网 IP" and (value in {"0.0.0.0", "255.255.255.255", "1.2.3.4"} or value.startswith(("192.0.2.", "198.51.100.", "203.0.113."))):
                    continue
                line_no = text.count("\n", 0, match.start()) + 1
                where = "%s（第 %d 行，%s）" % (label, line_no, masked(text, match))
                if is_template:
                    where += "  ← 模板/文档里的示例值，确认为占位符即可"
                add(path, where)
                if not args.verbose:
                    break

    if ignored_hits:
        print("已正确排除（不会上传）：")
        for item in ignored_hits:
            print("  ✓ %s" % item)
        print()

    if not problems:
        print("✅ 没有发现敏感内容，可以发布。")
        return 0

    print("⚠ 发现 %d 个文件需要处理：\n" % len(problems))
    for rel, items in sorted(problems.items()):
        print("  %s" % rel)
        for item in items:
            print("      └ %s" % item)
    print()
    print("处理建议：")
    print("  1) 真实密钥只能放在 .env（已被 .gitignore 排除），文档里一律用占位符")
    print("  2) 已经提交过历史的，光删文件没用，要 git rm --cached + 重置历史，并立刻轮换密钥")
    print("  3) 确认 .gitignore 生效：git status --short 里不应出现 .env / data / logs")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
