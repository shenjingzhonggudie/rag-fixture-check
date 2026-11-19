"""refresh 子命令的合成数据测试。

覆盖两类场景：

1. 纯 Python 单元测试：冻结 fixture、重建语料、缺失与读取失败的区分、输出
   目录保护、manifest 不参与校验；
2. 真实 Git 端到端测试：第一次提交是旧快照，后续提交是 refresh 产物，用
   ``rag-fixture-check check`` 比较两个提交，断言退出码与失效题号。

端到端测试需要 ``rag-fixture-check`` 可执行文件。查找顺序：
``RAG_FIXTURE_CHECK_BIN`` → PATH 里的 ``rag-fixture-check`` → ``go build``。
都找不到时该用例以明确原因跳过，其余用例照常运行。
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

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ragkit_import as imp  # noqa: E402


class FakeBackend:
    """只做 CRLF 归一的最小后端。"""

    def normalized_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class FailingBackend:
    """模拟权限/解码类失败：文件存在但读不出来。"""

    def normalized_text(self, path: Path) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def write_text(path: Path, text: str) -> None:
    """写文本文件，父目录按需创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: object) -> None:
    """写 JSON 文件，格式与导入器输出一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def sha256_hex(data: bytes) -> str:
    """返回字节串的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


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


BODIES = {
    "docs/refund.md": "# Refund\n\nRefunds within 30 days.\n",
    "docs/shipping.md": "# Shipping\n\nOrders ship within 2 days.\n",
    "docs/faq.md": "# FAQ\n\nWe ship worldwide.\n",
}


def build_project(tmp: Path, manifest: dict | None = None) -> Path:
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
    if manifest is not None:
        write_json(root / "data" / "eval" / "annotation_manifest.json", manifest)
    return root


def run_cli(args: list[str], backend=None) -> tuple[int, str, str]:
    """在重定向的 stdout/stderr 下调用 CLI。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = imp.main(args, backend=backend)
    return code, stdout.getvalue(), stderr.getvalue()


def import_snapshot(root: Path, output: Path, *extra: str) -> tuple[int, str, str]:
    """跑一次 v0.2 导入，生成基线快照。"""
    return run_cli(["--ragkit-root", str(root), "--output", str(output), *extra], backend=FakeBackend())


def refresh_snapshot(
    root: Path,
    baseline: Path,
    output: Path,
    *extra: str,
    backend=None,
) -> tuple[int, str, str]:
    """跑一次 refresh。"""
    return run_cli(
        [
            "refresh",
            "--ragkit-root",
            str(root),
            "--baseline",
            str(baseline),
            "--output",
            str(output),
            *extra,
        ],
        backend=backend or FakeBackend(),
    )


class RefreshTests(unittest.TestCase):
    """refresh 的冻结、重建、缺失与失败语义。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = build_project(self.tmp)
        self.baseline = self.tmp / "snapshot-v1"
        code, _, stderr = import_snapshot(self.root, self.baseline)
        self.assertEqual(code, imp.EXIT_OK, stderr)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_keeps_fixture_bytes_and_rebuilds_corpus(self) -> None:
        original = (self.baseline / ".rag-fixtures.json").read_bytes()
        output = self.tmp / "snapshot-v2"

        code, stdout, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertEqual((output / ".rag-fixtures.json").read_bytes(), original)
        summary = json.loads((output / "refresh-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["mode"], "refresh")
        self.assertEqual(summary["frozenFixture"]["sha256"], sha256_hex(original))
        self.assertEqual(summary["counts"]["documents"], 3)
        self.assertEqual(summary["counts"]["rebuilt"], 3)
        self.assertEqual(summary["counts"]["missing"], 0)
        self.assertEqual(summary["missingPaths"], [])
        self.assertIn("status:       ok", stdout)
        for doc, body in BODIES.items():
            self.assertEqual((output / "corpus" / doc).read_text(encoding="utf-8"), body)

    def test_rewritten_quote_still_refreshes(self) -> None:
        write_text(self.root / "data" / "raw" / "docs" / "refund.md", "# Refund\n\nRefunds within 14 days.\n")
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        body = (output / "corpus" / "docs" / "refund.md").read_text(encoding="utf-8")
        self.assertIn("14 days", body)
        self.assertNotIn("30 days", body)
        summary = json.loads((output / "refresh-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"]["rebuilt"], 3)
        self.assertEqual(summary["missingPaths"], [])

    def test_deleted_source_is_omitted(self) -> None:
        (self.root / "data" / "raw" / "docs" / "shipping.md").unlink()
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertFalse((output / "corpus" / "docs" / "shipping.md").exists())
        summary = json.loads((output / "refresh-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["counts"]["missing"], 1)
        self.assertEqual(summary["missingPaths"], ["corpus/docs/shipping.md"])
        self.assertEqual(summary["counts"]["rebuilt"], 2)

    def test_renamed_source_is_treated_as_missing(self) -> None:
        raw = self.root / "data" / "raw" / "docs"
        (raw / "shipping.md").rename(raw / "delivery.md")
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        summary = json.loads((output / "refresh-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["missingPaths"], ["corpus/docs/shipping.md"])

    def test_decode_failure_is_an_error_not_a_deletion(self) -> None:
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output, backend=FailingBackend())

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("cannot read corpus/docs/", stderr)
        self.assertNotIn("missing", stderr.lower())
        self.assertFalse((output / ".rag-fixtures.json").exists())
        self.assertFalse((output / "corpus").exists())

    def test_directory_at_source_path_is_an_error(self) -> None:
        target = self.root / "data" / "raw" / "docs" / "refund.md"
        target.unlink()
        target.mkdir()
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("not a regular file", stderr)
        self.assertFalse((output / "corpus").exists())

    def test_refuses_baseline_as_output(self) -> None:
        code, _, stderr = refresh_snapshot(self.root, self.baseline, self.baseline)

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("read-only", stderr)

    def test_refuses_output_inside_baseline(self) -> None:
        code, _, stderr = refresh_snapshot(self.root, self.baseline, self.baseline / "nested")

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("read-only", stderr)

    def test_requires_force_to_replace_a_refresh_snapshot(self) -> None:
        output = self.tmp / "snapshot-v2"
        self.assertEqual(refresh_snapshot(self.root, self.baseline, output)[0], imp.EXIT_OK)

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)
        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("--force", stderr)

        write_text(output / "corpus" / "docs" / "stale.md", "stale\n")
        code, _, stderr = refresh_snapshot(self.root, self.baseline, output, "--force")
        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertFalse((output / "corpus" / "docs" / "stale.md").exists())

    def test_keeps_custom_corpus_prefix(self) -> None:
        custom = self.tmp / "snapshot-custom"
        code, _, stderr = import_snapshot(self.root, custom, "--corpus-prefix", "source")
        self.assertEqual(code, imp.EXIT_OK, stderr)

        output = self.tmp / "snapshot-v2"
        code, _, stderr = refresh_snapshot(self.root, custom, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertTrue((output / "source" / "docs" / "refund.md").is_file())
        summary = json.loads((output / "refresh-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["outputs"]["corpus"], "source")
        self.assertEqual((output / ".rag-fixtures.json").read_bytes(), (custom / ".rag-fixtures.json").read_bytes())

    def test_ignores_the_annotation_manifest(self) -> None:
        """manifest 描述旧语料；文档改动后 refresh 不能因为摘要不符而失败。"""
        root = self.tmp / "project-with-manifest"
        shutil.copytree(self.root, root, dirs_exist_ok=True)
        qrels = (root / "data" / "eval" / "qrels.json").read_bytes()
        manifest = {
            "qrels_sha256": sha256_hex(qrels),
            "corpus_sha256": {
                "docs/refund.md": sha256_hex(BODIES["docs/refund.md"].encode("utf-8")),
                "docs/shipping.md": sha256_hex(BODIES["docs/shipping.md"].encode("utf-8")),
                "docs/faq.md": sha256_hex(BODIES["docs/faq.md"].encode("utf-8")),
            },
        }
        write_json(root / "data" / "eval" / "annotation_manifest.json", manifest)
        baseline = self.tmp / "snapshot-manifest"
        code, _, stderr = import_snapshot(root, baseline)
        self.assertEqual(code, imp.EXIT_OK, stderr)

        write_text(root / "data" / "raw" / "docs" / "refund.md", "# Refund\n\nRefunds within 14 days.\n")
        output = self.tmp / "snapshot-v2"
        code, _, stderr = refresh_snapshot(root, baseline, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertTrue((output / "corpus" / "docs" / "refund.md").is_file())

    def test_rejects_broken_baseline_fixture(self) -> None:
        write_text(self.baseline / ".rag-fixtures.json", '{"version": 1, "cases": [{"id": "x"}]}')
        code, _, stderr = refresh_snapshot(self.root, self.baseline, self.tmp / "snapshot-v2")

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("no evidence array", stderr)

    def test_missing_baseline_fixture_is_an_error(self) -> None:
        (self.baseline / ".rag-fixtures.json").unlink()
        code, _, stderr = refresh_snapshot(self.root, self.baseline, self.tmp / "snapshot-v2")

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("baseline fixture", stderr)

    def test_preserves_fixture_with_optional_question_omitted(self) -> None:
        fixture_path = self.baseline / imp.FIXTURE_NAME
        fixture = json.loads(fixture_path.read_bytes())
        del fixture["cases"][0]["question"]
        write_json(fixture_path, fixture)
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertEqual((output / imp.FIXTURE_NAME).read_bytes(), fixture_path.read_bytes())

    def test_file_blocking_source_path_is_an_error(self) -> None:
        docs = self.root / "data" / "raw" / "docs"
        docs.rename(docs.with_name("previous-docs"))
        write_text(docs, "This file blocks the docs directory.")
        output = self.tmp / "snapshot-v2"

        code, _, stderr = refresh_snapshot(self.root, self.baseline, output)

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("source parent is not a directory", stderr)
        self.assertFalse(output.exists())

    def test_rejects_fixture_the_go_checker_cannot_read(self) -> None:
        fixture_path = self.baseline / imp.FIXTURE_NAME
        valid = fixture_path.read_bytes()
        fixture = json.loads(valid)
        fixtures = [
            b"\xef\xbb\xbf" + valid,
            json.dumps(fixture | {"version": True}).encode(),
            json.dumps(fixture | {"extra": None}).encode(),
            json.dumps(fixture | {"cases": [fixture["cases"][0]] * 2}).encode(),
        ]
        output = self.tmp / "snapshot-v2"
        for data in fixtures:
            with self.subTest(data=data[:40]):
                fixture_path.write_bytes(data)
                code, _, stderr = refresh_snapshot(self.root, self.baseline, output)
                self.assertEqual(code, imp.EXIT_USAGE, stderr)
                self.assertIn("baseline fixture", stderr)
                self.assertFalse(output.exists())


# --- 真实 Git 端到端 -------------------------------------------------------

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


def git_env(tmp: Path) -> dict[str, str]:
    """隔离 Git 配置，避免本机 global 设置影响测试。"""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Refresh Test",
        "GIT_AUTHOR_EMAIL": "refresh@example.com",
        "GIT_COMMITTER_NAME": "Refresh Test",
        "GIT_COMMITTER_EMAIL": "refresh@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(repo: Path, *args: str, env: dict[str, str]) -> str:
    """执行 git 并返回 stdout；失败即断言失败。"""
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout


def git_bytes(repo: Path, *args: str, env: dict[str, str]) -> bytes:
    """执行 git 并返回原始 stdout 字节。"""
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace')}")
    return done.stdout


def install_snapshot(snapshot: Path, repo: Path) -> None:
    """用快照内容替换仓库里的快照（删除旧 corpus，避免残留文件）。"""
    shutil.copy2(snapshot / ".rag-fixtures.json", repo / ".rag-fixtures.json")
    shutil.rmtree(repo / "corpus", ignore_errors=True)
    shutil.copytree(snapshot / "corpus", repo / "corpus")
    summary = snapshot / "refresh-summary.json"
    if summary.is_file():
        shutil.copy2(summary, repo / "refresh-summary.json")


def run_check(checker: Path, repo: Path, base: str, head: str) -> tuple[int, dict]:
    """运行 Go 检查器并解析 JSON 报告。"""
    done = subprocess.run(
        [
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
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if done.returncode not in (0, 1):
        raise AssertionError(f"check exited {done.returncode}: {done.stderr}")
    return done.returncode, json.loads(done.stdout)


def statuses(report: dict) -> dict[str, list[str]]:
    """把报告压成 题号 → 状态列表。"""
    return {case["id"]: [item["status"] for item in case["evidence"]] for case in report["cases"]}


class RefreshGitEndToEndTests(unittest.TestCase):
    """旧快照提交 vs refresh 产物的两次提交对比。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.checker = find_checker()
        if self.checker is None:
            self.skipTest("rag-fixture-check binary not available (set RAG_FIXTURE_CHECK_BIN or install Go)")
        if shutil.which("git") is None:
            self.skipTest("git is not available in PATH")
        self.env = git_env(self.tmp)

        self.root = build_project(self.tmp)
        self.snapshot1 = self.tmp / "snapshot-v1"
        code, _, stderr = import_snapshot(self.root, self.snapshot1)
        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.fixture_bytes = (self.snapshot1 / ".rag-fixtures.json").read_bytes()

        self.repo = self.tmp / "snapshot-repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main", env=self.env)
        install_snapshot(self.snapshot1, self.repo)
        git(self.repo, "add", "-A", env=self.env)
        git(self.repo, "commit", "-qm", "baseline snapshot", env=self.env)
        self.base = git(self.repo, "rev-parse", "HEAD", env=self.env).strip()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def commit_refreshed(self, snapshot: Path, message: str) -> str:
        """把 refresh 产物装进仓库并提交，返回 SHA。"""
        install_snapshot(snapshot, self.repo)
        git(self.repo, "add", "-A", env=self.env)
        git(self.repo, "commit", "-qm", message, env=self.env)
        return git(self.repo, "rev-parse", "HEAD", env=self.env).strip()

    def assert_fixture_unchanged(self, sha: str) -> None:
        """fixture 在各次提交里必须逐字节相同。"""
        self.assertEqual(git_bytes(self.repo, "show", f"{sha}:.rag-fixtures.json", env=self.env), self.fixture_bytes)

    def test_no_change_rewrite_and_delete(self) -> None:
        # 1. 无改动：refresh 产物与原快照完全一致，check 退出 0。
        unchanged = self.tmp / "snapshot-unchanged"
        code, _, stderr = refresh_snapshot(self.root, self.snapshot1, unchanged)
        self.assertEqual(code, imp.EXIT_OK, stderr)
        unchanged_sha = self.commit_refreshed(unchanged, "refresh without source changes")
        self.assert_fixture_unchanged(unchanged_sha)

        code, report = run_check(self.checker, self.repo, self.base, unchanged_sha)
        self.assertEqual(code, 0, report["summary"])
        self.assertEqual(report["summary"]["intact"], 3)
        self.assertEqual(report["summary"]["missing"], 0)

        # 2. 原文改写：refresh 成功，check 报 refund-30 失效、退出 1。
        write_text(self.root / "data" / "raw" / "docs" / "refund.md", "# Refund\n\nRefunds within 14 days.\n")
        rewritten = self.tmp / "snapshot-rewritten"
        code, _, stderr = refresh_snapshot(self.root, self.snapshot1, rewritten)
        self.assertEqual(code, imp.EXIT_OK, stderr)
        rewritten_sha = self.commit_refreshed(rewritten, "rewrite the refund quote")
        self.assert_fixture_unchanged(rewritten_sha)

        code, report = run_check(self.checker, self.repo, unchanged_sha, rewritten_sha)
        self.assertEqual(code, 1, report["summary"])
        self.assertEqual(statuses(report)["refund-30"], ["missing"])
        self.assertEqual(statuses(report)["shipping"], ["intact"])
        self.assertEqual(statuses(report)["faq"], ["intact"])

        # 3. 文件删除：refresh 省略该文件，check 报 file_missing、退出 1。
        (self.root / "data" / "raw" / "docs" / "shipping.md").unlink()
        deleted = self.tmp / "snapshot-deleted"
        code, stdout, stderr = refresh_snapshot(self.root, self.snapshot1, deleted)
        self.assertEqual(code, imp.EXIT_OK, stderr)
        self.assertIn("missing source: corpus/docs/shipping.md", stdout)
        deleted_sha = self.commit_refreshed(deleted, "delete the shipping page")
        self.assert_fixture_unchanged(deleted_sha)

        code, report = run_check(self.checker, self.repo, rewritten_sha, deleted_sha)
        self.assertEqual(code, 1, report["summary"])
        self.assertEqual(statuses(report)["shipping"], ["file_missing"])
        self.assertEqual(statuses(report)["refund-30"], ["missing"])

    def test_read_failure_leaves_the_baseline_untouched(self) -> None:
        before = (self.snapshot1 / ".rag-fixtures.json").read_bytes()
        output = self.tmp / "snapshot-broken"

        code, _, stderr = refresh_snapshot(self.root, self.snapshot1, output, backend=FailingBackend())

        self.assertEqual(code, imp.EXIT_USAGE)
        self.assertIn("cannot read corpus/docs/", stderr)
        self.assertFalse(output.exists())
        self.assertEqual((self.snapshot1 / ".rag-fixtures.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
