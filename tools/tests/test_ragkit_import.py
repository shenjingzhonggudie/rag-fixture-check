"""RAGKit 导入器的合成数据测试。

只使用临时目录里生成的合成语料与题集，不读取任何真实 RAGKit 数据，也不依赖
本机是否安装了 RAGKit：需要真实 ``load_file``/``normalize_text`` 的那条测试会
在临时目录里伪造一个同签名的 ``ragkit`` 包。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import ragkit_import as imp  # noqa: E402


class FakeBackend:
    """只做 CRLF 归一的最小后端，供转换与 CLI 测试使用。"""

    def normalized_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def write_text(path: Path, text: str) -> None:
    """写文本文件，父目录按需创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: object) -> None:
    """写 JSON 文件，格式与导入器输出保持一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def sha256_hex(data: bytes) -> str:
    """返回字节串的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def anchor(
    doc: str,
    body: str,
    quote: str,
    occurrence: int = 0,
    *,
    start: int | None = None,
    end: int | None = None,
) -> dict:
    """构造一条与正文区间一致的证据。"""
    offsets = imp.occurrences(body, quote)
    chosen = offsets[occurrence]
    return {
        "doc": doc,
        "section": "synthetic",
        "start": chosen if start is None else start,
        "end": chosen + len(quote) if end is None else end,
        "quote": quote,
    }


def case(case_id: str, question: str, *evidence: dict, status: str = "complete", answers=None) -> dict:
    """构造一条 qrels 记录。"""
    if answers is None:
        answers = [{"point": "synthetic point", "evidence": list(evidence)}] if evidence else []
    return {
        "id": case_id,
        "query": question,
        "type": "direct",
        "source": "internal",
        "annotation_version": 2,
        "annotation_status": status,
        "answers": answers,
    }


def build_project(
    tmp: Path,
    docs: dict[str, str],
    items: list[dict],
    *,
    manifest: dict | None = None,
) -> Path:
    """在临时目录里搭一个最小 RAGKit 布局。"""
    root = tmp / "ragkit-project"
    (root / "data" / "eval").mkdir(parents=True, exist_ok=True)
    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    for doc, text in docs.items():
        write_text(root / "data" / "raw" / doc, text)
    write_json(root / "data" / "eval" / "qrels.json", items)
    if manifest is not None:
        write_json(root / "data" / "eval" / "annotation_manifest.json", manifest)
    return root


def options_for(root: Path, output: Path, *, force: bool = False) -> imp.ImportOptions:
    """构造导入选项。"""
    return imp.ImportOptions(
        root=root,
        output=output,
        qrels=root / "data" / "eval" / "qrels.json",
        manifest=root / "data" / "eval" / "annotation_manifest.json",
        raw_dir=root / "data" / "raw",
        force=force,
    )


def run_main(args: list[str], backend=None) -> tuple[int, str, str]:
    """在重定向的 stdout/stderr 下调用 main。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = imp.main(args, backend=backend)
    return code, stdout.getvalue(), stderr.getvalue()


class OccurrenceTests(unittest.TestCase):
    """引文计数与上下文匹配的基础语义。"""

    def test_occurrences_counts_overlaps(self) -> None:
        self.assertEqual(imp.occurrences("aaa", "aa"), [0, 1])
        self.assertEqual(imp.occurrences("abc", "z"), [])
        self.assertEqual(imp.occurrences("abc", "abc"), [0])

    def test_context_requires_adjacency(self) -> None:
        body = "## A\n\nquote.\n\n## B\n\nquote."
        second = body.rindex("quote.")
        self.assertTrue(imp.context_matches(body, second, "quote.", "## B\n\n", ""))
        self.assertFalse(imp.context_matches(body, 0, "quote.", "## B\n\n", ""))

    def test_selector_is_empty_for_unique_quote(self) -> None:
        body = "# T\n\nOnly once.\n"
        start = body.index("Only once.")
        self.assertEqual(imp.build_selector(body, start, start + len("Only once."), "Only once."), ("", ""))

    def test_selector_raises_when_context_is_identical(self) -> None:
        block = "x" * 300 + "TARGET" + "y" * 300 + "\n"
        body = block + block
        with self.assertRaises(imp.SelectorError):
            imp.build_selector(body, 300, 306, "TARGET")


class PathValidationTests(unittest.TestCase):
    """仓库相对路径规则与 Go 侧保持一致。"""

    def test_accepts_repo_relative_paths(self) -> None:
        for path in ("docs/refund.md", "corpus/退款说明.md", "a/b c.md"):
            self.assertEqual(imp.validate_repo_path(path), path)

    def test_rejects_unsafe_paths(self) -> None:
        cases = {
            "": "empty",
            "/abs.md": "repo-relative",
            "a\\b.md": "forward slashes",
            "C:/a.md": "':'",
            "a//b.md": "empty segment",
            "a/./b.md": "'.' segment",
            "../a.md": "'..' segment",
            "bad\x01name": "control",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as raised:
                    imp.validate_repo_path(path)
                self.assertIn(expected, str(raised.exception))


class ImportTests(unittest.TestCase):
    """正常导入、重复引文消歧、区间不一致与无法定位。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_imports_valid_cases_and_counts_skips(self) -> None:
        refund = "# Refund\n\nRefunds within 30 days.\n"
        faq = "# FAQ\n\nDo you ship internationally? No.\n"
        items = [
            case("q1", "How many days?", anchor("docs/refund.md", refund, "Refunds within 30 days.")),
            case("q2", "Do you ship?", anchor("docs/faq.md", faq, "Do you ship internationally? No.")),
            case("q3", "Unanswerable?", answers=[]),
            case("q4", "Needs review?", anchor("docs/refund.md", refund, "Refunds within 30 days."), status="needs_review"),
            case("q5", "No evidence?", answers=[{"point": "p", "evidence": []}]),
        ]
        root = build_project(self.tmp, {"docs/refund.md": refund, "docs/faq.md": faq}, items)

        result = imp.run_import(options_for(root, self.tmp / "out"), FakeBackend())

        self.assertEqual(result.problems, [])
        self.assertEqual(result.exit_code, imp.EXIT_OK)
        counts = result.summary["counts"]
        self.assertEqual(counts["queries"], 5)
        self.assertEqual(counts["cases"], 2)
        self.assertEqual(counts["anchors"], 2)
        self.assertEqual(counts["skippedNoAnswer"], 1)
        self.assertEqual(counts["skippedNeedsReview"], 1)
        self.assertEqual(counts["skippedWithoutEvidence"], 1)
        self.assertEqual(counts["documents"], 2)
        self.assertEqual(counts["disambiguatedAnchors"], 0)

        self.assertEqual([item["id"] for item in result.fixture["cases"]], ["q1", "q2"])
        first = result.fixture["cases"][0]
        self.assertEqual(first["question"], "How many days?")
        self.assertEqual(
            first["evidence"],
            [
                {
                    "path": "corpus/docs/refund.md",
                    "exact": "Refunds within 30 days.",
                    "prefix": "",
                    "suffix": "",
                }
            ],
        )
        self.assertEqual(
            sorted(result.corpus),
            ["corpus/docs/faq.md", "corpus/docs/refund.md"],
        )
        self.assertEqual(result.summary["status"], "ok")
        self.assertFalse(result.summary["inputs"]["manifestPresent"])

    def test_duplicate_quote_uses_annotated_position(self) -> None:
        body = "# Dup\n\nSame sentence.\n\nSame sentence.\n"
        items = [case("q1", "Which one?", anchor("docs/dup.md", body, "Same sentence.", occurrence=1))]
        root = build_project(self.tmp, {"docs/dup.md": body}, items)

        result = imp.run_import(options_for(root, self.tmp / "out"), FakeBackend())

        self.assertEqual(result.problems, [])
        evidence = result.fixture["cases"][0]["evidence"][0]
        self.assertTrue(evidence["prefix"] or evidence["suffix"])
        matches = [
            offset
            for offset in imp.occurrences(body, evidence["exact"])
            if imp.context_matches(body, offset, evidence["exact"], evidence["prefix"], evidence["suffix"])
        ]
        self.assertEqual(matches, [body.rindex("Same sentence.")])
        self.assertEqual(result.summary["counts"]["disambiguatedAnchors"], 1)

    def test_span_mismatch_reports_problem_and_writes_nothing(self) -> None:
        body = "# Refund\n\nRefunds within 30 days.\n"
        bad = anchor("docs/refund.md", body, "Refunds within 30 days.", start=0, end=5)
        root = build_project(self.tmp, {"docs/refund.md": body}, [case("q1", "How many days?", bad)])
        output = self.tmp / "out"

        code, _, stderr = run_main(
            ["--ragkit-root", str(root), "--output", str(output)],
            backend=FakeBackend(),
        )

        self.assertEqual(code, imp.EXIT_REVIEW)
        self.assertIn("q1", stderr)
        self.assertIn("corpus/docs/refund.md", stderr)
        self.assertIn("does not match the annotated span", stderr)
        self.assertFalse((output / imp.FIXTURE_NAME).exists())
        self.assertFalse((output / "corpus").exists())
        self.assertFalse((output / imp.SUMMARY_NAME).exists())

    def test_unresolvable_quote_reports_problem(self) -> None:
        block = "x" * 300 + "TARGET" + "y" * 300 + "\n"
        body = block + block
        items = [case("q1", "Which target?", anchor("docs/dup.md", body, "TARGET"))]
        root = build_project(self.tmp, {"docs/dup.md": body}, items)

        code, _, stderr = run_main(
            ["--ragkit-root", str(root), "--output", str(self.tmp / "out")],
            backend=FakeBackend(),
        )

        self.assertEqual(code, imp.EXIT_REVIEW)
        self.assertIn("cannot be narrowed", stderr)
        self.assertIn("q1", stderr)

    def test_rejects_doc_path_that_escapes_the_repository(self) -> None:
        items = [case("q1", "Escape?", {"doc": "../../etc/passwd", "start": 0, "end": 4, "quote": "root"})]
        root = build_project(self.tmp, {"docs/a.md": "alpha\n"}, items)

        result = imp.run_import(options_for(root, self.tmp / "out"), FakeBackend())

        self.assertEqual(result.exit_code, imp.EXIT_REVIEW)
        self.assertEqual(len(result.problems), 1)
        self.assertIn("invalid evidence path", result.problems[0].reason)

    def test_writes_snapshot_and_summary(self) -> None:
        body = "# Refund\n\nRefunds within 30 days.\n"
        items = [case("q1", "How many days?", anchor("docs/refund.md", body, "Refunds within 30 days."))]
        root = build_project(self.tmp, {"docs/refund.md": body}, items)
        output = self.tmp / "out"

        code, stdout, stderr = run_main(
            ["--ragkit-root", str(root), "--output", str(output)],
            backend=FakeBackend(),
        )

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertRegex(stdout, r"status:\s+ok")
        self.assertTrue((output / imp.FIXTURE_NAME).is_file())
        self.assertTrue((output / imp.SUMMARY_NAME).is_file())
        self.assertEqual(
            (output / "corpus" / "docs" / "refund.md").read_text(encoding="utf-8"),
            body,
        )
        fixture = json.loads((output / imp.FIXTURE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(fixture["version"], 1)
        summary = json.loads((output / imp.SUMMARY_NAME).read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["counts"]["anchors"], 1)
        self.assertNotIn(str(self.tmp), json.dumps(summary, ensure_ascii=False))

    def test_output_is_deterministic(self) -> None:
        body = "# Refund\n\nRefunds within 30 days.\n"
        items = [case("q1", "How many days?", anchor("docs/refund.md", body, "Refunds within 30 days."))]
        root = build_project(self.tmp, {"docs/refund.md": body}, items)

        first = self.tmp / "first"
        second = self.tmp / "second"
        self.assertEqual(run_main(["--ragkit-root", str(root), "--output", str(first)], backend=FakeBackend())[0], 0)
        self.assertEqual(run_main(["--ragkit-root", str(root), "--output", str(second)], backend=FakeBackend())[0], 0)

        for name in (imp.FIXTURE_NAME, imp.SUMMARY_NAME):
            self.assertEqual(
                (first / name).read_bytes(),
                (second / name).read_bytes(),
                f"{name} is not deterministic",
            )


class VerificationTests(unittest.TestCase):
    """写出前的产物自检：每个锚点必须唯一可定位。"""

    def test_accepts_resolvable_fixture(self) -> None:
        corpus = {"corpus/a.md": "# A\n\nOnly once.\n"}
        fixture = {
            "version": 1,
            "cases": [
                {
                    "id": "q1",
                    "question": "",
                    "evidence": [
                        {"path": "corpus/a.md", "exact": "Only once.", "prefix": "", "suffix": ""}
                    ],
                }
            ],
        }
        self.assertEqual(imp.verify_fixture(fixture, corpus), [])

    def test_reports_unanchored_anchor(self) -> None:
        corpus = {"corpus/a.md": "# A\n\nSome text.\n"}
        fixture = {
            "version": 1,
            "cases": [
                {
                    "id": "q1",
                    "question": "",
                    "evidence": [{"path": "corpus/a.md", "exact": "Missing text.", "prefix": "", "suffix": ""}],
                }
            ],
        }
        problems = imp.verify_fixture(fixture, corpus)

        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].case_id, "q1")
        self.assertIn("resolves to 0", problems[0].reason)

    def test_reports_ambiguous_anchor(self) -> None:
        corpus = {"corpus/a.md": "Same.\n\nSame.\n"}
        fixture = {
            "version": 1,
            "cases": [
                {
                    "id": "q1",
                    "question": "",
                    "evidence": [{"path": "corpus/a.md", "exact": "Same.", "prefix": "", "suffix": ""}],
                }
            ],
        }
        problems = imp.verify_fixture(fixture, corpus)

        self.assertEqual(len(problems), 1)
        self.assertIn("resolves to 2", problems[0].reason)


class ManifestTests(unittest.TestCase):
    """annotation manifest 的摘要校验。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, corpus_hash: str | None = None) -> Path:
        body = "# Refund\n\nRefunds within 30 days.\n"
        items = [case("q1", "How many days?", anchor("docs/refund.md", body, "Refunds within 30 days."))]
        root = build_project(self.tmp, {"docs/refund.md": body}, items)
        qrels_bytes = (root / "data" / "eval" / "qrels.json").read_bytes()
        manifest = {
            "qrels_sha256": sha256_hex(qrels_bytes),
            "corpus_sha256": {"docs/refund.md": corpus_hash if corpus_hash is not None else sha256_hex(body.encode("utf-8"))},
            "source_commit": "deadbeef" * 5,
        }
        write_json(root / "data" / "eval" / "annotation_manifest.json", manifest)
        return root

    def test_verifies_qrels_and_corpus_hashes(self) -> None:
        root = self._project()
        result = imp.run_import(options_for(root, self.tmp / "out"), FakeBackend())

        self.assertEqual(result.problems, [])
        self.assertTrue(result.summary["inputs"]["manifestVerified"])
        self.assertEqual(result.summary["inputs"]["annotationSourceCommit"], "deadbeef" * 5)

    def test_reports_corpus_hash_mismatch(self) -> None:
        root = self._project(corpus_hash="0" * 64)
        result = imp.run_import(options_for(root, self.tmp / "out"), FakeBackend())

        self.assertEqual(result.exit_code, imp.EXIT_REVIEW)
        self.assertIn("manifest hash", result.problems[0].reason)
        self.assertIn("corpus/docs/refund.md", result.problems[0].doc)

    def test_qrels_hash_mismatch_is_a_usage_error(self) -> None:
        root = self._project()
        manifest_path = root / "data" / "eval" / "annotation_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["qrels_sha256"] = "0" * 64
        write_json(manifest_path, manifest)

        code, _, stderr = run_main(
            ["--ragkit-root", str(root), "--output", str(self.tmp / "out")],
            backend=FakeBackend(),
        )

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("does not match its annotation manifest", stderr)

    def test_uncovered_document_is_reported(self) -> None:
        root = self._project()
        manifest_path = root / "data" / "eval" / "annotation_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["corpus_sha256"] = {"docs/other.md": "0" * 64}
        write_json(manifest_path, manifest)

        result = imp.run_import(options_for(root, self.tmp / "out"), FakeBackend())

        self.assertEqual(result.exit_code, imp.EXIT_REVIEW)
        self.assertIn("not covered by the annotation manifest", result.problems[0].reason)


class OutputTests(unittest.TestCase):
    """输出目录冲突与 --force 覆盖。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_requires_force_to_replace_an_existing_snapshot(self) -> None:
        body = "# Refund\n\nRefunds within 30 days.\n"
        items = [case("q1", "How many days?", anchor("docs/refund.md", body, "Refunds within 30 days."))]
        root = build_project(self.tmp, {"docs/refund.md": body}, items)
        output = self.tmp / "out"
        args = ["--ragkit-root", str(root), "--output", str(output)]

        self.assertEqual(run_main(args, backend=FakeBackend())[0], imp.EXIT_OK)
        code, _, stderr = run_main(args, backend=FakeBackend())
        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("--force", stderr)

        stray = output / "corpus" / "docs" / "stale.md"
        write_text(stray, "stale\n")
        self.assertEqual(run_main(args + ["--force"], backend=FakeBackend())[0], imp.EXIT_OK)
        self.assertFalse(stray.exists())

    def test_force_refuses_reserved_or_unowned_output(self) -> None:
        root = build_project(self.tmp, {}, [])
        output = self.tmp / "out"
        for prefix in (".git", "existing", ".rag-fixtures.json"):
            sentinel = output / prefix / "important"
            write_text(sentinel, "preserve")
            args = ["--ragkit-root", str(root), "--output", str(output),
                    "--force", "--corpus-prefix", prefix]
            with self.subTest(prefix=prefix):
                self.assertEqual(run_main(args, backend=FakeBackend())[0], imp.EXIT_USAGE)
                self.assertEqual(sentinel.read_text(), "preserve")
        args = ["--ragkit-root", str(root), "--output", str(root / "data"), "--force"]
        self.assertEqual(run_main(args, backend=FakeBackend())[0], imp.EXIT_USAGE)

    def test_io_failure_keeps_previous_snapshot(self) -> None:
        body = "Evidence."
        root = build_project(self.tmp, {"a.md": body}, [case("q", "Original?", anchor("a.md", body, body))])
        output = self.tmp / "out"
        args = ["--ragkit-root", str(root), "--output", str(output)]
        self.assertEqual(run_main(args, backend=FakeBackend())[0], imp.EXIT_OK)
        before = {path.relative_to(output): path.read_bytes()
                  for path in output.rglob("*") if path.is_file()}
        write_json(root / "data/eval/qrels.json", [case("q", "Changed?", anchor("a.md", body, body))])
        rename = Path.rename

        def fail_on_install(path, target):
            if path.parent.name == "incoming" and path.name == imp.SUMMARY_NAME:
                raise PermissionError("simulated rename failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_on_install):
            self.assertEqual(run_main(args + ["--force"], backend=FakeBackend())[0], imp.EXIT_USAGE)
        after = {path.relative_to(output): path.read_bytes()
                 for path in output.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_invalid_answers_or_oversized_question_writes_nothing(self) -> None:
        body = "Evidence."
        output = self.tmp / "out"
        for item in (case("q", "Question?", answers=123),
                     case("q", "Question?", answers=[{"evidence": 123}]),
                     case("q", "Q" * 4097, anchor("a.md", body, body))):
            with self.subTest(item=item["answers"]):
                root = build_project(self.tmp, {"a.md": body}, [item])
                args = ["--ragkit-root", str(root), "--output", str(output)]
                self.assertEqual(run_main(args, backend=FakeBackend())[0], imp.EXIT_REVIEW)
                self.assertFalse(output.exists())


class RagkitBridgeTests(unittest.TestCase):
    """通过 --ragkit-root 使用 RAGKit 真实的 load_file/normalize_text 接口。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install_fake_ragkit(self, root: Path) -> None:
        """在 root/src 下写一个同签名的假 ragkit 包。"""
        package = root / "src" / "ragkit"
        write_text(package / "__init__.py", '"""fake ragkit for tests"""\n')
        write_text(package / "loaders" / "__init__.py", "from .files import load_file\n")
        write_text(
            package / "loaders" / "files.py",
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass\n"
            "class Document:\n"
            "    text: str\n"
            "\n"
            "def load_file(path, *, source=None):\n"
            "    return Document(path.read_text(encoding='utf-8'))\n",
        )
        write_text(package / "text" / "__init__.py", "")
        write_text(
            package / "text" / "normalize.py",
            "import re\n"
            "\n"
            "def normalize_text(text):\n"
            "    text = text.replace('\\r\\n', '\\n')\n"
            "    text = '\\n'.join(line.rstrip() for line in text.split('\\n'))\n"
            "    return re.sub(r'[ ]{2,}', ' ', text)\n",
        )

    def test_uses_ragkit_loader_and_normalizer(self) -> None:
        raw = "# 标题\n\n多  空格 文本。\n"
        normalized = "# 标题\n\n多 空格 文本。\n"
        items = [case("q1", "多少空格?", anchor("docs/demo.md", normalized, "多 空格 文本。"))]
        root = build_project(self.tmp, {"docs/demo.md": raw}, items)
        self._install_fake_ragkit(root)
        output = self.tmp / "out"

        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "ragkit_import.py"),
                "--ragkit-root",
                str(root),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, imp.EXIT_OK, completed.stderr)
        body = (output / "corpus" / "docs" / "demo.md").read_text(encoding="utf-8")
        self.assertEqual(body, normalized)
        self.assertNotIn("多  空格", body)
        fixture = json.loads((output / imp.FIXTURE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(fixture["cases"][0]["evidence"][0]["exact"], "多 空格 文本。")

    def test_missing_ragkit_package_is_a_usage_error(self) -> None:
        root = build_project(self.tmp, {"docs/a.md": "alpha\n"}, [])
        code, _, stderr = run_main(["--ragkit-root", str(root), "--output", str(self.tmp / "out")])

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("cannot find the ragkit package", stderr)


if __name__ == "__main__":
    unittest.main()
