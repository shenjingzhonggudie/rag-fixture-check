package app_test

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/app"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/fixture"
)

// 集成测试全部基于真实临时 Git 仓库：不 mock git，也不依赖当前工作目录。

// reportDoc 是测试侧对 JSON 报告的最小结构镜像。
type reportDoc struct {
	SchemaVersion int    `json:"schemaVersion"`
	Repo          string `json:"repo"`
	Base          string `json:"base"`
	Head          string `json:"head"`
	Fixture       string `json:"fixture"`
	Summary       struct {
		Cases          int  `json:"cases"`
		Evidence       int  `json:"evidence"`
		Intact         int  `json:"intact"`
		Relocated      int  `json:"relocated"`
		Missing        int  `json:"missing"`
		Ambiguous      int  `json:"ambiguous"`
		ContextChanged int  `json:"contextChanged"`
		FileMissing    int  `json:"fileMissing"`
		Unreadable     int  `json:"unreadable"`
		NeedsReview    bool `json:"needsReview"`
	} `json:"summary"`
	FixtureChanges struct {
		Added    []string `json:"added"`
		Modified []string `json:"modified"`
		Removed  []string `json:"removed"`
	} `json:"fixtureChanges"`
	RemovedCases []struct {
		ID            string   `json:"id"`
		EvidenceCount int      `json:"evidenceCount"`
		EvidencePaths []string `json:"evidencePaths"`
	} `json:"removedCases"`
	Cases []struct {
		ID       string         `json:"id"`
		Question string         `json:"question"`
		Change   string         `json:"change"`
		Evidence []reportAnchor `json:"evidence"`
	} `json:"cases"`
}

// reportAnchor 是单条锚点的 JSON 结构镜像。
type reportAnchor struct {
	Index           int    `json:"index"`
	Path            string `json:"path"`
	Status          string `json:"status"`
	OldPath         string `json:"oldPath"`
	NewPath         string `json:"newPath"`
	OldLine         int    `json:"oldLine"`
	NewLine         int    `json:"newLine"`
	Occurrences     int    `json:"occurrences"`
	Selected        int    `json:"selected"`
	SelectorChanged bool   `json:"selectorChanged"`
	Detail          string `json:"detail"`
}

// testRepo 是一个隔离的临时仓库。
type testRepo struct {
	t   *testing.T
	dir string
}

// isolateGitEnv 把 git 的全局/系统配置指向临时文件，保证测试不受本机配置影响。
func isolateGitEnv(t *testing.T) {
	t.Helper()
	t.Setenv("GIT_CONFIG_GLOBAL", filepath.Join(t.TempDir(), "gitconfig"))
	t.Setenv("GIT_CONFIG_NOSYSTEM", "1")
	t.Setenv("GIT_AUTHOR_NAME", "Fixture Check")
	t.Setenv("GIT_AUTHOR_EMAIL", "fixture@example.com")
	t.Setenv("GIT_COMMITTER_NAME", "Fixture Check")
	t.Setenv("GIT_COMMITTER_EMAIL", "fixture@example.com")
	t.Setenv("GIT_TERMINAL_PROMPT", "0")
}

// newTestRepo 创建并初始化一个临时仓库。
func newTestRepo(t *testing.T) *testRepo {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not available in PATH")
	}
	isolateGitEnv(t)
	r := &testRepo{t: t, dir: t.TempDir()}
	r.git("init", "-b", "main")
	r.git("config", "core.autocrlf", "false")
	r.git("config", "commit.gpgsign", "false")
	r.git("config", "tag.gpgsign", "false")
	return r
}

// git 执行一条 git 命令，失败即终止测试。
func (r *testRepo) git(args ...string) string {
	r.t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = r.dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		r.t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

// gitWithInput 执行一条带标准输入的 git 命令。
func (r *testRepo) gitWithInput(input string, args ...string) string {
	r.t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = r.dir
	cmd.Stdin = strings.NewReader(input)
	out, err := cmd.CombinedOutput()
	if err != nil {
		r.t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return string(out)
}

// write 写入仓库内文件，自动创建父目录。
func (r *testRepo) write(path, content string) {
	r.t.Helper()
	full := filepath.Join(r.dir, filepath.FromSlash(path))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		r.t.Fatalf("mkdir for %q: %v", path, err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		r.t.Fatalf("write %q: %v", path, err)
	}
}

// mkdir 在仓库内创建目录。
func (r *testRepo) mkdir(path string) {
	r.t.Helper()
	if err := os.MkdirAll(filepath.Join(r.dir, filepath.FromSlash(path)), 0o755); err != nil {
		r.t.Fatalf("mkdir %q: %v", path, err)
	}
}

// remove 删除仓库内文件。
func (r *testRepo) remove(path string) {
	r.t.Helper()
	if err := os.Remove(filepath.Join(r.dir, filepath.FromSlash(path))); err != nil {
		r.t.Fatalf("remove %q: %v", path, err)
	}
}

// commit 暂存全部改动并提交，返回完整 SHA。
func (r *testRepo) commit(msg string) string {
	r.t.Helper()
	r.git("add", "-A")
	r.git("commit", "--no-gpg-sign", "-m", msg)
	return strings.TrimSpace(r.git("rev-parse", "HEAD"))
}

// check 以默认 repo 运行 check 子命令。
func (r *testRepo) check(base, head string, extra ...string) (int, string, string) {
	r.t.Helper()
	args := append([]string{
		"check", "--repo", r.dir, "--base", base, "--head", head,
	}, extra...)
	return runRaw(r.t, args...)
}

// runRaw 直接调用 app.Run 并收集输出。
func runRaw(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := app.Run(args, &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

// fixtureJSON 把 case 列表序列化成 fixture 文本。
func fixtureJSON(t *testing.T, cases ...fixture.Case) string {
	t.Helper()
	if cases == nil {
		cases = []fixture.Case{}
	}
	data, err := json.MarshalIndent(fixture.Document{Version: fixture.Version, Cases: cases}, "", "  ")
	if err != nil {
		t.Fatalf("marshal fixture: %v", err)
	}
	return string(data) + "\n"
}

// quote 构造一条只有 exact 的证据。
func quote(path, exact string) fixture.Evidence {
	return fixture.Evidence{Path: path, Exact: exact}
}

// decodeReport 解析 JSON 输出。
func decodeReport(t *testing.T, out string) reportDoc {
	t.Helper()
	var doc reportDoc
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatalf("decode JSON report: %v\noutput:\n%s", err, out)
	}
	return doc
}

// anchorAt 取出指定 case 的第 index 条锚点结果。
func anchorAt(t *testing.T, doc reportDoc, caseID string, index int) reportAnchor {
	t.Helper()
	for _, c := range doc.Cases {
		if c.ID != caseID {
			continue
		}
		if index >= len(c.Evidence) {
			t.Fatalf("case %q has %d evidence entries, want index %d", caseID, len(c.Evidence), index)
		}
		return c.Evidence[index]
	}
	t.Fatalf("case %q missing from report", caseID)
	return reportAnchor{}
}

// 退款期从 30 天改成 14 天：引文消失，必须报 missing 并以退出码 1 结束。
func TestQuoteChangedIsMissing(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/refund.md", "# Refunds\n\nRefunds are accepted within 30 days.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "refund-30",
		Question: "How many days?",
		Evidence: []fixture.Evidence{quote("docs/refund.md", "Refunds are accepted within 30 days.")},
	}))
	base := r.commit("base")

	r.write("docs/refund.md", "# Refunds\n\nRefunds are accepted within 14 days.\n")
	head := r.commit("head")

	code, stdout, stderr := r.check(base, head)
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d\nstdout:\n%s\nstderr:\n%s", code, app.ExitReview, stdout, stderr)
	}
	if !strings.Contains(stdout, "[missing]") {
		t.Fatalf("text output must report [missing]:\n%s", stdout)
	}
	if !strings.Contains(stdout, "matches=0") {
		t.Fatalf("text output must report the match count:\n%s", stdout)
	}

	_, out, _ := r.check(base, head, "--format", "json")
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "refund-30", 0)
	if a.Status != "missing" {
		t.Fatalf("status = %q, want missing", a.Status)
	}
	if a.Occurrences != 0 || a.Selected != 0 {
		t.Fatalf("occurrences/selected = %d/%d, want 0/0", a.Occurrences, a.Selected)
	}
	if a.OldLine != 3 || a.NewLine != 0 {
		t.Fatalf("old/new line = %d/%d, want 3/0", a.OldLine, a.NewLine)
	}
	if doc.Base != base || doc.Head != head {
		t.Fatalf("report must pin full SHAs, got base=%q head=%q", doc.Base, doc.Head)
	}
	if !doc.Summary.NeedsReview || doc.Summary.Missing != 1 {
		t.Fatalf("summary = %+v, want needsReview with one missing", doc.Summary)
	}
}

// 引文仍在同一文件里，只是上方插入了内容：报 relocated，但仍算有效。
func TestQuoteMovedInSameFileIsRelocated(t *testing.T) {
	r := newTestRepo(t)
	before := "# Shipping\n\nOrders ship within 2 days.\n\n## Tracking\n\nTracking numbers are emailed.\n"
	r.write("docs/shipping.md", before)
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "shipping",
		Question: "How fast do orders ship?",
		Evidence: []fixture.Evidence{quote("docs/shipping.md", "Orders ship within 2 days.")},
	}))
	base := r.commit("base")

	r.write("docs/shipping.md", "# Shipping\n\nWe now ship worldwide.\n\nOrders ship within 2 days.\n\n## Tracking\n\nTracking numbers are emailed.\n")
	head := r.commit("head")

	code, out, stderr := r.check(base, head, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "shipping", 0)
	if a.Status != "relocated" {
		t.Fatalf("status = %q, want relocated (%s)", a.Status, a.Detail)
	}
	if a.OldLine != 3 || a.NewLine != 5 {
		t.Fatalf("old/new line = %d/%d, want 3/5", a.OldLine, a.NewLine)
	}
	if doc.Summary.Relocated != 1 || doc.Summary.NeedsReview {
		t.Fatalf("summary = %+v, want one relocated and no review", doc.Summary)
	}
}

// 文件被重命名、fixture 没跟着改：只能靠真实 Git 重命名映射找回。
func TestRenameRescuesMissingPath(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/faq.md", "# FAQ\n\nDo you ship internationally? No.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "faq-shipping",
		Question: "Do you ship internationally?",
		Evidence: []fixture.Evidence{quote("docs/faq.md", "Do you ship internationally? No.")},
	}))
	base := r.commit("base")

	r.mkdir("docs/guides")
	r.git("mv", "docs/faq.md", "docs/guides/faq.md")
	head := r.commit("rename")

	code, out, stderr := r.check(base, head, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "faq-shipping", 0)
	if a.Status != "relocated" {
		t.Fatalf("status = %q, want relocated (%s)", a.Status, a.Detail)
	}
	if a.NewPath != "docs/guides/faq.md" {
		t.Fatalf("newPath = %q, want docs/guides/faq.md", a.NewPath)
	}
	if a.NewLine != 3 {
		t.Fatalf("newLine = %d, want 3", a.NewLine)
	}
}

// 文件被重命名、fixture 同步更新了 path：仍然报告为 relocated，并标明旧路径。
func TestRenameWithUpdatedFixturePath(t *testing.T) {
	r := newTestRepo(t)
	body := "# FAQ\n\nDo you ship internationally? No.\n"
	r.write("docs/faq.md", body)
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "faq-shipping",
		Evidence: []fixture.Evidence{quote("docs/faq.md", "Do you ship internationally? No.")},
	}))
	base := r.commit("base")

	r.mkdir("docs/guides")
	r.git("mv", "docs/faq.md", "docs/guides/faq.md")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "faq-shipping",
		Evidence: []fixture.Evidence{quote("docs/guides/faq.md", "Do you ship internationally? No.")},
	}))
	head := r.commit("rename and update fixture")

	code, out, _ := r.check(base, head, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d", code, app.ExitOK)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "faq-shipping", 0)
	if a.Status != "relocated" {
		t.Fatalf("status = %q, want relocated (%s)", a.Status, a.Detail)
	}
	if a.Path != "docs/guides/faq.md" || a.OldPath != "docs/faq.md" {
		t.Fatalf("path/oldPath = %q/%q", a.Path, a.OldPath)
	}
	if !a.SelectorChanged {
		t.Fatal("selectorChanged must be true when the fixture path was updated")
	}
}

// 标题改动导致 prefix 对不上：引文还在，但上下文漂移。
func TestContextDrift(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/refund.md", "## Refunds\n\nRefunds are accepted within 30 days.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "refund-context",
		Evidence: []fixture.Evidence{{Path: "docs/refund.md", Exact: "Refunds are accepted within 30 days.", Prefix: "## Refunds\n\n"}},
	}))
	base := r.commit("base")

	r.write("docs/refund.md", "## Returns\n\nRefunds are accepted within 30 days.\n")
	head := r.commit("retitle")

	code, out, _ := r.check(base, head, "--format", "json")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d", code, app.ExitReview)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "refund-context", 0)
	if a.Status != "context_changed" {
		t.Fatalf("status = %q, want context_changed (%s)", a.Status, a.Detail)
	}
	if a.Occurrences != 1 || a.Selected != 0 {
		t.Fatalf("occurrences/selected = %d/%d, want 1/0", a.Occurrences, a.Selected)
	}
}

// 同一句出现两次且没有选择器：必须报 ambiguous，不能偷偷取第一条。
func TestAmbiguousQuoteIsNotSilentlyResolved(t *testing.T) {
	r := newTestRepo(t)
	body := "# Dup\n\nRefunds are accepted within 30 days.\n\nRefunds are accepted within 30 days.\n"
	r.write("docs/dup.md", body)
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "dup",
		Evidence: []fixture.Evidence{quote("docs/dup.md", "Refunds are accepted within 30 days.")},
	}))
	sha := r.commit("base")

	code, out, _ := r.check(sha, sha, "--format", "json")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d", code, app.ExitReview)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "dup", 0)
	if a.Status != "ambiguous" {
		t.Fatalf("status = %q, want ambiguous", a.Status)
	}
	if a.Occurrences != 2 || a.Selected != 2 {
		t.Fatalf("occurrences/selected = %d/%d, want 2/2", a.Occurrences, a.Selected)
	}
	if a.NewLine != 0 {
		t.Fatalf("newLine = %d, want 0: no line may be chosen for an ambiguous quote", a.NewLine)
	}
}

// 选择器能把重复引文收敛到唯一一处时，仍然是 intact。
func TestSelectorDisambiguatesDuplicate(t *testing.T) {
	r := newTestRepo(t)
	body := "## A\n\nRefunds are accepted within 30 days.\n\n## B\n\nRefunds are accepted within 30 days.\n"
	r.write("docs/dup.md", body)
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "dup-selector",
		Evidence: []fixture.Evidence{{
			Path:   "docs/dup.md",
			Exact:  "Refunds are accepted within 30 days.",
			Prefix: "## B\n\n",
		}},
	}))
	sha := r.commit("base")

	code, out, _ := r.check(sha, sha, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d", code, app.ExitOK)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "dup-selector", 0)
	if a.Status != "intact" || a.Selected != 1 || a.NewLine != 7 {
		t.Fatalf("status/selected/line = %q/%d/%d, want intact/1/7", a.Status, a.Selected, a.NewLine)
	}
}

// 中文路径与带空格路径都必须可读可报。
func TestChineseAndSpacedPaths(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/退款说明.md", "# 退款\n\n退款在 30 天内受理。\n")
	r.write("docs/refund policy.md", "Policy text.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t,
		fixture.Case{
			ID:       "退款",
			Question: "多少天内可以退款？",
			Evidence: []fixture.Evidence{quote("docs/退款说明.md", "退款在 30 天内受理。")},
		},
		fixture.Case{
			ID:       "spaced",
			Evidence: []fixture.Evidence{quote("docs/refund policy.md", "Policy text.")},
		},
	))
	sha := r.commit("base")

	code, out, stderr := r.check(sha, sha, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	doc := decodeReport(t, out)
	if a := anchorAt(t, doc, "退款", 0); a.Status != "intact" || a.NewLine != 3 {
		t.Fatalf("chinese anchor = %q line %d, want intact line 3", a.Status, a.NewLine)
	}
	if a := anchorAt(t, doc, "spaced", 0); a.Status != "intact" {
		t.Fatalf("spaced anchor = %q, want intact", a.Status)
	}
}

// 文档用 CRLF 存储，fixture 用 LF 书写：归一后必须命中。
func TestCRLFDocument(t *testing.T) {
	r := newTestRepo(t)
	body := "# Refund\r\n\r\nRefunds are accepted within 30 days.\r\n"
	r.write("docs/crlf.md", body)
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "crlf",
		Evidence: []fixture.Evidence{{
			Path:  "docs/crlf.md",
			Exact: "Refunds are accepted within 30 days.\n",
		}},
	}))
	sha := r.commit("base")

	code, out, stderr := r.check(sha, sha, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	doc := decodeReport(t, out)
	a := anchorAt(t, doc, "crlf", 0)
	if a.Status != "intact" || a.NewLine != 3 {
		t.Fatalf("status/line = %q/%d, want intact/3 (%s)", a.Status, a.NewLine, a.Detail)
	}
}

// 工作树被改脏不影响判定：只读已提交的 tree。
func TestDirtyWorktreeIsIgnored(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/refund.md", "# Refunds\n\nRefunds are accepted within 30 days.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "refund-30",
		Evidence: []fixture.Evidence{quote("docs/refund.md", "Refunds are accepted within 30 days.")},
	}))
	base := r.commit("base")

	// 提交里引文保持 30 天，只在文件末尾追加一节。
	r.write("docs/refund.md", "# Refunds\n\nRefunds are accepted within 30 days.\n\n## Contact\n\nEmail support.\n")
	head := r.commit("head")

	// 未提交的改动把 30 天改成 365 天：如果工具读工作树，这条锚点会变成 missing。
	r.write("docs/refund.md", "# Refunds\n\nRefunds are accepted within 365 days.\n\n## Contact\n\nEmail support.\n")
	r.write("docs/untracked.md", "scratch\n")

	code, out, stderr := r.check(base, head, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	a := anchorAt(t, decodeReport(t, out), "refund-30", 0)
	if a.Status != "intact" || a.NewLine != 3 {
		t.Fatalf("status/line = %q/%d, want intact/3", a.Status, a.NewLine)
	}
}

// 新增题与删题都要可见，但都不算锚点失效。
func TestAddedAndRemovedCases(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/a.md", "Alpha.\n")
	r.write("docs/b.md", "Beta.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t,
		fixture.Case{ID: "a", Question: "Alpha?", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")}},
		fixture.Case{ID: "b", Question: "Beta?", Evidence: []fixture.Evidence{quote("docs/b.md", "Beta.")}},
	))
	base := r.commit("base")

	r.write("docs/c.md", "Gamma.\n")
	r.remove("docs/b.md")
	r.write(".rag-fixtures.json", fixtureJSON(t,
		fixture.Case{ID: "a", Question: "Alpha?", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")}},
		fixture.Case{ID: "c", Question: "Gamma?", Evidence: []fixture.Evidence{quote("docs/c.md", "Gamma.")}},
	))
	head := r.commit("add c, drop b")

	code, out, stderr := r.check(base, head, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	doc := decodeReport(t, out)
	if len(doc.FixtureChanges.Added) != 1 || doc.FixtureChanges.Added[0] != "c" {
		t.Fatalf("added = %v, want [c]", doc.FixtureChanges.Added)
	}
	if len(doc.FixtureChanges.Removed) != 1 || doc.FixtureChanges.Removed[0] != "b" {
		t.Fatalf("removed = %v, want [b]", doc.FixtureChanges.Removed)
	}
	if len(doc.RemovedCases) != 1 || doc.RemovedCases[0].ID != "b" {
		t.Fatalf("removedCases = %+v, want one entry for b", doc.RemovedCases)
	}
	if doc.RemovedCases[0].EvidenceCount != 1 {
		t.Fatalf("removed case evidenceCount = %d, want 1", doc.RemovedCases[0].EvidenceCount)
	}
	for _, c := range doc.Cases {
		if c.ID == "c" && c.Change != "added" {
			t.Fatalf("case c change = %q, want added", c.Change)
		}
	}
}

// 证据文件被删除且没有重命名可循：file_missing。
func TestDeletedFileIsFileMissing(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/gone.md", "Legacy policy text.\n")
	r.write("docs/keep.md", "Keep this.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "legacy",
		Evidence: []fixture.Evidence{quote("docs/gone.md", "Legacy policy text.")},
	}))
	base := r.commit("base")

	r.remove("docs/gone.md")
	head := r.commit("drop legacy page")

	code, out, _ := r.check(base, head, "--format", "json")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d", code, app.ExitReview)
	}
	a := anchorAt(t, decodeReport(t, out), "legacy", 0)
	if a.Status != "file_missing" {
		t.Fatalf("status = %q, want file_missing (%s)", a.Status, a.Detail)
	}
	if a.OldLine != 1 {
		t.Fatalf("oldLine = %d, want 1", a.OldLine)
	}
}

// 符号链接不是普通 blob，必须拒绝而不是跟随。
func TestSymlinkIsRejected(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/real.md", "Real content.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID:       "link",
		Evidence: []fixture.Evidence{quote("docs/link.md", "Real content.")},
	}))
	base := r.commit("base")

	oid := strings.TrimSpace(r.gitWithInput("docs/real.md", "hash-object", "-w", "--stdin"))
	r.git("update-index", "--add", "--cacheinfo", "120000", oid, "docs/link.md")
	r.git("commit", "--no-gpg-sign", "-m", "add symlink")
	head := strings.TrimSpace(r.git("rev-parse", "HEAD"))

	code, out, _ := r.check(base, head, "--format", "json")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d", code, app.ExitReview)
	}
	a := anchorAt(t, decodeReport(t, out), "link", 0)
	if a.Status != "unreadable" {
		t.Fatalf("status = %q, want unreadable (%s)", a.Status, a.Detail)
	}
	if !strings.Contains(a.Detail, "symlink") {
		t.Fatalf("detail = %q, want it to mention symlink", a.Detail)
	}
}

// 同一文件里的多条引文各自独立判定。
func TestMultipleQuotesInSameFile(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/policy.md", "# Policy\n\nRefunds within 30 days.\n\nShipping within 2 days.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "multi",
		Evidence: []fixture.Evidence{
			quote("docs/policy.md", "Refunds within 30 days."),
			quote("docs/policy.md", "Shipping within 2 days."),
		},
	}))
	base := r.commit("base")

	r.write("docs/policy.md", "# Policy\n\nRefunds within 14 days.\n\nShipping within 2 days.\n")
	head := r.commit("head")

	code, out, _ := r.check(base, head, "--format", "json")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d", code, app.ExitReview)
	}
	doc := decodeReport(t, out)
	first := anchorAt(t, doc, "multi", 0)
	second := anchorAt(t, doc, "multi", 1)
	if first.Status != "missing" {
		t.Fatalf("first status = %q, want missing", first.Status)
	}
	if second.Status != "intact" || second.NewLine != 5 {
		t.Fatalf("second status/line = %q/%d, want intact/5", second.Status, second.NewLine)
	}
	if doc.Summary.Evidence != 2 || doc.Summary.Missing != 1 || doc.Summary.Intact != 1 {
		t.Fatalf("summary = %+v, want 2 evidence with 1 missing and 1 intact", doc.Summary)
	}
}

// 从子目录启动也要能定位仓库根：nested cwd / nested --repo。
func TestNestedRepoPath(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/a.md", "Alpha.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "a", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")},
	}))
	sha := r.commit("base")

	nested := filepath.Join(r.dir, "docs")
	code, out, stderr := runRaw(t, "check", "--repo", nested, "--base", sha, "--head", sha, "--format", "json")
	if code != app.ExitOK {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitOK, stderr)
	}
	doc := decodeReport(t, out)
	gotInfo, err := os.Stat(doc.Repo)
	if err != nil {
		t.Fatalf("stat reported repo root %q: %v", doc.Repo, err)
	}
	wantInfo, err := os.Stat(r.dir)
	if err != nil {
		t.Fatalf("stat temp repo %q: %v", r.dir, err)
	}
	if !os.SameFile(gotInfo, wantInfo) {
		t.Fatalf("repo root = %q, want the worktree root %q", doc.Repo, r.dir)
	}
}

// commit-ish 会被固定成完整 SHA，并接受 HEAD~1 这类常见写法。
func TestCommitIshIsPinnedToFullSHA(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/a.md", "Alpha.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "a", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")},
	}))
	base := r.commit("base")
	r.write("docs/a.md", "Alpha v2.\n")
	head := r.commit("head")

	code, out, stderr := r.check("HEAD~1", "HEAD", "--format", "json")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitReview, stderr)
	}
	doc := decodeReport(t, out)
	if doc.Base != base || doc.Head != head {
		t.Fatalf("pinned SHAs = %q/%q, want %q/%q", doc.Base, doc.Head, base, head)
	}
	if len(doc.Base) != 40 || len(doc.Head) != 40 {
		t.Fatalf("expected full 40-char SHAs, got %d and %d chars", len(doc.Base), len(doc.Head))
	}
}

// JSON 输出必须可重复：同一输入逐字节相同。
func TestJSONOutputIsDeterministic(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/a.md", "Alpha.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "a", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")},
	}))
	base := r.commit("base")
	r.write("docs/a.md", "Alpha changed.\n")
	head := r.commit("head")

	code1, out1, _ := r.check(base, head, "--format", "json")
	code2, out2, _ := r.check(base, head, "--format", "json")
	if code1 != code2 {
		t.Fatalf("exit codes differ: %d vs %d", code1, code2)
	}
	if out1 != out2 {
		t.Fatalf("JSON output is not deterministic:\n%s\n---\n%s", out1, out2)
	}
	if decodeReport(t, out1).SchemaVersion != 1 {
		t.Fatalf("schemaVersion = %d, want 1", decodeReport(t, out1).SchemaVersion)
	}
}

// GitHub 格式对失效锚点发 error 注解，并以 error 结尾。
func TestGitHubFormat(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/refund.md", "Refunds are accepted within 30 days.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "refund-30", Evidence: []fixture.Evidence{quote("docs/refund.md", "Refunds are accepted within 30 days.")},
	}))
	base := r.commit("base")
	r.write("docs/refund.md", "Refunds are accepted within 14 days.\n")
	head := r.commit("head")

	code, out, stderr := r.check(base, head, "--format", "github")
	if code != app.ExitReview {
		t.Fatalf("exit = %d, want %d\nstderr:\n%s", code, app.ExitReview, stderr)
	}
	if !strings.Contains(out, "::error file=docs/refund.md,title=rag-fixture-check (missing)::") {
		t.Fatalf("missing error annotation:\n%s", out)
	}
	if !strings.Contains(out, "::error title=rag-fixture-check::") {
		t.Fatalf("missing summary annotation:\n%s", out)
	}
	for _, line := range strings.Split(strings.TrimRight(out, "\n"), "\n") {
		if !strings.HasPrefix(line, "::") {
			t.Fatalf("unexpected non-annotation line %q", line)
		}
	}
}

// 输入错误统一退出码 2，且不产出半截报告。
func TestInputErrorsExitTwo(t *testing.T) {
	r := newTestRepo(t)
	r.write("docs/a.md", "Alpha.\n")
	r.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "a", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")},
	}))
	base := r.commit("base")
	notARepo := t.TempDir()
	t.Setenv("GIT_CEILING_DIRECTORIES", filepath.Dir(notARepo))

	cases := []struct {
		name string
		args []string
		want string
	}{
		{"missing repo path", []string{"check", "--repo", filepath.Join(notARepo, "nope"), "--base", base, "--head", base}, "repo path"},
		{"not a git repo", []string{"check", "--repo", notARepo, "--base", base, "--head", base}, "not a git repository"},
		{"unknown revision", []string{"check", "--repo", r.dir, "--base", "does-not-exist", "--head", base}, "cannot resolve"},
		{"revision flag injection", []string{"check", "--repo", r.dir, "--base", "--upload-pack=/tmp/x", "--head", base}, "must not start"},
		{"unknown flag", []string{"check", "--repo", r.dir, "--base", base, "--head", base, "--bogus", "1"}, "unknown flag"},
		{"missing head", []string{"check", "--repo", r.dir, "--base", base}, "--head is required"},
		{"bad format", []string{"check", "--repo", r.dir, "--base", base, "--head", base, "--format", "yaml"}, "--format must be one of"},
		{"missing fixture", []string{"check", "--repo", r.dir, "--base", base, "--head", base, "--fixture", "nope.json"}, "not found in head commit"},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			code, stdout, stderr := runRaw(t, tc.args...)
			if code != app.ExitUsage {
				t.Fatalf("exit = %d, want %d\nstdout:\n%s\nstderr:\n%s", code, app.ExitUsage, stdout, stderr)
			}
			if !strings.Contains(stderr, tc.want) {
				t.Fatalf("stderr missing %q:\n%s", tc.want, stderr)
			}
			if stdout != "" {
				t.Fatalf("no report may be written on input errors, got:\n%s", stdout)
			}
		})
	}

	t.Run("malformed fixture", func(t *testing.T) {
		r2 := newTestRepo(t)
		r2.write(".rag-fixtures.json", "{")
		sha := r2.commit("broken fixture")
		code, _, stderr := runRaw(t, "check", "--repo", r2.dir, "--base", sha, "--head", sha)
		if code != app.ExitUsage || !strings.Contains(stderr, "invalid JSON") {
			t.Fatalf("exit/stderr = %d/%q, want 2 and invalid JSON", code, stderr)
		}
	})

	t.Run("invalid evidence path", func(t *testing.T) {
		r2 := newTestRepo(t)
		r2.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
			ID: "escape", Evidence: []fixture.Evidence{quote("../escape.md", "x")},
		}))
		sha := r2.commit("bad path")
		code, _, stderr := runRaw(t, "check", "--repo", r2.dir, "--base", sha, "--head", sha)
		if code != app.ExitUsage || !strings.Contains(stderr, "'..' segment") {
			t.Fatalf("exit/stderr = %d/%q, want 2 and a '..' complaint", code, stderr)
		}
	})

	t.Run("empty exact quote", func(t *testing.T) {
		r2 := newTestRepo(t)
		r2.write("docs/a.md", "Alpha.\n")
		r2.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
			ID: "empty", Evidence: []fixture.Evidence{quote("docs/a.md", "")},
		}))
		sha := r2.commit("empty quote")
		code, _, stderr := runRaw(t, "check", "--repo", r2.dir, "--base", sha, "--head", sha)
		if code != app.ExitUsage || !strings.Contains(stderr, "exact") {
			t.Fatalf("exit/stderr = %d/%q, want 2 and an exact complaint", code, stderr)
		}
	})

	t.Run("deleted head anchor blocks the run", func(t *testing.T) {
		r2 := newTestRepo(t)
		r2.write("docs/a.md", "Alpha.\n")
		r2.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
			ID: "a", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")},
		}))
		r2.commit("base")
		// head 把 fixture 的 path 改成绝对路径：无法解析的锚点必须阻断。
		r2.write(".rag-fixtures.json", fixtureJSON(t, fixture.Case{
			ID: "a", Evidence: []fixture.Evidence{quote("/etc/passwd", "Alpha.")},
		}))
		sha := r2.commit("broken anchor")
		code, _, stderr := runRaw(t, "check", "--repo", r2.dir, "--base", sha, "--head", sha)
		if code != app.ExitUsage || !strings.Contains(stderr, "repo-relative") {
			t.Fatalf("exit/stderr = %d/%q, want 2 and a repo-relative complaint", code, stderr)
		}
	})
}

// 浅克隆里缺 baseline 提交时，错误信息要能指向 fetch-depth。
func TestShallowRepositoryHint(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not available in PATH")
	}
	isolateGitEnv(t)

	origin := t.TempDir()
	runGit(t, origin, "init", "-b", "main")
	writeFile(t, origin, "docs/a.md", "Alpha.\n")
	writeFile(t, origin, ".rag-fixtures.json", fixtureJSON(t, fixture.Case{
		ID: "a", Evidence: []fixture.Evidence{quote("docs/a.md", "Alpha.")},
	}))
	runGit(t, origin, "add", "-A")
	runGit(t, origin, "commit", "--no-gpg-sign", "-m", "base")
	first := strings.TrimSpace(runGit(t, origin, "rev-parse", "HEAD"))
	writeFile(t, origin, "docs/a.md", "Alpha v2.\n")
	runGit(t, origin, "add", "-A")
	runGit(t, origin, "commit", "--no-gpg-sign", "-m", "head")

	cloneDir := t.TempDir()
	url := "file://" + ensureLeadingSlash(filepath.ToSlash(origin))
	if _, err := runGitAllowFail(cloneDir, "clone", "--depth", "1", url, "."); err != nil {
		t.Skipf("cannot create a shallow clone in this environment: %v", err)
	}

	code, _, stderr := runRaw(t, "check", "--repo", cloneDir, "--base", first, "--head", "HEAD")
	if code != app.ExitUsage {
		t.Fatalf("exit = %d, want %d (stderr: %s)", code, app.ExitUsage, stderr)
	}
	if !strings.Contains(stderr, "shallow") || !strings.Contains(stderr, "fetch-depth") {
		t.Fatalf("stderr must explain the shallow-clone workaround:\n%s", stderr)
	}
}

// runGit 在指定目录执行 git，失败即终止测试。
func runGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := runGitAllowFail(dir, args...)
	if err != nil {
		t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return out
}

// runGitAllowFail 在指定目录执行 git，返回输出与错误。
func runGitAllowFail(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// writeFile 写入绝对路径文件。
func writeFile(t *testing.T, root, path, content string) {
	t.Helper()
	full := filepath.Join(root, filepath.FromSlash(path))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatalf("mkdir for %q: %v", path, err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		t.Fatalf("write %q: %v", path, err)
	}
}

// ensureLeadingSlash 保证路径以 "/" 开头，用于拼 file:// URL。
func ensureLeadingSlash(p string) string {
	if strings.HasPrefix(p, "/") {
		return p
	}
	return "/" + p
}
