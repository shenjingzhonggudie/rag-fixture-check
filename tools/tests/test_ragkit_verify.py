"""verify 子命令的合成数据测试。

覆盖：文档不变 → 0；改写或删除源文件 → 1 且报告正确题号；源文件读不出来 →
2 且不写报告；输出与输入保护；baseline 只读；以及"两份快照相同"仍能跑通。

端到端用例需要 ``rag-fixture-check`` 与 ``git``：
``RAG_FIXTURE_CHECK_BIN`` → PATH 上的 ``rag-fixture-check`` → ``go build``。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import ragkit_import as imp  # noqa: E402


class FakeBackend:
    """只做 CRLF 归一的最小后端。"""

    def normalized_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class FailingBackend:
    """模拟权限/解码失败：文件存在但读不出来。"""

    def normalized_text(self, path: Path) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


BODIES = {
    "docs/refund.md": "# Refund\n\nRefunds within 30 days.\n",
    "docs/shipping.md": "# Shipping\n\nOrders ship within 2 days.\n",
    "docs/faq.md": "# FAQ\n\nWe ship worldwide.\n",
}


def write_text(path: Path, text: str) -> None:
    """写文本文件，父目录按需创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: object) -> None:
    """写 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def anchor(doc: str, body: str, quote: str) -> dict:
    """构造一条与正文区间一致的证据。"""
    start = body.index(quote)
    return {"doc": doc, "section": "synthetic", "start": start, "end": start + len(quote), "quote": quote}


def case(case_id: str, question: str, *evidence: dict) -> dict:
    """构造一条 qrels 记录。"""
    return {
        "id": case_id,
        "query": question,
        "type": "direct",
        "source": "internal",
        "annotation_version": 2,
        "annotation_status": "complete",
        "answers": [{"point": "synthetic point", "evidence": list(evidence)}],
    }


def build_project(tmp: Path) -> Path:
    """在临时目录里搭一个最小 RAGKit 布局。"""
    root = tmp / "ragkit-project"
    (root / "data" / "eval").mkdir(parents=True, exist_ok=True)
    for doc, text in BODIES.items():
        write_text(root / "data" / "raw" / doc, text)
    items = [
        case("refund-30", "How many days?", anchor("docs/refund.md", BODIES["docs/refund.md"], "Refunds within 30 days.")),
        case("shipping", "How fast?", anchor("docs/shipping.md", BODIES["docs/shipping.md"], "Orders ship within 2 days.")),
        case("faq", "Do you ship?", anchor("docs/faq.md", BODIES["docs/faq.md"], "We ship worldwide.")),
    ]
    write_json(root / "data" / "eval" / "qrels.json", items)
    return root


def run_cli(args: list[str], backend=None) -> tuple[int, str, str]:
    """在重定向的 stdout/stderr 下调用 CLI。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = imp.main(args, backend=backend)
    return code, stdout.getvalue(), stderr.getvalue()


_CHECKER: list[Path | None] = []


def find_checker() -> Path | None:
    """定位或构建 ``rag-fixture-check``；找不到时返回 None。"""
    if _CHECKER:
        return _CHECKER[0]
    candidate = os.environ.get("RAG_FIXTURE_CHECK_BIN")
    path = Path(candidate) if candidate else None
    if path is None or not path.is_file():
        found = shutil.which("rag-fixture-check")
        path = Path(found) if found else None
    if path is None:
        go = shutil.which("go")
        if go:
            binary = Path(tempfile.mkdtemp(prefix="rfc-bin-")) / (
                "rag-fixture-check.exe" if os.name == "nt" else "rag-fixture-check"
            )
            built = subprocess.run(
                [go, "build", "-o", str(binary), "."],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            if built.returncode == 0 and binary.is_file():
                path = binary
    _CHECKER.append(path)
    return path


def snapshot_digest(root: Path) -> str:
    """把快照目录的内容摘要成一条字符串，用于断言"没被改写"。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


class VerifyTests(unittest.TestCase):
    """verify 的退出码、报告内容与只读约束。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.checker = find_checker()
        if self.checker is None:
            self.skipTest("rag-fixture-check binary not available (set RAG_FIXTURE_CHECK_BIN or install Go)")
        if shutil.which("git") is None:
            self.skipTest("git is not available in PATH")

        self.root = build_project(self.tmp)
        self.baseline = self.tmp / "snapshot-v1"
        code, _, stderr = run_cli(["--ragkit-root", str(self.root), "--output", str(self.baseline)], backend=FakeBackend())
        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.report = self.tmp / "verify-report.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def verify(self, *, raw_dir: Path | None = None, report: Path | None = None, backend=None):
        """跑一次 verify。"""
        return run_cli(
            [
                "verify",
                "--ragkit-root",
                str(self.root),
                "--baseline",
                str(self.baseline),
                "--raw-dir",
                str(raw_dir or self.root / "data" / "raw"),
                "--checker",
                str(self.checker),
                "--report",
                str(report or self.report),
            ],
            backend=backend or FakeBackend(),
        )

    def load_report(self) -> dict:
        """读取本次报告。"""
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_unchanged_documents_pass(self) -> None:
        code, stdout, stderr = self.verify()

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertIn("status: ok", stdout)
        payload = self.load_report()
        self.assertEqual(payload["mode"], "verify")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checkerExitCode"], 0)
        self.assertEqual(payload["affected"], [])
        self.assertEqual(payload["corpus"]["rebuilt"], 3)
        self.assertEqual(payload["corpus"]["missing"], 0)
        self.assertTrue(payload["frozenFixture"]["bytesIdenticalAcrossCommits"])
        self.assertEqual(payload["frozenFixture"]["cases"], 3)
        self.assertEqual(payload["check"]["summary"]["intact"], 3)
        self.assertEqual(payload["check"]["summary"]["cases"], 3)
        # 两份快照相同也要有两个提交。
        self.assertNotEqual(payload["commits"]["base"], payload["commits"]["head"])

    def test_rewritten_markdown_is_reported(self) -> None:
        write_text(self.root / "data" / "raw" / "docs" / "refund.md", "# Refund\n\nRefunds within 14 days.\n")

        code, stdout, stderr = self.verify()

        self.assertEqual(code, imp.EXIT_REVIEW, stderr)
        self.assertIn("refund-30: missing", stdout)
        payload = self.load_report()
        self.assertEqual(payload["status"], "review")
        self.assertEqual(payload["checkerExitCode"], 1)
        self.assertEqual(
            payload["affected"],
            [{"id": "refund-30", "question": "How many days?", "statuses": ["missing"]}],
        )
        self.assertEqual(payload["check"]["summary"]["missing"], 1)
        self.assertEqual(payload["check"]["summary"]["intact"], 2)
        self.assertEqual(payload["corpus"]["rebuilt"], 3)

    def test_deleted_source_is_file_missing(self) -> None:
        (self.root / "data" / "raw" / "docs" / "shipping.md").unlink()

        code, stdout, stderr = self.verify()

        self.assertEqual(code, imp.EXIT_REVIEW, stderr)
        self.assertIn("shipping: file_missing", stdout)
        payload = self.load_report()
        self.assertEqual(payload["corpus"]["missing"], 1)
        self.assertEqual(payload["corpus"]["missingPaths"], ["corpus/docs/shipping.md"])
        self.assertEqual(payload["affected"][0]["id"], "shipping")
        self.assertEqual(payload["affected"][0]["statuses"], ["file_missing"])

    def test_unreadable_source_is_exit_two(self) -> None:
        code, _, stderr = self.verify(backend=FailingBackend())

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("cannot read corpus/docs/", stderr)
        self.assertFalse(self.report.exists())

    def test_missing_checker_is_exit_two(self) -> None:
        code, _, stderr = run_cli(
            [
                "verify",
                "--ragkit-root",
                str(self.root),
                "--baseline",
                str(self.baseline),
                "--raw-dir",
                str(self.root / "data" / "raw"),
                "--checker",
                str(self.tmp / "no-such-checker"),
                "--report",
                str(self.report),
            ],
            backend=FakeBackend(),
        )

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("--checker is not a file", stderr)

    def test_missing_raw_dir_is_exit_two(self) -> None:
        code, _, stderr = self.verify(raw_dir=self.tmp / "no-such-dir")

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("--raw-dir is not a directory", stderr)

    def test_report_inside_baseline_is_refused(self) -> None:
        inside = self.baseline / "report.json"

        code, _, stderr = self.verify(report=inside)

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("must not be written inside the baseline", stderr)
        self.assertFalse(inside.exists())

    def test_report_cannot_overwrite_inputs(self) -> None:
        raw_file = self.root / "data" / "raw" / "docs" / "refund.md"
        before = raw_file.read_bytes()
        for destination in (raw_file, self.root / "report.json", self.checker,
                            self.tmp / ".git" / "config"):
            with self.subTest(destination=destination):
                code, _, stderr = self.verify(report=destination)
                self.assertEqual(code, imp.EXIT_USAGE, stderr)
                self.assertIn("--report must", stderr)
        self.assertEqual(raw_file.read_bytes(), before)

    def test_ignores_caller_git_routing_and_configuration(self) -> None:
        caller = self.tmp / "caller"
        caller.mkdir()
        with patch.dict(os.environ, {
            "GIT_DIR": str(caller / ".git"),
            "GIT_WORK_TREE": str(caller),
            "GIT_INDEX_FILE": str(caller / "caller-index"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.autocrlf",
            "GIT_CONFIG_VALUE_0": "true",
        }):
            code, _, stderr = self.verify()
        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertEqual(list(caller.iterdir()), [])
        self.assertEqual(self.load_report()["check"]["summary"]["intact"], 3)

    def test_failed_report_replacement_preserves_previous_report(self) -> None:
        self.report.write_bytes(b"previous report")
        with patch.object(Path, "replace", side_effect=PermissionError("blocked")):
            with self.assertRaises(PermissionError):
                imp.write_verify_report(self.report, {"status": "review"})
        self.assertEqual(self.report.read_bytes(), b"previous report")
        self.assertEqual(list(self.report.parent.glob(".verify-report-*")), [])

    def test_baseline_is_not_modified(self) -> None:
        before = snapshot_digest(self.baseline)
        write_text(self.root / "data" / "raw" / "docs" / "refund.md", "# Refund\n\nRefunds within 14 days.\n")

        code, _, stderr = self.verify()

        self.assertEqual(code, imp.EXIT_REVIEW, stderr)
        self.assertEqual(snapshot_digest(self.baseline), before)

    def test_report_is_written_before_failing(self) -> None:
        write_text(self.root / "data" / "raw" / "docs" / "faq.md", "# FAQ\n\nWe do not ship.\n")

        code, _, _ = self.verify()

        self.assertEqual(code, imp.EXIT_REVIEW)
        self.assertTrue(self.report.is_file())
        self.assertEqual(self.load_report()["affected"][0]["id"], "faq")


if __name__ == "__main__":
    unittest.main()
