#!/usr/bin/env python3
"""RAGKit 评测集导入器：把 RAGKit 的 qrels 转成 rag-fixture-check 的 fixture 快照。

职责边界
--------
- 只读 RAGKit 项目（qrels、annotation manifest、``data/raw`` 语料）；
- 只写 ``--output`` 目录下的 ``.rag-fixtures.json``、``corpus/`` 与 ``import-summary.json``；
- 语料正文一律来自 RAGKit 的 ``load_file`` + ``normalize_text``，本文件不实现
  任何 Markdown 解析、front matter 剥离或空白归一；
- 不修改 RAGKit，不联网，不调用 git，不写 ``--output`` 之外的路径。

为什么需要规范化快照
--------------------
RAGKit 的标注区间是在 ``normalize_text(load_file(path).text)`` 上算出来的。
直接拿原始 Markdown 当语料，front matter、HTML 注释、MDX 行、全角字符与行尾
空白都会让偏移错位，检查器会把大量本来有效的证据误报成 ``missing``。导入器
因此把"规范化后的正文"落成快照，fixture 里的 ``path`` 指向快照内部
（``corpus/...``），而不是原始文档路径。

承诺清单
--------
1. fixture 只包含有效题：无答案题与 ``needs_review`` 题分别跳过并计数；
2. 每条 evidence 的 ``exact`` 必须与标注区间 ``[start, end)`` 逐字节一致，
   否则记录问题并以退出码 1 结束；
3. 引文重复时，依据标注位置生成能唯一定位的前缀/后缀；无法唯一化则记录问题；
4. 有问题时一个输出文件都不写，问题打到 stderr，退出码 1——避免留下"半个
   快照"或让旧快照与失败摘要混在一起；
5. 输出里不出现本机绝对路径，便于把快照提交进仓库。

退出码
------
0 导入成功；1 有无法定位/校验失败的证据；2 环境或输入不可用（RAGKit 导入失败、
qrels 摘要不匹配、输出目录冲突等）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import shutil
import stat
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

TOOL = "ragkit-fixture-import"
VERSION = "0.2.0"
SCHEMA_VERSION = 1

DEFAULT_QRELS = Path("data/eval/qrels.json")
DEFAULT_MANIFEST = Path("data/eval/annotation_manifest.json")
DEFAULT_RAW_DIR = Path("data/raw")
DEFAULT_CORPUS_PREFIX = "corpus"
FIXTURE_NAME = ".rag-fixtures.json"
SUMMARY_NAME = "import-summary.json"

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
        if len(case["question"].encode("utf-8")) > MAX_QUESTION_BYTES:
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
    if corpus_prefix.casefold() in {FIXTURE_NAME, SUMMARY_NAME, LOCK_NAME}:
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


def write_snapshot(options: ImportOptions, result: ImportResult) -> None:
    """Stage all artifacts first and keep the old snapshot until replacement succeeds."""
    output = options.output
    output.mkdir(parents=True, exist_ok=True)
    lock = output / LOCK_NAME
    lock.mkdir()
    stage = None
    keep_backup = False
    try:
        existing = _existing_outputs(options)
        stage = Path(tempfile.mkdtemp(prefix=".ragkit-stage-", dir=output))
        incoming = stage / "incoming"
        backup = stage / "previous"
        (incoming / options.corpus_prefix).mkdir(parents=True)
        backup.mkdir()
        for repo_path, content in sorted(result.corpus.items()):
            target = incoming.joinpath(*repo_path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content.encode("utf-8"))
        (incoming / FIXTURE_NAME).write_bytes(_json_bytes(result.fixture))
        (incoming / SUMMARY_NAME).write_bytes(_json_bytes(result.summary))
        moved, installed = [], []
        try:
            for name in existing:
                (output / name).rename(backup / name)
                moved.append(name)
            for name in (options.corpus_prefix, FIXTURE_NAME, SUMMARY_NAME):
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


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    Returns:
        argparse.ArgumentParser: 解析器实例。

    Notes:
        必需参数只有 ``--ragkit-root`` 与 ``--output``；其余路径可覆盖，用于
        布局不同的 RAGKit 分支。
    """
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Export a RAGKit evaluation set as a rag-fixture-check snapshot.",
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


def main(argv: Sequence[str] | None = None, *, backend: RagkitBackend | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 不含程序名的参数列表；``None`` 表示取 ``sys.argv[1:]``。
        backend: 仅用于测试注入；生产调用应省略，由 ``--ragkit-root`` 加载。

    Returns:
        int: 0 成功、1 有导入问题、2 输入或环境错误。

    Notes:
        ``argparse`` 自身在参数错误时会以 2 退出，与本文档的退出码约定一致。
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


if __name__ == "__main__":
    sys.exit(main())
