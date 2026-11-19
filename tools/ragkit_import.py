#!/usr/bin/env python3
"""RAGKit 评测集工具：导入题集、刷新语料，或直接校验文档 PR。

三种模式
--------
``import``（默认，v0.2 行为）
    从 qrels 生成 fixture 与规范化语料快照：

        python tools/ragkit_import.py --ragkit-root <root> --output <snapshot>

``refresh``（v0.3）
    冻结旧快照的 fixture，只重建语料：

        python tools/ragkit_import.py refresh --ragkit-root <root> \
            --baseline <old-snapshot> --output <new-snapshot>

``verify``（v0.3）
    一条命令校验 PR 原始文档是否破坏已有证据：冻结 fixture 来自旧快照，语料
    来自 PR 的原始文档，比较在一次性 Git 仓库里完成：

        python tools/ragkit_import.py verify --ragkit-root <trusted-loader> \
            --baseline <old-snapshot> --raw-dir <pr-raw-docs> \
            --checker <rag-fixture-check> --report <report.json>

职责边界
--------
- 只读 RAGKit 项目（qrels、annotation manifest、``data/raw`` 语料）、旧快照
  与 PR 原始语料；
- ``import``/``refresh`` 只写 ``--output`` 目录；``verify`` 只写 ``--report``，
  快照与 Git 操作全部发生在系统临时目录；
- 语料正文一律来自 RAGKit 的 ``load_file`` + ``normalize_text``，本文件不实现
  任何 Markdown 解析、front matter 剥离或空白归一；
- 不修改 RAGKit，不联网；``verify`` 只在临时仓库里调用 git，不碰调用者工作区。

为什么需要规范化快照
--------------------
RAGKit 的标注区间是在 ``normalize_text(load_file(path).text)`` 上算出来的。
直接拿原始 Markdown 当语料，front matter、HTML 注释、MDX 行、全角字符与行尾
空白都会让偏移错位，检查器会把大量本来有效的证据误报成 ``missing``。导入器
因此把"规范化后的正文"落成快照，fixture 里的 ``path`` 指向快照内部
（``corpus/...``），而不是原始文档路径。

为什么需要 refresh
------------------
文档改动后，旧 qrels 的引文与 manifest 摘要都会失效。若重新跑 ``import``，会被
旧引文或 manifest 校验挡住。``refresh`` 逐字节复制旧 fixture、只按旧路径重建
语料，于是旧引文能进入比较流程，由 Go ``check`` 报出哪些题受影响（``missing``、
``context_changed``），源文件被删除的路径则报 ``file_missing``。

承诺清单（import）
------------------
1. fixture 只包含有效题：无答案题与 ``needs_review`` 题分别跳过并计数；
2. 每条 evidence 的 ``exact`` 必须与标注区间 ``[start, end)`` 逐字节一致，
   否则记录问题并以退出码 1 结束；
3. 引文重复时，依据标注位置生成能唯一定位的前缀/后缀；无法唯一化则记录问题；
4. 有问题时一个输出文件都不写，问题打到 stderr，退出码 1——避免留下"半个
   快照"或让旧快照与失败摘要混在一起；
5. 输出里不出现本机绝对路径，便于把快照提交进仓库。

承诺清单（refresh）
------------------
1. fixture 逐字节复制，题号、引文与前后文选择器一律不改；
2. 旧引文不存在时刷新仍然成功，失效由 ``check`` 报 ``missing``；
3. 源文件确实不存在 → 新快照省略该文件（``check`` 报 ``file_missing``）；
   权限、解码、路径、写入失败 → 退出码 2，绝不伪装成删除；
4. 源文件重命名本轮按"旧路径缺失"处理；
5. 不校验 annotation manifest（它描述的是旧语料）；
6. 输出必须是新目录，且不得与旧快照重叠。

承诺清单（verify）
------------------
1. 复用 refresh 的冻结与重建逻辑，fixture 字节在两个提交里必须完全相同；
2. 比较发生在系统临时目录的一次性 Git 仓库里，调用者的工作区与配置不受影响；
3. 旧快照、可信加载器与 PR 原始语料全程只读；
4. 完整 JSON 报告写到 ``--report``；退出 1 时报告一定已经落盘；
5. 读取失败（权限、解码、路径）退出 2，绝不降级成 ``file_missing``；
6. 两份快照完全相同时仍然创建两个提交并跑通，返回 0。

退出码
------
0 成功；1 导入时有无法定位/校验失败的证据，或 verify 发现需复核的证据；2
环境或输入不可用（RAGKit 导入失败、qrels 摘要不匹配、输出目录冲突、源文档
读取失败、Git/检查器执行失败等）。``refresh`` 只返回 0 或 2。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

TOOL = "ragkit-fixture-import"
VERSION = "0.3.0"
SCHEMA_VERSION = 1

DEFAULT_QRELS = Path("data/eval/qrels.json")
DEFAULT_MANIFEST = Path("data/eval/annotation_manifest.json")
DEFAULT_RAW_DIR = Path("data/raw")
DEFAULT_CORPUS_PREFIX = "corpus"
FIXTURE_NAME = ".rag-fixtures.json"
SUMMARY_NAME = "import-summary.json"
REFRESH_SUMMARY_NAME = "refresh-summary.json"
REFRESH_MODE = "refresh"
VERIFY_MODE = "verify"

EXIT_OK = 0
EXIT_REVIEW = 1
EXIT_USAGE = 2

# 上下文按 8 的倍数增长，和参考实现的搜索策略保持一致：先给最短能唯一化的
# 上下文，避免把整段文章塞进 fixture。
CONTEXT_SIZES = (8, 16, 32, 64, 128, 256)
MAX_CONTEXT = CONTEXT_SIZES[-1]

# 与 Go 侧 internal/fixture 的限额保持一致，提前在导入阶段报清楚。
MAX_ID_BYTES = 200
MAX_QUESTION_BYTES = 4096
MAX_PATH_BYTES = 1024
MAX_EXACT_BYTES = 16 * 1024
MAX_CONTEXT_BYTES = 4 * 1024
MAX_CASES = 5000
MAX_EVIDENCE_PER_CASE = 200
MAX_EVIDENCE_TOTAL = 20000
MAX_FIXTURE_BYTES = 4 << 20
MAX_BLOB_BYTES = 8 << 20
LOCK_NAME = ".ragkit-import.lock"

COUNT_KEYS = (
    "queries",
    "cases",
    "anchors",
    "documents",
    "skippedNoAnswer",
    "skippedNeedsReview",
    "skippedWithoutEvidence",
    "disambiguatedAnchors",
)


class InputError(Exception):
    """输入或环境不可用，对应退出码 2。

    Notes:
        这类错误发生在"还没法开始逐条判定"的阶段，例如 RAGKit 导入失败、
        qrels 缺失、qrels 摘要与 manifest 不一致、输出目录已有快照。
    """


class DocumentError(Exception):
    """单篇文档无法加载或与 manifest 不一致。

    Notes:
        会被转换成带题号与路径的 :class:`Problem`，不影响其他文档继续检查。
    """


class SelectorError(Exception):
    """引文重复且给定上下文长度内无法唯一化。"""


@dataclass(frozen=True)
class Problem:
    """一条需要人工处理的导入问题。

    Args:
        case_id: 题号；无法从记录中取到题号时使用 ``<index N>``。
        doc: 仓库相对路径或原始 doc 标识；未知时为空串。
        reason: 可读原因，直接进入报告与摘要。

    Notes:
        问题对象只描述事实，不做补救；有问题时整次导入不产出 fixture。
    """

    case_id: str
    doc: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        """转换成摘要里的 JSON 结构。

        Returns:
            dict: 固定包含 ``id``、``doc``、``reason`` 三个键。
        """
        return {"id": self.case_id, "doc": self.doc, "reason": self.reason}


@dataclass(frozen=True)
class Inputs:
    """本次导入的输入侧事实，用于写入摘要。

    Args:
        qrels_sha256: qrels 原始字节的 SHA-256 十六进制摘要。
        manifest_present: 是否找到 annotation manifest。
        manifest_verified: qrels 摘要是否与 manifest 对得上。
        source_commit: manifest 记录的标注源提交；未知时为空串。

    Notes:
        这里不放任何绝对路径，摘要要能安全提交进仓库。
    """

    qrels_sha256: str
    manifest_present: bool
    manifest_verified: bool
    source_commit: str


@dataclass(frozen=True)
class ImportOptions:
    """一次导入的路径与开关。

    Args:
        root: RAGKit 项目根目录的绝对路径。
        output: 输出目录的绝对路径。
        qrels: qrels 文件绝对路径。
        manifest: annotation manifest 绝对路径；文件可能不存在。
        raw_dir: 原始语料目录绝对路径。
        corpus_prefix: 快照内的语料目录名，默认 ``corpus``，必须是单段路径。
        force: 是否允许覆盖已存在的快照。
    """

    root: Path
    output: Path
    qrels: Path
    manifest: Path
    raw_dir: Path
    corpus_prefix: str = DEFAULT_CORPUS_PREFIX
    force: bool = False


@dataclass
class Conversion:
    """逐题转换的中间结果。

    Args:
        cases: 已构造好的 fixture case 列表。
        problems: 转换阶段发现的问题。
        counts: 计数（queries/cases/anchors/跳过数等）。
        documents: doc 标识 → 规范化正文，仅包含真正被引用过的文档。
    """

    cases: list[dict]
    problems: list[Problem]
    counts: dict[str, int]
    documents: dict[str, str]


@dataclass
class ImportResult:
    """一次导入的完整结果，供写盘与打印使用。"""

    fixture: dict
    corpus: dict[str, str]
    summary: dict
    problems: list[Problem]

    @property
    def exit_code(self) -> int:
        """返回 CLI 退出码。

        Returns:
            int: 无问题为 0，存在问题为 1。
        """
        return EXIT_OK if not self.problems else EXIT_REVIEW


@dataclass(frozen=True)
class RefreshOptions:
    """一次语料刷新的路径与开关。

    Args:
        root: RAGKit 项目根目录的绝对路径，只读。
        baseline: 旧快照目录的绝对路径，只读。
        output: 新快照目录的绝对路径；必须与 baseline 不重叠。
        raw_dir: 原始语料目录绝对路径，默认 ``<root>/data/raw``。
        force: 是否允许替换输出目录里已有的 refresh 快照。

    Notes:
        这里没有 ``corpus_prefix``：快照内的语料目录名由旧 fixture 的路径推导。
        refresh 逐字节复制 fixture，因此也不能改路径前缀，否则新快照会自相矛盾。
    """

    root: Path
    baseline: Path
    output: Path
    raw_dir: Path
    force: bool = False


@dataclass
class RefreshResult:
    """一次刷新的完整结果，供写盘与打印使用。

    Args:
        fixture_bytes: 从旧快照逐字节复制的 fixture。
        corpus: 新快照路径 → 重新规范化后的正文。
        missing_paths: 源文件确实不存在、因而被省略的快照路径（升序）。
        summary: ``refresh-summary.json`` 的内容。

    Notes:
        缺失路径不是错误：它们让 Go ``check`` 报 ``file_missing``。只有读取
        失败（权限、解码、路径）才会让 refresh 以退出码 2 结束。
    """

    fixture_bytes: bytes
    corpus: dict[str, str]
    missing_paths: list[str]
    summary: dict


@dataclass(frozen=True)
class VerifyOptions:
    """一次 PR 证据校验的输入。

    Args:
        root: 可信的 RAGKit 加载器项目根目录，只读。
        baseline: 旧快照目录（含冻结 fixture 与基线语料），只读。
        raw_dir: PR 版本的原始语料目录，只读。
        checker: ``rag-fixture-check`` 可执行文件路径。
        report: 完整 JSON 报告的输出路径。

    Notes:
        ``raw_dir`` 与 ``root`` 分开，是为了让加载器只来自可信代码，PR 只提供
        数据。verify 不接受 ``--force``：它不写快照，只写报告。
    """

    root: Path
    baseline: Path
    raw_dir: Path
    checker: Path
    report: Path


@dataclass
class VerifyResult:
    """一次 PR 证据校验的结果。

    Args:
        report: 要写入 ``--report`` 的完整报告。
        exit_code: 0 表示全部有效，1 表示有证据需要复核。
        affected: 受影响的题号、状态与原问题，供终端打印。
    """

    report: dict
    exit_code: int
    affected: list[dict]


class RagkitBackend:
    """把 RAGKit 的加载与归一封装成"路径 → 规范化正文"。

    Args:
        load_file: RAGKit 的 ``load_file``，签名为 ``load_file(path) -> Document``。
        normalize_text: RAGKit 的 ``normalize_text``，签名为 ``text -> text``。

    Notes:
        本类不缓存、不校验；缓存与哈希校验由 :class:`DocumentReader` 负责，
        这样 backend 可以在测试里被替换成合成实现。
    """

    def __init__(self, load_file: Callable, normalize_text: Callable) -> None:
        self._load_file = load_file
        self._normalize_text = normalize_text

    def normalized_text(self, raw_path: Path) -> str:
        """读取并规范化一篇文档。

        Args:
            raw_path: 原始文档的绝对路径。

        Returns:
            str: RAGKit 规范化后的正文。

        Raises:
            RuntimeError: 加载结果没有 ``text`` 字符串，或归一结果不是字符串。
        """
        document = self._load_file(raw_path)
        text = getattr(document, "text", None)
        if not isinstance(text, str):
            raise RuntimeError("load_file(...).text is not a string")
        normalized = self._normalize_text(text)
        if not isinstance(normalized, str):
            raise RuntimeError("normalize_text(...) did not return a string")
        return normalized


class DocumentReader:
    """按需加载文档，缓存正文、错误与 manifest 校验结果。

    Args:
        options: 导入选项，提供 ``raw_dir``。
        backend: 具备 ``normalized_text(path)`` 的后端。
        manifest_hashes: doc 标识 → 期望的规范化正文 SHA-256；``None`` 表示跳过校验。

    Notes:
        同一文档常被多条证据引用，这里保证只加载与校验一次；错误也缓存，
        避免同一坏文件刷出上百条重复问题。
    """

    def __init__(
        self,
        options: ImportOptions,
        backend: RagkitBackend,
        manifest_hashes: dict[str, str] | None,
    ) -> None:
        self._options = options
        self._backend = backend
        self._manifest_hashes = manifest_hashes
        self._texts: dict[str, str] = {}
        self._errors: dict[str, str] = {}

    def __call__(self, doc: str) -> str:
        """返回文档的规范化正文。

        Args:
            doc: qrels 中的 doc 标识，使用正斜杠的仓库相对路径。

        Returns:
            str: 规范化正文。

        Raises:
            DocumentError: 文件无法加载、不在 manifest 覆盖范围内，或摘要不符。
        """
        if doc in self._texts:
            return self._texts[doc]
        if doc in self._errors:
            raise DocumentError(self._errors[doc])

        raw_path = self._options.raw_dir.joinpath(*doc.split("/"))
        try:
            if not raw_path.resolve().is_relative_to(self._options.raw_dir.resolve()):
                raise ValueError("document resolves outside the raw corpus")
            body = self._backend.normalized_text(raw_path)
            encoded = body.encode("utf-8")
            if len(encoded) > MAX_BLOB_BYTES:
                raise ValueError(f"normalized document exceeds {MAX_BLOB_BYTES} bytes")
        except Exception as exc:  # noqa: BLE001 - 任何后端异常都转成可读问题
            message = f"cannot load document: {exc}"
            self._errors[doc] = message
            raise DocumentError(message) from exc

        if self._manifest_hashes is not None:
            expected = self._manifest_hashes.get(doc)
            if expected is None:
                message = "document is not covered by the annotation manifest"
                self._errors[doc] = message
                raise DocumentError(message)
            actual = hashlib.sha256(encoded).hexdigest()
            if actual != expected:
                message = (
                    "normalized text does not match the manifest hash "
                    f"(expected {expected[:12]}..., got {actual[:12]}...)"
                )
                self._errors[doc] = message
                raise DocumentError(message)

        self._texts[doc] = body
        return body

    @property
    def documents(self) -> dict[str, str]:
        """返回已加载文档的快照副本。"""
        return dict(self._texts)


def occurrences(text: str, quote: str) -> list[int]:
    """列出引文在文本中的所有起始偏移，包含重叠出现。

    Args:
        text: 规范化正文。
        quote: 非空字面引文。

    Returns:
        list[int]: 升序偏移列表；无匹配时为空列表。

    Notes:
        与 Go 侧 ``internal/match`` 的计数方式一致：每次命中后只前进一个字符，
        因此 ``"aa"`` 在 ``"aaa"`` 中算两次。
    """
    offsets: list[int] = []
    start = 0
    while (index := text.find(quote, start)) >= 0:
        offsets.append(index)
        start = index + 1
    return offsets


def context_matches(text: str, offset: int, quote: str, prefix: str, suffix: str) -> bool:
    """判断某次出现的前后是否与选择器完全吻合。

    Args:
        text: 规范化正文。
        offset: 引文起始偏移。
        quote: 字面引文。
        prefix: 紧邻引文之前的文本，空串表示不约束。
        suffix: 紧邻引文之后的文本，空串表示不约束。

    Returns:
        bool: 前后都吻合返回 True。

    Notes:
        必须是"紧邻"而不是"包含"。这条语义与 Go 侧完全一致，否则导入器生成
        的选择器会在检查阶段被判定为 context_changed。
    """
    start = offset - len(prefix)
    end = offset + len(quote) + len(suffix)
    if start < 0 or end > len(text):
        return False
    if prefix and text[start:offset] != prefix:
        return False
    if suffix and text[offset + len(quote):end] != suffix:
        return False
    return True


def build_selector(text: str, start: int, end: int, quote: str) -> tuple[str, str]:
    """为标注位置生成能唯一定位的前缀/后缀。

    Args:
        text: 规范化正文。
        start: 标注区间起点（含）。
        end: 标注区间终点（不含）。
        quote: 与 ``text[start:end]`` 完全一致的引文。

    Returns:
        tuple[str, str]: ``(prefix, suffix)``；引文本来就唯一时返回两个空串。

    Raises:
        SelectorError: 在 ``MAX_CONTEXT`` 个字符的上下文内仍无法唯一化。

    Notes:
        唯一性以"候选偏移集合等于 ``[start]``"为准，因此不会因为另一处引文
        恰好被前缀匹配而误判；每次只扩展一侧的窗口，窗口长度按 8 的倍数递增。
    """
    matches = occurrences(text, quote)
    if matches == [start]:
        return "", ""
    for size in CONTEXT_SIZES:
        prefix = text[max(0, start - size):start]
        suffix = text[end:end + size]
        if not prefix and not suffix:
            continue
        selected = [
            offset
            for offset in matches
            if context_matches(text, offset, quote, prefix, suffix)
        ]
        if selected == [start]:
            return prefix, suffix
    raise SelectorError(
        f"quote occurs {len(matches)} times and cannot be narrowed to the annotated span "
        f"with {MAX_CONTEXT} characters of context"
    )


def validate_repo_path(path: str) -> str:
    """校验一个仓库相对路径是否可安全写进 fixture 与快照。

    Args:
        path: 待校验路径，必须使用正斜杠。

    Returns:
        str: 原样返回合法路径。

    Raises:
        ValueError: 路径为空、超长、绝对、含反斜杠/冒号/控制字符或危险段。

    Notes:
        规则与 Go 侧 ``fixture.ValidatePath`` 对齐：中文与空格允许，``.``/``..``
        与空段拒绝。这样导入阶段就能拦住会让 Go 检查器直接退出 2 的路径。
    """
    if not path:
        raise ValueError("path is empty")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError(f"path is longer than {MAX_PATH_BYTES} bytes")
    if path.startswith("/"):
        raise ValueError("path must be repo-relative (no leading '/')")
    if "\\" in path:
        raise ValueError("path must use forward slashes")
    if ":" in path:
        raise ValueError("path must not contain ':'")
    for char in path:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise ValueError("path contains control characters")
    for segment in path.split("/"):
        if segment == "":
            raise ValueError("path has an empty segment")
        if segment in (".", ".."):
            raise ValueError(f"path must not contain a '{segment}' segment")
        if segment.casefold() == ".git" or segment.endswith((".", " ")):
            raise ValueError(f"path segment {segment!r} is unsafe for a Git snapshot")
        if any(char in segment for char in '<>"|?*') or re.fullmatch(
            r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", segment.split(".")[0].rstrip(" ")
        ):
            raise ValueError(f"path segment {segment!r} is not portable")
    return path


def import_cases(
    items: Iterable[dict],
    read_document: Callable[[str], str],
    *,
    corpus_prefix: str = DEFAULT_CORPUS_PREFIX,
) -> Conversion:
    """把 qrels 条目转换成 fixture case 列表。

    Args:
        items: qrels 顶层数组。
        read_document: ``doc -> 规范化正文`` 的可调用对象；失败时抛 DocumentError。
        corpus_prefix: 快照内语料目录名，默认 ``corpus``。

    Returns:
        Conversion: cases、problems、计数与已加载文档。

    Notes:
        跳过规则是确定的：``annotation_status == "needs_review"`` 先跳过；
        没有任何 answer point 的题记为无答案；有 answer 但一条 evidence 都
        没有的题记入 ``skippedWithoutEvidence``。只要有证据定位失败，该题不会
        进入 fixture，整次导入也会因为 problems 非空而以退出码 1 结束。
    """
    cases: list[dict] = []
    problems: list[Problem] = []
    counts: Counter[str] = Counter()
    loaded: dict[str, str] = {}
    seen_ids: set[str] = set()

    def read(doc: str) -> str:
        body = read_document(doc)
        loaded[doc] = body
        return body

    for index, item in enumerate(items):
        counts["queries"] += 1
        if not isinstance(item, dict):
            problems.append(Problem(f"<index {index}>", "", "case record is not an object"))
            continue

        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            problems.append(Problem(f"<index {index}>", "", "case id is missing or empty"))
            continue
        case_id = raw_id
        if case_id in seen_ids:
            problems.append(Problem(case_id, "", "duplicate case id"))
            continue
        seen_ids.add(case_id)

        if item.get("annotation_status") == "needs_review":
            counts["skippedNeedsReview"] += 1
            continue
        answers = item.get("answers")
        if not isinstance(answers, list):
            problems.append(Problem(case_id, "", "answers must be an array"))
            continue
        if not answers:
            counts["skippedNoAnswer"] += 1
            continue

        question = item.get("query")
        if not isinstance(question, str) or not question.strip():
            problems.append(Problem(case_id, "", "query must be a nonempty string"))
            continue
        evidence: list[dict] = []
        failed = False
        for answer in answers:
            if not isinstance(answer, dict):
                problems.append(Problem(case_id, "", "answer point is not an object"))
                failed = True
                continue
            raw_evidence = answer.get("evidence")
            if not isinstance(raw_evidence, list):
                problems.append(Problem(case_id, "", "evidence must be an array"))
                failed = True
                continue
            for raw in raw_evidence:
                anchor, problem = _build_anchor(case_id, raw, read, corpus_prefix)
                if problem is not None:
                    problems.append(problem)
                    failed = True
                    continue
                evidence.append(anchor)

        if failed:
            continue
        if not evidence:
            counts["skippedWithoutEvidence"] += 1
            continue
        counts["disambiguatedAnchors"] += sum(
            1 for anchor in evidence if anchor["prefix"] or anchor["suffix"]
        )
        cases.append({"id": case_id, "question": question, "evidence": evidence})

    counts["cases"] = len(cases)
    counts["anchors"] = sum(len(case["evidence"]) for case in cases)
    counts["documents"] = len(loaded)
    return Conversion(
        cases=cases,
        problems=problems,
        counts={key: int(counts.get(key, 0)) for key in COUNT_KEYS},
        documents=loaded,
    )


def _build_anchor(
    case_id: str,
    raw: object,
    read_document: Callable[[str], str],
    corpus_prefix: str,
) -> tuple[dict | None, Problem | None]:
    """把一条原始 evidence 转成 fixture 锚点。

    Args:
        case_id: 题号，用于问题报告。
        raw: qrels 中的 evidence 对象。
        read_document: 文档读取函数。
        corpus_prefix: 语料目录名。

    Returns:
        tuple: 成功时 ``(anchor, None)``，失败时 ``(None, problem)``。

    Notes:
        校验顺序固定：结构 → 路径 → 文档 → 区间 → 引文 → 选择器。先做便宜的
        结构校验，避免为一篇根本不会用到的文档触发加载。
    """
    if not isinstance(raw, dict):
        return None, Problem(case_id, "", "evidence is not an object")
    doc = raw.get("doc")
    if not isinstance(doc, str) or not doc:
        return None, Problem(case_id, "", "evidence has no doc")
    quote = raw.get("quote")
    if not isinstance(quote, str) or not quote:
        return None, Problem(case_id, doc, "evidence has no quote")
    if len(quote.encode("utf-8")) > MAX_EXACT_BYTES:
        return None, Problem(case_id, doc, f"quote is longer than {MAX_EXACT_BYTES} bytes")

    start = raw.get("start")
    end = raw.get("end")
    if not _is_int(start) or not _is_int(end):
        return None, Problem(case_id, doc, "evidence start/end must be integers")

    try:
        repo_path = validate_repo_path(f"{corpus_prefix}/{doc}")
    except ValueError as exc:
        return None, Problem(case_id, doc, f"invalid evidence path: {exc}")

    try:
        body = read_document(doc)
    except DocumentError as exc:
        return None, Problem(case_id, repo_path, str(exc))

    if start < 0 or end <= start or end > len(body):
        return None, Problem(
            case_id,
            repo_path,
            f"span {start}..{end} is outside the normalized document (length {len(body)})",
        )
    if body[start:end] != quote:
        return None, Problem(
            case_id,
            repo_path,
            f"quote does not match the annotated span {start}..{end}",
        )

    try:
        prefix, suffix = build_selector(body, start, end, quote)
    except SelectorError as exc:
        return None, Problem(case_id, repo_path, str(exc))
    if len(prefix.encode("utf-8")) > MAX_CONTEXT_BYTES or len(suffix.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return None, Problem(case_id, repo_path, "generated context exceeds the selector size limit")
    return {"path": repo_path, "exact": quote, "prefix": prefix, "suffix": suffix}, None


def _is_int(value: object) -> bool:
    """判断值是否为真正的整数（排除 bool）。

    Args:
        value: 任意值。

    Returns:
        bool: ``True`` 表示可直接参与区间运算。

    Notes:
        ``bool`` 是 ``int`` 的子类，JSON 里的 ``true`` 不能被当成偏移 1。
    """
    return isinstance(value, int) and not isinstance(value, bool)


def verify_fixture(fixture: dict, corpus: dict[str, str]) -> list[Problem]:
    """用与 Go 检查器相同的匹配规则复验 fixture 里的每个锚点。

    Args:
        fixture: 待写出的 fixture 文档。
        corpus: 仓库路径 → 规范化正文。

    Returns:
        list[Problem]: 每个无法唯一定位的锚点一条；全部可定位时为空列表。

    Notes:
        判定与 ``internal/match.Evaluate`` 的 intact 路径一致：先数字面出现
        次数，再用前缀/后缀过滤，必须恰好剩一个候选。这层复验不能代替真实
        ``check``（行号、重命名、baseline 对比仍由 Go 侧负责），但它保证
        "写出去的 fixture 在快照里一定能锚定"，也让导入器在没有 Go 工具链的
        环境里仍能自证产物可用。
    """
    problems: list[Problem] = []
    for case in fixture["cases"]:
        for index, anchor in enumerate(case["evidence"]):
            body = corpus.get(anchor["path"])
            if body is None:
                problems.append(
                    Problem(case["id"], anchor["path"], f"evidence {index}: snapshot is missing this document")
                )
                continue
            selected = [
                offset
                for offset in occurrences(body, anchor["exact"])
                if context_matches(body, offset, anchor["exact"], anchor["prefix"], anchor["suffix"])
            ]
            if len(selected) != 1:
                problems.append(
                    Problem(
                        case["id"],
                        anchor["path"],
                        f"evidence {index}: anchor resolves to {len(selected)} locations in the snapshot",
                    )
                )
    return problems


def check_fixture_limits(cases: Sequence[dict]) -> list[Problem]:
    """检查 fixture 是否满足 Go 侧的硬上限。

    Args:
        cases: 已构造的 case 列表。

    Returns:
        list[Problem]: 违规项；全部合规时为空。

    Notes:
        这些上限由 Go 侧 ``internal/fixture`` 强制，提前检查可以把"导入成功、
        检查时才退出 2"的意外挡在导入阶段。
    """
    problems: list[Problem] = []
    if len(cases) > MAX_CASES:
        problems.append(Problem("", "", f"{len(cases)} cases exceed the limit of {MAX_CASES}"))
    total = 0
    for case in cases:
        total += len(case["evidence"])
        if len(case["evidence"]) > MAX_EVIDENCE_PER_CASE:
            problems.append(
                Problem(case["id"], "", f"{len(case['evidence'])} evidence entries exceed {MAX_EVIDENCE_PER_CASE}")
            )
        if len(case["id"].encode("utf-8")) > MAX_ID_BYTES:
            problems.append(Problem(case["id"], "", f"case id is longer than {MAX_ID_BYTES} bytes"))
        if len(case.get("question", "").encode("utf-8")) > MAX_QUESTION_BYTES:
            problems.append(Problem(case["id"], "", f"query is longer than {MAX_QUESTION_BYTES} bytes"))
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in case["id"]):
            problems.append(Problem(case["id"], "", "case id contains control characters"))
    if total > MAX_EVIDENCE_TOTAL:
        problems.append(Problem("", "", f"{total} evidence anchors exceed the limit of {MAX_EVIDENCE_TOTAL}"))
    return problems


def run_import(options: ImportOptions, backend: RagkitBackend) -> ImportResult:
    """执行一次完整导入（不写盘）。

    Args:
        options: 路径与开关。
        backend: 文档加载与归一后端。

    Returns:
        ImportResult: fixture、语料、摘要与问题列表。

    Raises:
        InputError: 输入不可用（qrels 缺失/非法、摘要不匹配、输出目录冲突等）。

    Notes:
        所有结果先在内存里构造；只有 ``problems`` 为空时调用方才应写快照，
        这样失败的一次运行不会留下半个语料目录。
    """
    _existing_outputs(options)

    qrels_bytes = _read_bytes(options.qrels, "qrels")
    items = _parse_qrels(qrels_bytes)
    manifest, hashes, source_commit = _load_manifest(options.manifest)

    manifest_verified = False
    if manifest is not None:
        expected = manifest.get("qrels_sha256")
        actual = hashlib.sha256(qrels_bytes).hexdigest()
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise InputError("annotation manifest is missing a valid qrels_sha256")
        if expected != actual:
            raise InputError(
                "qrels.json does not match its annotation manifest "
                f"(expected {expected[:12]}..., got {actual[:12]}...)"
            )
        manifest_verified = True

    reader = DocumentReader(options, backend, hashes)
    conversion = import_cases(items, reader, corpus_prefix=options.corpus_prefix)
    problems = list(conversion.problems) + check_fixture_limits(conversion.cases)
    loaded = reader.documents

    corpus: dict[str, str] = {}
    portable_paths: set[str] = set()
    for doc, body in loaded.items():
        try:
            path = validate_repo_path(f"{options.corpus_prefix}/{doc}")
            if path.casefold() in portable_paths:
                raise ValueError("path conflicts with another corpus path ignoring case")
            portable_paths.add(path.casefold())
            corpus[path] = body
        except ValueError as exc:
            problems.append(Problem("", doc, f"invalid snapshot path: {exc}"))

    fixture = {
        "version": 1,
        "cases": [
            {
                "id": case["id"],
                "question": case["question"],
                "evidence": [
                    {
                        "path": anchor["path"],
                        "exact": anchor["exact"],
                        "prefix": anchor["prefix"],
                        "suffix": anchor["suffix"],
                    }
                    for anchor in case["evidence"]
                ],
            }
            for case in conversion.cases
        ],
    }
    problems.extend(verify_fixture(fixture, corpus))
    if len(_json_bytes(fixture)) > MAX_FIXTURE_BYTES:
        problems.append(Problem("", "", f"fixture exceeds {MAX_FIXTURE_BYTES} bytes"))
    summary = _build_summary(options, conversion.counts, problems, Inputs(
        qrels_sha256=hashlib.sha256(qrels_bytes).hexdigest(),
        manifest_present=manifest is not None,
        manifest_verified=manifest_verified,
        source_commit=source_commit,
    ))
    return ImportResult(fixture=fixture, corpus=corpus, summary=summary, problems=problems)


def _existing_outputs(options: ImportOptions) -> list[str]:
    """Only replace entries belonging to a complete, recognized import."""
    existing = []
    for name in (FIXTURE_NAME, SUMMARY_NAME, options.corpus_prefix):
        path = options.output / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        is_link = stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        correct_type = (stat.S_ISDIR(info.st_mode) if name == options.corpus_prefix
                        else stat.S_ISREG(info.st_mode))
        if is_link or not correct_type:
            raise InputError(f"output entry is not a regular snapshot file or directory: {path}")
        existing.append(name)
    if not existing:
        return existing
    if not options.force:
        raise InputError(f"output already contains a snapshot ({existing[0]}); pass --force to replace it")
    if len(existing) != 3:
        raise InputError("--force requires a complete prior snapshot; use a new output directory")
    try:
        summary = json.loads((options.output / SUMMARY_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InputError("--force requires a valid prior import summary") from exc
    if not isinstance(summary, dict) or summary.get("tool") != TOOL or (
        summary.get("schemaVersion") != SCHEMA_VERSION or summary.get("status") != "ok"
        or not isinstance(summary.get("outputs"), dict)
        or summary["outputs"].get("corpus") != options.corpus_prefix
    ):
        raise InputError("--force requires an existing importer snapshot with the same corpus prefix")
    return existing


def _read_bytes(path: Path, label: str) -> bytes:
    """读取文件原始字节。

    Args:
        path: 文件路径。
        label: 报错时使用的字段名。

    Returns:
        bytes: 文件内容。

    Raises:
        InputError: 文件不存在或不可读。
    """
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read {label} {path}: {exc}") from exc


def _parse_qrels(data: bytes) -> list[dict]:
    """解析 qrels JSON。

    Args:
        data: 文件原始字节，允许带 UTF-8 BOM。

    Returns:
        list[dict]: 顶层数组。

    Raises:
        InputError: 不是 JSON、不是数组，或元素不是对象。
    """
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"qrels is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise InputError("qrels must be a JSON array")
    if any(not isinstance(item, dict) for item in value):
        raise InputError("every qrels entry must be a JSON object")
    return value


def _load_manifest(path: Path) -> tuple[dict | None, dict[str, str] | None, str]:
    """读取 annotation manifest。

    Args:
        path: manifest 路径。

    Returns:
        tuple: ``(manifest, corpus_hashes, source_commit)``。文件不存在时返回
        ``(None, None, "")``。

    Raises:
        InputError: manifest 存在但不是合法 JSON 对象。
    """
    if not path.exists():
        return None, None, ""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"annotation manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InputError("annotation manifest must be a JSON object")
    raw_hashes = manifest.get("corpus_sha256")
    if not isinstance(raw_hashes, dict) or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in raw_hashes.values()
    ):
        raise InputError("annotation manifest must contain a valid corpus_sha256 object")
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit):
        source_commit = ""
    return manifest, raw_hashes, source_commit


def _build_summary(
    options: ImportOptions,
    counts: dict[str, int],
    problems: Sequence[Problem],
    inputs: Inputs,
) -> dict:
    """组装导入摘要。

    Args:
        options: 导入选项。
        counts: 计数。
        problems: 问题列表。
        inputs: 输入侧事实。

    Returns:
        dict: 可直接序列化的摘要；不含绝对路径。

    Notes:
        摘要描述"已经写出的快照"，因此只在导入无问题时落盘。``status`` 字段
        仍保留，是为了让读者一眼看出这份摘要是成功结果。
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "version": VERSION,
        "status": "ok" if not problems else "failed",
        "inputs": {
            "qrels": _display_path(options.root, options.qrels),
            "qrelsSha256": inputs.qrels_sha256,
            "rawDir": _display_path(options.root, options.raw_dir),
            "manifest": _display_path(options.root, options.manifest),
            "manifestPresent": inputs.manifest_present,
            "manifestVerified": inputs.manifest_verified,
            "annotationSourceCommit": inputs.source_commit,
        },
        "outputs": {
            "fixture": FIXTURE_NAME,
            "corpus": options.corpus_prefix,
            "summary": SUMMARY_NAME,
        },
        "counts": counts,
        "problems": [problem.as_dict() for problem in problems],
    }


def _display_path(root: Path, path: Path) -> str:
    """把路径转成可提交的相对形式。

    Args:
        root: RAGKit 根目录。
        path: 目标路径。

    Returns:
        str: 位于根目录内时为相对 POSIX 路径，否则退化为文件名。

    Notes:
        摘要可能被提交进仓库，因此这里绝不返回绝对路径。
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def resolve_options(args: argparse.Namespace) -> ImportOptions:
    """把命令行参数解析成绝对路径选项并做基础校验。

    Args:
        args: ``argparse`` 解析结果。

    Returns:
        ImportOptions: 可直接使用的选项。

    Raises:
        InputError: 根目录不是目录、原始语料目录缺失、corpus 前缀非法。
    """
    root = Path(args.ragkit_root).expanduser().resolve()
    if not root.is_dir():
        raise InputError(f"--ragkit-root is not a directory: {root}")

    if root.name == "src" and (root / "ragkit" / "__init__.py").is_file():
        root = root.parent

    def under_root(value: str | Path) -> Path:
        path = Path(value).expanduser()
        return (root / path).resolve() if not path.is_absolute() else path.resolve()

    output = Path(args.output).expanduser().resolve()
    if any(part.casefold() == ".git" for part in output.parts):
        raise InputError("--output must not be inside a Git metadata directory")
    qrels = under_root(args.qrels)
    manifest = under_root(args.manifest)
    raw_dir = under_root(args.raw_dir)
    if not raw_dir.is_dir():
        raise InputError(f"raw corpus directory does not exist: {raw_dir}")
    for protected in (root, raw_dir, qrels, manifest):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise InputError("--output must not overlap the RAGKit checkout or its inputs")
    if output.exists() and not output.is_dir():
        raise InputError(f"--output is not a directory: {output}")
    corpus_prefix = args.corpus_prefix
    try:
        validate_repo_path(corpus_prefix)
    except ValueError as exc:
        raise InputError(f"--corpus-prefix is invalid: {exc}") from exc
    if "/" in corpus_prefix:
        raise InputError("--corpus-prefix must be a single path segment")
    if corpus_prefix.casefold() in {FIXTURE_NAME, SUMMARY_NAME, REFRESH_SUMMARY_NAME, LOCK_NAME}:
        raise InputError("--corpus-prefix is reserved for an importer file")
    return ImportOptions(
        root=root,
        output=output,
        qrels=qrels,
        manifest=manifest,
        raw_dir=raw_dir,
        corpus_prefix=corpus_prefix,
        force=bool(args.force),
    )


def load_ragkit_backend(root: Path) -> RagkitBackend:
    """从 RAGKit 项目加载 ``load_file`` 与 ``normalize_text``。

    Args:
        root: RAGKit 项目根目录；也接受 ``src`` 目录本身。

    Returns:
        RagkitBackend: 使用真实 RAGKit 实现的后端。

    Raises:
        InputError: 找不到 ``ragkit`` 包，或导入/取不到函数。

    Notes:
        候选顺序是 ``<root>/ragkit`` 与 ``<root>/src/ragkit``，找到哪个就把它的
        父目录插到 ``sys.path`` 最前面（去重），因此项目内代码优先于已安装版本。
    """
    package_root = _ragkit_package_root(root)
    if package_root is None:
        raise InputError(
            "cannot find the ragkit package; expected "
            f"{root / 'ragkit'} or {root / 'src' / 'ragkit'}"
        )
    entry = str(package_root)
    if entry not in sys.path:
        sys.modules.pop("ragkit", None)
        sys.path.insert(0, entry)
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loaders = importlib.import_module("ragkit.loaders")
        normalize_module = importlib.import_module("ragkit.text.normalize")
    except Exception as exc:  # noqa: BLE001 - 导入失败原因需要原样透传
        raise InputError(f"cannot import RAGKit from {package_root}: {exc}") from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting

    load_file = getattr(loaders, "load_file", None)
    normalize_text = getattr(normalize_module, "normalize_text", None)
    if not callable(load_file) or not callable(normalize_text):
        raise InputError(
            "RAGKit is missing ragkit.loaders.load_file or ragkit.text.normalize.normalize_text"
        )
    return RagkitBackend(load_file, normalize_text)


def _ragkit_package_root(root: Path) -> Path | None:
    """定位包含 ``ragkit`` 包的目录。

    Args:
        root: 项目根或 ``src`` 目录。

    Returns:
        Path | None: 找到时返回应加入 ``sys.path`` 的目录。
    """
    for candidate in (root, root / "src"):
        if (candidate / "ragkit" / "__init__.py").is_file():
            return candidate
    return None


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _install_snapshot(
    output: Path,
    corpus_prefix: str,
    existing_outputs: Callable[[], Sequence[str]],
    corpus: dict[str, str],
    documents: dict[str, bytes],
) -> None:
    """把已构造好的快照暂存后整体换进输出目录。

    Args:
        output: 输出目录；不存在时创建。
        corpus_prefix: 语料子目录名，例如 ``corpus``。
        existing_outputs: 在持锁时重新验证、返回可替换的快照条目。
        corpus: 快照路径 → 正文。
        documents: 输出根目录下的 JSON 文件名 → 原始字节。

    Raises:
        OSError: 写入、改名或回滚失败。

    Notes:
        先在同一目录下建 staging，把全部内容写完后才改名替换；中途失败会把已经
        换上去的条目搬回 staging、把旧条目搬回原位。回滚本身失败时保留 staging
        并在错误里给出路径，避免静默丢数据。
    """
    output.mkdir(parents=True, exist_ok=True)
    lock = output / LOCK_NAME
    lock.mkdir()
    stage = None
    keep_backup = False
    try:
        existing = existing_outputs()
        stage = Path(tempfile.mkdtemp(prefix=".ragkit-stage-", dir=output))
        incoming = stage / "incoming"
        backup = stage / "previous"
        (incoming / corpus_prefix).mkdir(parents=True)
        backup.mkdir()
        for repo_path, content in sorted(corpus.items()):
            target = incoming.joinpath(*repo_path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content.encode("utf-8"))
        for name, data in documents.items():
            with (incoming / name).open("xb") as stream:
                stream.write(data)
        moved, installed = [], []
        try:
            for name in existing:
                (output / name).rename(backup / name)
                moved.append(name)
            for name in [corpus_prefix, *documents]:
                (incoming / name).rename(output / name)
                installed.append(name)
        except OSError:
            try:
                for name in reversed(installed):
                    (output / name).rename(incoming / name)
                for name in reversed(moved):
                    (backup / name).rename(output / name)
            except OSError as exc:
                keep_backup = True
                raise OSError(f"snapshot rollback failed; the staging directory was kept at {stage}") from exc
            raise
    finally:
        try:
            if stage is not None and not keep_backup:
                shutil.rmtree(stage)
        finally:
            lock.rmdir()


def write_snapshot(options: ImportOptions, result: ImportResult) -> None:
    """把导入结果写入快照目录。

    Args:
        options: 导入选项，提供输出目录、语料前缀与 ``--force``。
        result: 导入结果。

    Raises:
        InputError: 输出目录已有快照且不满足替换条件。
        OSError: 写入失败。
    """
    _install_snapshot(
        options.output,
        options.corpus_prefix,
        lambda: _existing_outputs(options),
        result.corpus,
        {
            FIXTURE_NAME: _json_bytes(result.fixture),
            SUMMARY_NAME: _json_bytes(result.summary),
        },
    )


def print_report(options: ImportOptions, result: ImportResult) -> None:
    """把导入结果打印到终端。

    Args:
        options: 导入选项。
        result: 导入结果。

    Notes:
        成功信息写 stdout，问题写 stderr；这样 CI 里 ``> report.txt`` 不会把
        失败原因一起吞掉。
    """
    counts = result.summary["counts"]
    print(f"{TOOL} {VERSION}")
    print(f"ragkit root:  {options.root}")
    print(f"fixture:      {FIXTURE_NAME}")
    print(f"corpus:       {options.corpus_prefix}/ ({counts['documents']} documents)")
    print(
        f"cases:        {counts['cases']} (skipped {counts['skippedNoAnswer']} no-answer, "
        f"{counts['skippedNeedsReview']} needs-review, "
        f"{counts['skippedWithoutEvidence']} without evidence)"
    )
    print(
        f"anchors:      {counts['anchors']} "
        f"({counts['disambiguatedAnchors']} with generated context)"
    )
    if result.problems:
        print(f"problems:     {len(result.problems)} (see stderr)", file=sys.stderr)
        for problem in result.problems:
            location = f"{problem.case_id} {problem.doc}".strip()
            print(f"  - {location}: {problem.reason}", file=sys.stderr)
        print(
            f"status: failed (exit {EXIT_REVIEW}); no output was written, "
            "fix the problems above and re-run",
            file=sys.stderr,
        )
    else:
        print(f"summary:      {SUMMARY_NAME}")
        print("status:       ok")


def parse_frozen_fixture(data: bytes) -> dict:
    """解析旧快照里的 fixture，提取可安全复用的结构。

    Args:
        data: 旧 ``.rag-fixtures.json`` 的原始字节。

    Returns:
        dict: 解析后的 fixture 文档。

    Raises:
        InputError: 文档过大、不是合法 JSON、版本不是 1，或证据字段不合法。

    Notes:
        refresh 不重算引文，也不判断引文是否还存在；这里只保证"每条 evidence
        都能读出 path，且结构满足 Go 侧的 fixture 契约"。结构坏掉必须在这里
        失败，而不是等 Go ``check`` 用退出码 2 告诉用户。
    """
    if len(data) > MAX_FIXTURE_BYTES:
        raise InputError(f"baseline fixture exceeds {MAX_FIXTURE_BYTES} bytes")
    try:
        fixture = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"baseline fixture is not valid JSON: {exc}") from exc
    if not isinstance(fixture, dict):
        raise InputError("baseline fixture must be a JSON object")
    if set(fixture) - {"$schema", "version", "cases"}:
        raise InputError("baseline fixture contains unknown fields")
    if not _is_int(fixture.get("version")) or fixture["version"] != 1:
        raise InputError("baseline fixture must have version 1")
    if not isinstance(fixture.get("$schema", ""), str):
        raise InputError("baseline fixture $schema must be a string")
    cases = fixture.get("cases")
    if not isinstance(cases, list):
        raise InputError("baseline fixture must contain a cases array")
    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise InputError(f"baseline fixture cases[{index}] is not an object")
        if set(case) - {"id", "question", "evidence"}:
            raise InputError(f"baseline fixture cases[{index}] contains unknown fields")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise InputError(f"baseline fixture cases[{index}] has no id")
        if case_id in seen_ids:
            raise InputError(f"baseline fixture has duplicate case id {case_id!r}")
        seen_ids.add(case_id)
        if not isinstance(case.get("question", ""), str):
            raise InputError(f"baseline fixture case {case_id!r} question is not a string")
        evidence = case.get("evidence")
        if not isinstance(evidence, list):
            raise InputError(f"baseline fixture case {case_id!r} has no evidence array")
        for position, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise InputError(f"baseline fixture case {case_id!r} evidence[{position}] is not an object")
            if set(item) - {"path", "exact", "prefix", "suffix"}:
                raise InputError(f"baseline fixture case {case_id!r} evidence[{position}] contains unknown fields")
            path = item.get("path")
            if not isinstance(path, str):
                raise InputError(f"baseline fixture case {case_id!r} evidence[{position}] has no path")
            try:
                validate_repo_path(path)
            except ValueError as exc:
                raise InputError(
                    f"baseline fixture case {case_id!r} evidence[{position}] path: {exc}"
                ) from exc
            exact = item.get("exact")
            if not isinstance(exact, str) or not exact:
                raise InputError(f"baseline fixture case {case_id!r} evidence[{position}] has no exact quote")
            if len(exact.encode("utf-8")) > MAX_EXACT_BYTES:
                raise InputError(f"baseline fixture case {case_id!r} evidence[{position}] exact is too long")
            for selector in ("prefix", "suffix"):
                value = item.get(selector, "")
                if not isinstance(value, str):
                    raise InputError(
                        f"baseline fixture case {case_id!r} evidence[{position}] {selector} is not a string"
                    )
                if len(value.encode("utf-8")) > MAX_CONTEXT_BYTES:
                    raise InputError(
                        f"baseline fixture case {case_id!r} evidence[{position}] {selector} is too long"
                    )
    limits = check_fixture_limits(cases)
    if limits:
        raise InputError("baseline fixture violates limits: " + "; ".join(problem.reason for problem in limits))
    return fixture


def fixture_paths(fixture: dict) -> list[str]:
    """按首次出现顺序返回 fixture 引用的快照路径（去重）。

    Args:
        fixture: 已通过 :func:`parse_frozen_fixture` 的文档。

    Returns:
        list[str]: 需要重建的快照路径。

    Notes:
        顺序只影响重建与摘要，不影响 fixture 字节；去重避免同一文档被重复
        加载、重复写入。
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for case in fixture["cases"]:
        for item in case["evidence"]:
            path = item["path"]
            if path not in seen:
                seen.add(path)
                ordered.append(path)
    return ordered


def split_corpus_paths(paths: Sequence[str]) -> tuple[str, list[tuple[str, str]]]:
    """把快照路径拆成语料前缀与 ``(doc, 快照路径)`` 列表。

    Args:
        paths: :func:`fixture_paths` 的结果。

    Returns:
        tuple: ``(prefix, entries)``；没有路径时前缀回退为 ``corpus``。

    Raises:
        InputError: 路径没有语料前缀段，或同一份 fixture 混用了多个前缀。

    Notes:
        refresh 不接收 ``--corpus-prefix``：前缀必须与冻结 fixture 一致，
        否则复制过来的 fixture 会指向一个不存在的目录。
    """
    if not paths:
        return DEFAULT_CORPUS_PREFIX, []
    prefix: str | None = None
    entries: list[tuple[str, str]] = []
    portable_paths: set[str] = set()
    for path in paths:
        head, separator, rest = path.partition("/")
        if not separator or not rest:
            raise InputError(f"fixture path has no corpus prefix: {path}")
        if head.casefold() in {FIXTURE_NAME, SUMMARY_NAME, REFRESH_SUMMARY_NAME, LOCK_NAME}:
            raise InputError(f"fixture path has a reserved corpus prefix: {path}")
        if path.casefold() in portable_paths:
            raise InputError(f"fixture paths conflict ignoring case: {path}")
        portable_paths.add(path.casefold())
        if prefix is None:
            prefix = head
        elif head != prefix:
            raise InputError(f"fixture mixes corpus prefixes {prefix!r} and {head!r}")
        entries.append((rest, path))
    return prefix or DEFAULT_CORPUS_PREFIX, entries


def _regular_source(path: Path, root: Path, repo_path: str) -> bool:
    """Return false for absent files and reject unreadable or non-regular entries."""
    try:
        cursor = root
        info = cursor.stat()
        for segment in path.relative_to(root).parts:
            if not stat.S_ISDIR(info.st_mode):
                raise InputError(f"cannot read {repo_path}: source parent is not a directory")
            cursor /= segment
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise InputError(f"cannot read {repo_path}: path contains a symlink or junction")
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read {repo_path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InputError(f"cannot read {repo_path}: {path} is not a regular file")
    return True


def _read_source(raw_path: Path, raw_root: Path, repo_path: str, backend: RagkitBackend) -> str | None:
    """读取并规范化一篇源文档，区分"不存在"与"读不出来"。

    Args:
        raw_path: 源文件绝对路径。
        raw_root: 已解析的原始语料根目录，用于越界检查。
        repo_path: 快照内路径，仅用于报错定位。
        backend: RAGKit 后端。

    Returns:
        str | None: 规范化正文；源文件确实不存在时返回 ``None``。

    Raises:
        InputError: 路径越界、不是普通文件、符号链接悬空，或读取/解码失败。

    Notes:
        "文件不存在"与"读不出来"必须严格区分：前者让新快照省略该文件，由
        Go ``check`` 报 ``file_missing``；后者必须让 refresh 以退出码 2 结束，
        否则权限、编码或路径事故会被伪装成文档删除。
    """
    if not _regular_source(raw_path, raw_root, repo_path):
        return None
    try:
        body = backend.normalized_text(raw_path)
    except Exception as exc:  # noqa: BLE001 - 加载/解码失败必须原样暴露
        raise InputError(f"cannot read {repo_path}: {exc}") from exc
    if len(body.encode("utf-8")) > MAX_BLOB_BYTES:
        raise InputError(f"cannot read {repo_path}: normalized document exceeds {MAX_BLOB_BYTES} bytes")
    return body


def prepare_refresh(baseline: Path) -> tuple[bytes, dict, str, list[tuple[str, str]]]:
    """读取冻结 fixture，推导语料前缀与需要重建的文档清单。

    Args:
        baseline: 旧快照目录，只读。

    Returns:
        tuple: ``(fixture 字节, 解析结果, 语料前缀, [(doc, 快照路径)])``。

    Raises:
        InputError: fixture 缺失、超限、结构损坏或包含非法路径。

    Notes:
        ``refresh`` 与 ``verify`` 共用这段读取逻辑，保证两者对"冻结"的理解
        完全一致：fixture 只读、只解析，不重算任何引文。
    """
    fixture_path = baseline / FIXTURE_NAME
    try:
        with fixture_path.open("rb") as stream:
            fixture_bytes = stream.read(MAX_FIXTURE_BYTES + 1)
    except OSError as exc:
        raise InputError(f"cannot read baseline fixture {fixture_path}: {exc}") from exc
    fixture = parse_frozen_fixture(fixture_bytes)
    prefix, entries = split_corpus_paths(fixture_paths(fixture))
    return fixture_bytes, fixture, prefix, entries


def rebuild_corpus(
    fixture_bytes: bytes,
    fixture: dict,
    prefix: str,
    entries: Sequence[tuple[str, str]],
    raw_dir: Path,
    backend: RagkitBackend,
) -> RefreshResult:
    """按重建清单重新规范化语料，不写盘。

    Args:
        fixture_bytes: 冻结 fixture 的原始字节。
        fixture: 解析后的 fixture 文档。
        prefix: 语料目录名，来自冻结 fixture 的路径。
        entries: ``(doc, 快照路径)`` 列表。
        raw_dir: 本次要读取的原始语料根目录。
        backend: 文档加载与归一后端。

    Returns:
        RefreshResult: 新语料、缺失路径与摘要。

    Raises:
        InputError: 某篇源文档读取失败（权限、解码、路径）。

    Notes:
        源文件确实不存在只记入 ``missing_paths``，交给 Go ``check`` 报
        ``file_missing``；读取失败则直接抛错，不允许退化成"删除"。
    """
    raw_root = raw_dir.resolve()
    corpus: dict[str, str] = {}
    missing: list[str] = []
    for doc, repo_path in entries:
        body = _read_source(raw_dir.joinpath(*doc.split("/")), raw_root, repo_path, backend)
        if body is None:
            missing.append(repo_path)
        else:
            corpus[repo_path] = body

    anchors = sum(len(case["evidence"]) for case in fixture["cases"])
    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "version": VERSION,
        "mode": REFRESH_MODE,
        "status": "ok",
        "frozenFixture": {
            "path": FIXTURE_NAME,
            "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "cases": len(fixture["cases"]),
            "anchors": anchors,
        },
        "counts": {
            "cases": len(fixture["cases"]),
            "anchors": anchors,
            "documents": len(entries),
            "rebuilt": len(corpus),
            "missing": len(missing),
        },
        "missingPaths": sorted(missing),
        "outputs": {"fixture": FIXTURE_NAME, "corpus": prefix, "summary": REFRESH_SUMMARY_NAME},
    }
    return RefreshResult(
        fixture_bytes=fixture_bytes,
        corpus=corpus,
        missing_paths=sorted(missing),
        summary=summary,
    )


def run_refresh(options: RefreshOptions, backend: RagkitBackend) -> RefreshResult:
    """重建旧快照引用的规范化语料，fixture 原样复制。

    Args:
        options: 路径与开关。
        backend: 文档加载与归一后端。

    Returns:
        RefreshResult: 冻结 fixture 字节、新语料、缺失路径与摘要。

    Raises:
        InputError: 基线 fixture 不可用、输出与基线重叠，或源文档读取失败。

    Notes:
        不校验 annotation manifest：它记录的是旧语料，文档改动后必然不匹配；
        refresh 的职责正是让旧引文进入比较流程，由 Go ``check`` 报失效。
        旧引文是否还存在不影响刷新结果，源文件重命名也按"旧路径缺失"处理。
    """
    fixture_bytes, fixture, prefix, entries = prepare_refresh(options.baseline)
    _refresh_existing_outputs(options, prefix)
    return rebuild_corpus(fixture_bytes, fixture, prefix, entries, options.raw_dir, backend)


def _refresh_existing_outputs(options: RefreshOptions, corpus_prefix: str) -> list[str]:
    """校验输出目录，返回需要先移开的既有 refresh 快照条目。

    Args:
        options: 刷新选项。
        corpus_prefix: 由旧 fixture 推导出的语料目录名。

    Returns:
        list[str]: 已存在且可以替换的条目名；没有则为空。

    Raises:
        InputError: 输出与基线重叠、条目类型异常，或已有快照但不是本工具的
            refresh 产物、或未加 ``--force``。

    Notes:
        与导入侧的 ``_existing_outputs`` 规则一致：宁可拒绝也不覆盖来路不明
        的目录内容。基线目录永远只读，连重叠都不允许。
    """
    baseline = options.baseline.resolve()
    output = options.output.resolve()
    if output == baseline or output.is_relative_to(baseline) or baseline.is_relative_to(output):
        raise InputError("--output must not overlap --baseline; the old snapshot is read-only")

    existing: list[str] = []
    for name in (FIXTURE_NAME, REFRESH_SUMMARY_NAME, corpus_prefix):
        path = options.output / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        is_link = stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        correct_type = stat.S_ISDIR(info.st_mode) if name == corpus_prefix else stat.S_ISREG(info.st_mode)
        if is_link or not correct_type:
            raise InputError(f"output entry is not a regular snapshot file or directory: {path}")
        existing.append(name)
    if not existing:
        return existing
    if not options.force:
        raise InputError(
            f"output already contains a snapshot ({existing[0]}); pass --force or pick a new directory"
        )
    if len(existing) != 3:
        raise InputError("--force requires a complete prior refresh snapshot; use a new output directory")
    try:
        summary = json.loads((options.output / REFRESH_SUMMARY_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InputError("--force requires a valid prior refresh summary") from exc
    if not isinstance(summary, dict) or summary.get("tool") != TOOL or (
        summary.get("mode") != REFRESH_MODE
        or summary.get("schemaVersion") != SCHEMA_VERSION
        or summary.get("status") != "ok"
        or not isinstance(summary.get("outputs"), dict)
        or summary["outputs"].get("corpus") != corpus_prefix
    ):
        raise InputError("--force requires an existing refresh snapshot with the same corpus prefix")
    return existing


def resolve_refresh_options(args: argparse.Namespace) -> RefreshOptions:
    """把 ``refresh`` 子命令参数解析成绝对路径选项。

    Args:
        args: ``argparse`` 解析结果。

    Returns:
        RefreshOptions: 可直接使用的选项。

    Raises:
        InputError: 根目录/基线不是目录、原始语料缺失、输出重叠或类型非法。
    """
    root = Path(args.ragkit_root).expanduser().resolve()
    if not root.is_dir():
        raise InputError(f"--ragkit-root is not a directory: {root}")
    if root.name == "src" and (root / "ragkit" / "__init__.py").is_file():
        root = root.parent

    baseline = Path(args.baseline).expanduser().resolve()
    if not baseline.is_dir():
        raise InputError(f"--baseline is not a directory: {baseline}")

    raw_dir = Path(args.raw_dir).expanduser()
    raw_dir = (root / raw_dir).resolve() if not raw_dir.is_absolute() else raw_dir.resolve()
    if not raw_dir.is_dir():
        raise InputError(f"raw corpus directory does not exist: {raw_dir}")

    output = Path(args.output).expanduser().resolve()
    if any(part.casefold() == ".git" for part in output.parts):
        raise InputError("--output must not be inside a Git metadata directory")
    if output.exists() and not output.is_dir():
        raise InputError(f"--output is not a directory: {output}")
    for protected in (root, raw_dir):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise InputError("--output must not overlap the RAGKit checkout or its raw corpus")
    return RefreshOptions(
        root=root,
        baseline=baseline,
        output=output,
        raw_dir=raw_dir,
        force=bool(args.force),
    )


def write_refresh_snapshot(options: RefreshOptions, result: RefreshResult) -> None:
    """把刷新结果写入新快照目录。

    Args:
        options: 刷新选项。
        result: 刷新结果。

    Raises:
        InputError: 输出目录已有快照且不满足替换条件。
        OSError: 写入失败。

    Notes:
        fixture 用 ``bytes`` 原样写入，绝不重新序列化——这是"冻结"的核心承诺。
    """
    corpus_prefix = result.summary["outputs"]["corpus"]
    _install_snapshot(
        options.output,
        corpus_prefix,
        lambda: _refresh_existing_outputs(options, corpus_prefix),
        result.corpus,
        {
            FIXTURE_NAME: result.fixture_bytes,
            REFRESH_SUMMARY_NAME: _json_bytes(result.summary),
        },
    )


def print_refresh_report(options: RefreshOptions, result: RefreshResult) -> None:
    """把刷新结果打印到终端。

    Args:
        options: 刷新选项。
        result: 刷新结果。

    Notes:
        缺失源文件是预期结果（交给 ``check`` 报 ``file_missing``），因此写
        stdout 而不是 stderr；真正的失败会在写盘前抛错并以退出码 2 结束。
    """
    counts = result.summary["counts"]
    frozen = result.summary["frozenFixture"]
    print(f"{TOOL} {VERSION} (refresh)")
    print(f"ragkit root:  {options.root}")
    print(f"baseline:     {options.baseline}")
    print(f"fixture:      {FIXTURE_NAME} (frozen, sha256 {frozen['sha256'][:12]}...)")
    print(f"corpus:       {result.summary['outputs']['corpus']}/ "
          f"({counts['rebuilt']} rebuilt, {counts['missing']} missing)")
    for path in result.missing_paths:
        print(f"  missing source: {path} (omitted; check will report file_missing)")
    print(f"summary:      {REFRESH_SUMMARY_NAME}")
    print("status:       ok")


def _first_line(text: str) -> str:
    """取第一行非空文本，用于把子进程错误压成一条消息。

    Args:
        text: 子进程的 stderr。

    Returns:
        str: 第一条非空内容；全空时返回 ``no details``。
    """
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "no details"


def _git_env(repo: Path) -> dict[str, str]:
    """构造隔离的 Git 环境，避免读写调用者的 Git 配置。

    Args:
        repo: 临时仓库目录。

    Returns:
        dict: 合并后的环境变量。

    Notes:
        全局配置指向 ``.git`` 下的不存在文件，加上 ``GIT_CONFIG_NOSYSTEM=1``，
        调用者的 global/system 配置不会影响提交结果，也不会被我们写入。
    """
    return {
        **{name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": str(repo / ".git" / "verify-gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "ragkit-verify",
        "GIT_AUTHOR_EMAIL": "ragkit-verify@example.invalid",
        "GIT_COMMITTER_NAME": "ragkit-verify",
        "GIT_COMMITTER_EMAIL": "ragkit-verify@example.invalid",
    }


def _run_git_bytes(
    repo: Path, args: Sequence[str], env: dict[str, str], data: bytes | None = None,
) -> bytes:
    try:
        done = subprocess.run(
            ["git", *args], cwd=repo, input=data, capture_output=True, env=env,
        )
    except OSError as exc:
        raise InputError(f"cannot run git: {exc}") from exc
    if done.returncode != 0:
        raise InputError(
            f"git {args[0]} failed: {_first_line(done.stderr.decode('utf-8', 'replace'))}"
        )
    return done.stdout


def _commit_snapshot(
    repo: Path, message: str, files: Iterable[tuple[str, bytes]], env: dict[str, str],
    fixture_blob: bytes,
) -> str:
    """Populate the scratch index from raw blobs, bypassing worktree filters."""
    _run_git_bytes(repo, ["read-tree", "--empty"], env)
    entries = [b"100644 " + fixture_blob + b"\t" + FIXTURE_NAME.encode() + b"\0"]
    for path, body in files:
        blob = _run_git_bytes(repo, ["hash-object", "-w", "--stdin"], env, body).strip()
        entries.append(b"100644 " + blob + b"\t" + path.encode() + b"\0")
    _run_git_bytes(repo, ["update-index", "-z", "--index-info"], env, b"".join(entries))
    _run_git_bytes(repo, ["commit", "--allow-empty", "--no-gpg-sign", "-q", "-m", message], env)
    return _run_git_bytes(repo, ["rev-parse", "HEAD"], env).decode("ascii").strip()


def _baseline_files(baseline: Path, entries: Sequence[tuple[str, str]]) -> Iterator[tuple[str, bytes]]:
    for _, repo_path in entries:
        path = baseline.joinpath(*repo_path.split("/"))
        if not _regular_source(path, baseline, repo_path):
            continue
        with path.open("rb") as stream:
            body = stream.read(MAX_BLOB_BYTES + 1)
        if len(body) > MAX_BLOB_BYTES:
            raise InputError(f"cannot read baseline {repo_path}: blob exceeds {MAX_BLOB_BYTES} bytes")
        yield repo_path, body


def _assert_fixture_frozen(repo: Path, base: str, head: str, fixture_bytes: bytes, env: dict[str, str]) -> None:
    """确认两个提交里的 fixture 与冻结字节完全一致。

    Raises:
        InputError: 任一提交的 fixture 与冻结字节不同（内部不变量被破坏）。
    """
    for sha in (base, head):
        committed = _run_git_bytes(repo, ["show", f"{sha}:{FIXTURE_NAME}"], env)
        if committed != fixture_bytes:
            raise InputError(f"fixture bytes differ in commit {sha[:12]}; refusing to report")


def _run_checker(checker: Path, repo: Path, base: str, head: str, env: dict[str, str]) -> tuple[int, dict]:
    """调用 Go 检查器并解析 JSON 报告。

    Args:
        checker: 可执行文件路径。
        repo: 临时仓库目录。
        base: 基线提交 SHA。
        head: PR 提交 SHA。

    Returns:
        tuple: ``(退出码, 报告)``，退出码只会是 0 或 1。

    Raises:
        InputError: 无法执行、退出码异常，或输出不是合法 JSON 报告。
    """
    command = [
        str(checker),
        "check",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--format",
        "json",
    ]
    try:
        done = subprocess.run(
            command,
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise InputError(f"cannot execute checker {checker}: {exc}") from exc
    if done.returncode not in (EXIT_OK, EXIT_REVIEW):
        raise InputError(f"checker exited {done.returncode}: {_first_line(done.stderr)}")
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise InputError(f"checker did not return a JSON report: {exc}") from exc
    if (not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION
            or payload.get("tool") != "rag-fixture-check" or payload.get("base") != base
            or payload.get("head") != head or not isinstance(payload.get("cases"), list)):
        raise InputError("checker report does not match the requested snapshot comparison")
    return done.returncode, payload


def _affected_cases(payload: dict) -> list[dict]:
    """从检查器报告里挑出需要复核的题。

    Args:
        payload: Go 检查器的 JSON 报告。

    Returns:
        list[dict]: 每项包含 ``id``、``question``（可能为空）与 ``statuses``。

    Notes:
        ``intact`` 与 ``relocated`` 视为有效；其余状态都进列表。这里只做筛选，
        不改写任何状态。
    """
    affected: list[dict] = []
    for case in payload["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(
            case.get("evidence"), list
        ):
            raise InputError("checker report contains an invalid case")
        statuses = []
        for item in case["evidence"]:
            status = item.get("status") if isinstance(item, dict) else None
            if status not in ("intact", "relocated", "missing", "ambiguous",
                              "context_changed", "file_missing", "unreadable"):
                raise InputError("checker report contains an invalid evidence status")
            statuses.append(status)
        if any(status not in ("intact", "relocated") for status in statuses):
            affected.append(
                {
                    "id": case.get("id"),
                    "question": case.get("question", ""),
                    "statuses": statuses,
                }
            )
    return affected


@contextlib.contextmanager
def _temporary_repo() -> Iterator[Path]:
    """创建并清理一次性 Git 仓库目录。

    Yields:
        Path: 仓库根目录（已存在、为空）。

    Notes:
        仓库建在系统临时目录，不会碰调用者的工作区；退出时尽力删除。
    """
    root = Path(tempfile.mkdtemp(prefix="ragkit-verify-"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_verify(options: VerifyOptions, backend: RagkitBackend) -> VerifyResult:
    """用可信加载器校验 PR 原始语料对冻结 fixture 的影响。

    Args:
        options: 校验选项。
        backend: 可信 RAGKit 加载器后端。

    Returns:
        VerifyResult: 完整报告、退出码与受影响题。

    Raises:
        InputError: 输入不可用、源文档读取失败、Git 或检查器执行失败。

    Notes:
        流程固定为：读冻结 fixture → 用 ``--raw-dir`` 重建语料 → 在临时仓库
        提交基线快照与 PR 快照 → 断言两份 fixture 字节相同 → 调 Go ``check``。
        整个过程中旧快照与源语料只读，调用者的 Git 工作区完全不参与。
    """
    fixture_bytes, fixture, prefix, entries = prepare_refresh(options.baseline)
    refresh = rebuild_corpus(fixture_bytes, fixture, prefix, entries, options.raw_dir, backend)

    with _temporary_repo() as repo:
        env = _git_env(repo)
        _run_git_bytes(repo, ["init", "-q", "-b", "main", "--template="], env)
        fixture_blob = _run_git_bytes(repo, ["hash-object", "-w", "--stdin"], env, fixture_bytes).strip()
        base = _commit_snapshot(repo, "baseline snapshot", _baseline_files(options.baseline, entries),
                                env, fixture_blob)
        head = _commit_snapshot(repo, "PR snapshot",
                                ((path, body.encode("utf-8")) for path, body in refresh.corpus.items()),
                                env, fixture_blob)
        _assert_fixture_frozen(repo, base, head, fixture_bytes, env)
        checker_code, payload = _run_checker(options.checker, repo, base, head, env)

    affected = _affected_cases(payload)
    expected_cases = [(case["id"], len(case["evidence"])) for case in fixture["cases"]]
    reported_cases = [(case["id"], len(case["evidence"])) for case in payload["cases"]]
    if reported_cases != expected_cases:
        raise InputError("checker report does not cover the frozen fixture")
    if bool(affected) != (checker_code == EXIT_REVIEW):
        raise InputError("checker findings do not match its exit code")
    anchors = sum(len(case["evidence"]) for case in fixture["cases"])
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "version": VERSION,
        "mode": VERIFY_MODE,
        "status": "ok" if checker_code == EXIT_OK else "review",
        "checkerExitCode": checker_code,
        "frozenFixture": {
            "path": FIXTURE_NAME,
            "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "bytesIdenticalAcrossCommits": True,
            "cases": len(fixture["cases"]),
            "anchors": anchors,
        },
        "corpus": {
            "rebuilt": len(refresh.corpus),
            "missing": len(refresh.missing_paths),
            "missingPaths": refresh.missing_paths,
        },
        "commits": {"base": base, "head": head},
        "checker": {"binary": options.checker.name},
        "affected": affected,
        "check": payload,
    }
    return VerifyResult(
        report=report,
        exit_code=EXIT_OK if checker_code == EXIT_OK else EXIT_REVIEW,
        affected=affected,
    )


def resolve_verify_options(args: argparse.Namespace) -> VerifyOptions:
    """把 ``verify`` 子命令参数解析成绝对路径选项。

    Args:
        args: ``argparse`` 解析结果。

    Returns:
        VerifyOptions: 可直接使用的选项。

    Raises:
        InputError: 目录缺失、检查器不是文件、报告路径非法或落在旧快照内。
    """
    root = Path(args.ragkit_root).expanduser().resolve()
    if not root.is_dir():
        raise InputError(f"--ragkit-root is not a directory: {root}")
    if root.name == "src" and (root / "ragkit" / "__init__.py").is_file():
        root = root.parent

    baseline = Path(args.baseline).expanduser().resolve()
    if not baseline.is_dir():
        raise InputError(f"--baseline is not a directory: {baseline}")

    raw_dir = Path(args.raw_dir).expanduser()
    raw_dir = (root / raw_dir).resolve() if not raw_dir.is_absolute() else raw_dir.resolve()
    if not raw_dir.is_dir():
        raise InputError(f"--raw-dir is not a directory: {raw_dir}")

    checker = Path(args.checker).expanduser().resolve()
    if not checker.is_file():
        raise InputError(f"--checker is not a file: {checker}")

    report = Path(args.report).expanduser().resolve()
    if report.exists() and report.is_dir():
        raise InputError(f"--report is a directory: {report}")
    if report.is_relative_to(baseline):
        raise InputError("--report must not be written inside the baseline snapshot")
    if report.is_relative_to(root) or report.is_relative_to(raw_dir) or report == checker:
        raise InputError("--report must be outside the loader checkout, PR corpus and checker file")
    if any(part.casefold() == ".git" for part in report.parts):
        raise InputError("--report must not be written inside Git metadata")
    return VerifyOptions(
        root=root,
        baseline=baseline,
        raw_dir=raw_dir,
        checker=checker,
        report=report,
    )


def write_verify_report(path: Path, report: dict) -> None:
    """写出完整 JSON 报告，父目录按需创建。

    Args:
        path: 报告路径。
        report: 报告内容。

    Raises:
        OSError: 创建目录或写入失败。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".verify-report-", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(_json_bytes(report))
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def print_verify_report(options: VerifyOptions, result: VerifyResult) -> None:
    """把校验结果打印到终端。

    Args:
        options: 校验选项。
        result: 校验结果。

    Notes:
        终端只打印摘要与受影响题号；完整细节在 ``--report`` 里。退出码 1 时
        报告已经写好，打印只是提示。
    """
    frozen = result.report["frozenFixture"]
    corpus = result.report["corpus"]
    print(f"{TOOL} {VERSION} (verify)")
    print(f"baseline:     {options.baseline}")
    print(f"raw dir:      {options.raw_dir}")
    print(f"report:       {options.report}")
    print(f"fixture:      {FIXTURE_NAME} (frozen, sha256 {frozen['sha256'][:12]}...)")
    print(f"corpus:       {corpus['rebuilt']} rebuilt, {corpus['missing']} missing")
    if result.affected:
        print("affected:")
        for case in result.affected:
            print(f"  {case['id']}: {', '.join(case['statuses'])}")
        print(f"status: review needed (exit {EXIT_REVIEW})")
    else:
        print(f"status: ok (exit {EXIT_OK})")


def build_verify_parser() -> argparse.ArgumentParser:
    """构造 ``verify`` 子命令的解析器。

    Returns:
        argparse.ArgumentParser: 解析器实例。

    Notes:
        五个参数都是必需的：可信加载器、旧快照、PR 原始语料、检查器、报告路径。
        ``--raw-dir`` 没有默认值，避免误把本仓库的语料当成 PR 语料。
    """
    parser = argparse.ArgumentParser(
        prog=f"{TOOL} verify",
        description=(
            "Check whether a docs PR invalidates committed evaluation evidence. "
            "The frozen fixture comes from --baseline, the text from --raw-dir, "
            "and the comparison runs in a throwaway Git repository."
        ),
    )
    parser.add_argument("--ragkit-root", required=True, help="trusted RAGKit loader project (read-only)")
    parser.add_argument("--baseline", required=True, help="snapshot holding the frozen fixture (read-only)")
    parser.add_argument("--raw-dir", required=True, help="raw corpus of the PR (absolute or root-relative)")
    parser.add_argument("--checker", required=True, help="rag-fixture-check executable")
    parser.add_argument("--report", required=True, help="path of the complete JSON report")
    parser.add_argument("--version", action="version", version=f"{TOOL} {VERSION}")
    return parser


def _verify_cli(argv: Sequence[str], backend: RagkitBackend | None) -> int:
    """执行 ``verify`` 子命令。

    Args:
        argv: ``verify`` 之后的参数。
        backend: 仅用于测试注入。

    Returns:
        int: 0 全部有效、1 有证据需要复核、2 输入/读取/执行失败。

    Notes:
        退出 1 之前一定先写报告；写报告失败按退出 2 处理，避免"任务失败但
        没有证据"的中间状态。
    """
    args = build_verify_parser().parse_args(argv)
    try:
        options = resolve_verify_options(args)
        if backend is None:
            backend = load_ragkit_backend(options.root)
        result = run_verify(options, backend)
    except (InputError, OSError, UnicodeError, RuntimeError) as exc:
        print(f"{TOOL}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        write_verify_report(options.report, result.report)
    except OSError as exc:
        print(f"{TOOL}: error: cannot write report {options.report}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print_verify_report(options, result)
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        argparse.ArgumentParser: 解析器实例。

    Notes:
        必需参数只有 ``--ragkit-root`` 与 ``--output``；其余路径可覆盖，用于
        布局不同的 RAGKit 分支。``refresh`` 是独立子命令，见
        :func:`build_refresh_parser`；不写子命令时保持 v0.2 的导入行为。
    """
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Export a RAGKit evaluation set as a rag-fixture-check snapshot.",
        epilog=(
            "Subcommands:\n"
            "  refresh   rebuild the normalized corpus for a frozen fixture\n"
            "  verify    check a docs PR against a frozen fixture with a checker\n"
            "            run '" + TOOL + " <command> --help' for their flags"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ragkit-root", required=True, help="RAGKit project root (or its src directory)")
    parser.add_argument("--output", required=True, help="directory that receives the snapshot")
    parser.add_argument("--qrels", default=str(DEFAULT_QRELS), help="qrels path, relative to the root")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="annotation manifest path, relative to the root")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="raw corpus directory, relative to the root")
    parser.add_argument("--corpus-prefix", default=DEFAULT_CORPUS_PREFIX, help="snapshot directory name for corpus files")
    parser.add_argument("--force", action="store_true", help="replace an existing snapshot in the output directory")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def build_refresh_parser() -> argparse.ArgumentParser:
    """构造 ``refresh`` 子命令的解析器。

    Returns:
        argparse.ArgumentParser: 解析器实例。

    Notes:
        必需参数是 ``--ragkit-root``、``--baseline`` 与 ``--output``；没有
        ``--corpus-prefix``，因为前缀由冻结 fixture 决定。
    """
    parser = argparse.ArgumentParser(
        prog=f"{TOOL} refresh",
        description=(
            "Rebuild the normalized corpus for a frozen fixture without changing the fixture. "
            "Quotes that no longer exist are left alone for the checker to report."
        ),
    )
    parser.add_argument("--ragkit-root", required=True, help="RAGKit project root (or its src directory)")
    parser.add_argument("--baseline", required=True, help="previous snapshot directory (read-only)")
    parser.add_argument("--output", required=True, help="new snapshot directory")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="raw corpus directory, relative to the root")
    parser.add_argument("--force", action="store_true", help="replace an existing refresh snapshot in the output directory")
    parser.add_argument("--version", action="version", version=f"{TOOL} {VERSION}")
    return parser


def _import_cli(argv: Sequence[str] | None, backend: RagkitBackend | None) -> int:
    """执行 v0.2 的导入流程。

    Args:
        argv: 导入参数（不含 ``import`` 前缀）。
        backend: 仅用于测试注入。

    Returns:
        int: 0 成功、1 有导入问题、2 输入或环境错误。
    """
    args = build_parser().parse_args(argv)
    try:
        options = resolve_options(args)
        if backend is None:
            backend = load_ragkit_backend(options.root)
        result = run_import(options, backend)
    except (InputError, OSError, UnicodeError, RuntimeError) as exc:
        print(f"{TOOL}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if result.exit_code != EXIT_OK:
        print_report(options, result)
        return EXIT_REVIEW

    try:
        write_snapshot(options, result)
    except (InputError, OSError, UnicodeError) as exc:
        print(f"{TOOL}: error: cannot write output: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print_report(options, result)
    return EXIT_OK


def _refresh_cli(argv: Sequence[str], backend: RagkitBackend | None) -> int:
    """执行 ``refresh`` 子命令。

    Args:
        argv: ``refresh`` 之后的参数。
        backend: 仅用于测试注入。

    Returns:
        int: 0 刷新成功（包含源文件缺失的情况）、2 输入或环境错误。

    Notes:
        refresh 没有"需审阅"这个退出码：旧引文是否失效由 Go ``check`` 判定，
        本命令只负责把新语料准备好。
    """
    args = build_refresh_parser().parse_args(argv)
    try:
        options = resolve_refresh_options(args)
        if backend is None:
            backend = load_ragkit_backend(options.root)
        result = run_refresh(options, backend)
    except (InputError, OSError, UnicodeError, RuntimeError) as exc:
        print(f"{TOOL}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        write_refresh_snapshot(options, result)
    except (InputError, OSError, UnicodeError) as exc:
        print(f"{TOOL}: error: cannot write output: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print_refresh_report(options, result)
    return EXIT_OK


def main(argv: Sequence[str] | None = None, *, backend: RagkitBackend | None = None) -> int:
    """CLI 入口，负责在 ``import``（默认）与 ``refresh`` 之间分派。

    Args:
        argv: 不含程序名的参数列表；``None`` 表示取 ``sys.argv[1:]``。
        backend: 仅用于测试注入；生产调用应省略，由 ``--ragkit-root`` 加载。

    Returns:
        int: 导入与 verify 返回 0/1/2；refresh 返回 0/2。

    Notes:
        不写子命令时按 v0.2 的导入命令处理，``import`` 也可以显式写出。
        ``argparse`` 自身在参数错误时会以 2 退出，与退出码约定一致。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == REFRESH_MODE:
        return _refresh_cli(args[1:], backend)
    if args and args[0] == VERIFY_MODE:
        return _verify_cli(args[1:], backend)
    if args and args[0] == "import":
        args = args[1:]
    return _import_cli(args, backend)


if __name__ == "__main__":
    sys.exit(main())
