#!/usr/bin/env python3
"""
通用开源仓库防泄密与隐私巡检脚本（纯 Python 标准库实现）。

功能说明：
1. 扫描仓库全量文本文件与前端构建产物（自动跳过 .git / .venv / node_modules / data / __pycache__）；
2. 内置通用敏感信息检测：
   - 明文 API Key / Token（Google AIza / AQ.、OpenAI/DeepSeek sk-、GitHub Token、AWS AKIA、私钥块）；
   - 私网/内网 IPv4 地址（RFC1918 与 CGNAT 网段）；
   - 硬编码本机绝对用户路径（macOS / Windows / Linux 真实用户名路径）；
   - 意外存在或被 git 追踪的敏感文件（.env / .secrets.env / .privacy-blocklist.local / *.db / *.key 等）。
3. 支持可选加载仓库根目录的本地私有黑名单 `.privacy-blocklist.local`（已被 .gitignore 忽略）：
   - 每行一条规则，支持 `#` 注释与空行；
   - 支持纯文本禁词（不区分大小写）或正则（`re:<pattern>`、`/<pattern>/` 或含正则元字符）；
   - 本公开脚本自身绝不硬编码任何个人敏感词。

用法示例：
  python3 scripts/privacy_check.py
  python3 scripts/privacy_check.py --root /path/to/repo --blocklist .privacy-blocklist.local

可一键设为 git pre-commit hook：
  ln -sf ../../scripts/privacy_check.py .git/hooks/pre-commit
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def _ensure_utf8_stdio() -> None:
    """在非 UTF-8 终端（如英文 Windows cp1252 / 中文 Windows gbk）下安全切换标准流编码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_ensure_utf8_stdio()

EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "data",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
    }
)

# 明确的二进制扩展名，直接跳过文本正则扫描
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".tiff",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".mp3",
        ".wav",
        ".flac",
        ".m4a",
        ".ogg",
        ".aac",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".pdf",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".wasm",
    }
)

# 任何情况下都不应出现在干净开源仓库目录或 git 追踪列表中的文件名模式
FORBIDDEN_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".secrets.env",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite3",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
)

# 仅在被 git 追踪时报错的文件（允许存在于本地工作区供扫描使用）
FORBIDDEN_TRACKED_ONLY_PATTERNS: tuple[str, ...] = (
    ".privacy-blocklist.local",
    "*.log",
)

_PRIV_KEY_BEGIN = "-----BEGIN " + r"(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"

BUILTIN_RULES: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    # (规则名, 编译后正则, 是否脱敏输出命中片段)
    ("SECRET_GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"), True),
    ("SECRET_GOOGLE_AQ_TOKEN", re.compile(r"\bAQ\.[0-9A-Za-z\-_]{20,}\b"), True),
    ("SECRET_SK_API_KEY", re.compile(r"\bsk-(?:proj-|ant-)?[0-9A-Za-z\-_]{20,}\b"), True),
    ("SECRET_GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{20,}\b"), True),
    ("SECRET_GITHUB_PAT", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b"), True),
    ("SECRET_AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), True),
    ("SECRET_PRIVATE_KEY", re.compile(_PRIV_KEY_BEGIN), True),
    (
        "PRIVATE_IP_192_168",
        re.compile(rf"(?<![0-9.])192\.168\.{_OCTET}\.{_OCTET}(?![0-9.])"),
        False,
    ),
    (
        "PRIVATE_IP_10",
        re.compile(rf"(?<![0-9.])10\.{_OCTET}\.{_OCTET}\.{_OCTET}(?![0-9.])"),
        False,
    ),
    (
        "PRIVATE_IP_100",
        re.compile(rf"(?<![0-9.])100\.{_OCTET}\.{_OCTET}\.{_OCTET}(?![0-9.])"),
        False,
    ),
    (
        "PRIVATE_IP_172",
        re.compile(rf"(?<![0-9.])172\.(?:1[6-9]|2\d|3[01])\.{_OCTET}\.{_OCTET}(?![0-9.])"),
        False,
    ),
    (
        "LOCAL_ABS_PATH_MACOS",
        re.compile(r"/Users/[A-Za-z0-9._-]+(?:/|(?=['\"\s`)\],;]|$))"),
        False,
    ),
    (
        "LOCAL_ABS_PATH_WINDOWS",
        re.compile(r"(?i)[A-Z]:[\\/]Users[\\/][A-Za-z0-9._-]+"),
        False,
    ),
    (
        "LOCAL_ABS_PATH_LINUX",
        re.compile(r"/home/[A-Za-z0-9._-]+/"),
        False,
    ),
)

_REGEX_META_CHARS = set("|*+?[]()^$\\")


@dataclass
class Finding:
    rule: str
    rel_path: str
    line_no: int
    snippet: str


def _mask_snippet(line: str, match: re.Match[str], redact: bool) -> str:
    """仅截取命中点前后上下文（避免压缩 JS 单行输出刷屏），敏感密钥自动打码。"""
    start = max(0, match.start() - 36)
    end = min(len(line), match.end() + 36)
    prefix = line[start : match.start()]
    matched = match.group(0)
    suffix = line[match.end() : end]

    if redact and len(matched) > 8:
        matched = f"{matched[:4]}***{matched[-3:]}"
    elif redact:
        matched = "***"

    snippet = f"{prefix}{matched}{suffix}".replace("\r", "").replace("\t", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(line):
        snippet = snippet + "..."
    return snippet


def _compile_blocklist_entry(raw_line: str, idx: int) -> tuple[str, re.Pattern[str]] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    rule_name = f"BLOCKLIST_L{idx}"
    if line.startswith("re:"):
        pattern_str = line[3:].strip()
        if not pattern_str:
            return None
        return rule_name, re.compile(pattern_str, re.IGNORECASE)

    if line.startswith("regex:"):
        pattern_str = line[6:].strip()
        if not pattern_str:
            return None
        return rule_name, re.compile(pattern_str, re.IGNORECASE)

    if len(line) > 2 and line.startswith("/") and line.endswith("/"):
        return rule_name, re.compile(line[1:-1], re.IGNORECASE)

    if any(ch in _REGEX_META_CHARS for ch in line):
        try:
            return rule_name, re.compile(line, re.IGNORECASE)
        except re.error:
            return rule_name, re.compile(re.escape(line), re.IGNORECASE)

    return rule_name, re.compile(re.escape(line), re.IGNORECASE)


def load_local_blocklist(blocklist_path: Path) -> list[tuple[str, re.Pattern[str]]]:
    if not blocklist_path.exists() or not blocklist_path.is_file():
        return []
    rules: list[tuple[str, re.Pattern[str]]] = []
    text = blocklist_path.read_text(encoding="utf-8-sig", errors="replace")
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        try:
            compiled = _compile_blocklist_entry(raw_line, idx)
        except re.error as exc:
            print(
                f"警告：跳过无效正则 {blocklist_path.name}:{idx} ({exc})",
                file=sys.stderr,
            )
            continue
        if compiled is not None:
            rules.append(compiled)
    return rules


def _is_forbidden_name(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _check_git_tracked_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except OSError:
        return findings

    if res.returncode != 0:
        return findings

    for rel in res.stdout.splitlines():
        rel_norm = rel.strip()
        if not rel_norm:
            continue
        parts = Path(rel_norm).parts
        name = parts[-1]
        if (
            parts[0] == "data"
            or _is_forbidden_name(name, FORBIDDEN_FILE_PATTERNS)
            or _is_forbidden_name(name, FORBIDDEN_TRACKED_ONLY_PATTERNS)
        ):
            findings.append(
                Finding(
                    rule="GIT_TRACKED_FORBIDDEN_FILE",
                    rel_path=rel_norm,
                    line_no=0,
                    snippet="敏感或运行时文件已被 git 追踪，请执行 git rm --cached 移出追踪并检查 .gitignore",
                )
            )
    return findings


def _is_binary_bytes(sample: bytes) -> bool:
    return b"\x00" in sample


def scan_repository(
    root: Path,
    blocklist_path: Path,
) -> tuple[int, int, list[Finding]]:
    blocklist_rules = load_local_blocklist(blocklist_path)
    findings: list[Finding] = []
    findings.extend(_check_git_tracked_files(root))

    blocklist_resolved = blocklist_path.resolve() if blocklist_path.exists() else None
    scanned_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for filename in sorted(filenames):
            fpath = Path(dirpath) / filename
            if blocklist_resolved is not None and fpath.resolve() == blocklist_resolved:
                continue

            rel_path = fpath.relative_to(root).as_posix()

            # 检查不应滞留在工作区的敏感文件
            if _is_forbidden_name(filename, FORBIDDEN_FILE_PATTERNS):
                findings.append(
                    Finding(
                        rule="FORBIDDEN_FILE_PRESENT",
                        rel_path=rel_path,
                        line_no=0,
                        snippet="检测到敏感配置/密钥/数据库文件，请移出仓库或确认仅保存在受控位置",
                    )
                )
                continue

            if fpath.suffix.lower() in BINARY_EXTENSIONS:
                continue

            try:
                raw = fpath.read_bytes()
            except OSError as exc:
                print(f"警告：无法读取 {rel_path} ({exc})", file=sys.stderr)
                continue

            if _is_binary_bytes(raw[:8192]):
                continue

            scanned_files += 1
            text = raw.decode("utf-8-sig", errors="replace")

            for line_no, line in enumerate(text.splitlines(), start=1):
                for rule_name, pattern, redact in BUILTIN_RULES:
                    for match in pattern.finditer(line):
                        findings.append(
                            Finding(
                                rule=rule_name,
                                rel_path=rel_path,
                                line_no=line_no,
                                snippet=_mask_snippet(line, match, redact=redact),
                            )
                        )
                for rule_name, pattern in blocklist_rules:
                    for match in pattern.finditer(line):
                        findings.append(
                            Finding(
                                rule=rule_name,
                                rel_path=rel_path,
                                line_no=line_no,
                                snippet=_mask_snippet(line, match, redact=False),
                            )
                        )

    return scanned_files, len(blocklist_rules), findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="反重力视频工坊 — 通用开源防泄密与隐私巡检工具"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="待扫描仓库根目录（默认自动定位为本脚本所在仓库根目录）",
    )
    parser.add_argument(
        "--blocklist",
        type=Path,
        default=None,
        help="可选本地私有黑名单文件路径（默认读取 <root>/.privacy-blocklist.local）",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"错误：扫描目录不存在：{root}", file=sys.stderr)
        return 2

    blocklist_path = (
        args.blocklist.resolve()
        if args.blocklist is not None
        else root / ".privacy-blocklist.local"
    )

    scanned_files, blocklist_count, findings = scan_repository(root, blocklist_path)

    print(
        f"[privacy_check] 已扫描 {scanned_files} 个文件"
        f"（内置规则 {len(BUILTIN_RULES)} 条，"
        f"本地私有黑名单 {blocklist_count} 条"
        f"{'：已加载 ' + blocklist_path.name if blocklist_count > 0 else '：未检测到本地黑名单'}）"
    )

    if findings:
        print(f"[privacy_check] 发现 {len(findings)} 处疑似敏感或命中项：", file=sys.stderr)
        for item in findings:
            loc = f"{item.rel_path}:{item.line_no}" if item.line_no > 0 else item.rel_path
            print(f"  - [{item.rule}] {loc}: {item.snippet}", file=sys.stderr)
        return 1

    print("[privacy_check] 巡检通过：0 处敏感泄露或黑名单命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
