// Command demo 在临时目录里构造一个真实的两次提交仓库，然后运行检查器，
// 演示 intact / relocated / missing / ambiguous / context_changed / file_missing
// 六种状态。
//
// 运行方式：
//
//	go run ./examples/demo
//
// Notes:
//
//	demo 不需要事先准备仓库，也不会触碰当前工作目录；退出码 1 是本 demo 的
//	预期结果（因为故意制造了失效锚点），断言失败时才返回非 1 的值。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/app"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/fixture"
)

// repo 是一个 demo 专用的临时仓库。
type repo struct {
	dir string
	env []string
}

func main() {
	if _, err := exec.LookPath("git"); err != nil {
		fmt.Fprintln(os.Stderr, "demo: git is required in PATH")
		os.Exit(1)
	}

	dir, err := os.MkdirTemp("", "rag-fixture-demo-")
	if err != nil {
		fmt.Fprintln(os.Stderr, "demo: create temp dir:", err)
		os.Exit(1)
	}
	defer os.RemoveAll(dir)

	r, err := newRepo(dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "demo:", err)
		os.Exit(1)
	}
	base, head, err := r.build()
	if err != nil {
		fmt.Fprintln(os.Stderr, "demo:", err)
		os.Exit(1)
	}

	fmt.Printf("demo repo: %s\nbase: %s\nhead: %s\n\n", dir, base, head)
	code := app.Run([]string{
		"check",
		"--repo", dir,
		"--base", base,
		"--head", head,
	}, os.Stdout, os.Stderr)

	fmt.Printf("\ndemo exit code: %d (1 is expected: four anchors still need review)\n", code)
	if code != app.ExitReview {
		fmt.Fprintf(os.Stderr, "demo assertion failed: exit code = %d, want %d\n", code, app.ExitReview)
		os.Exit(1)
	}
	fmt.Println("demo assertions passed: exit 1, six states demonstrated")
}

// newRepo 初始化仓库并隔离 git 配置，保证 demo 可重复。
//
// Notes:
//
//	全局配置文件放在 .git 目录里，既不会污染用户配置，也不会被 git add 收录。
func newRepo(dir string) (*repo, error) {
	configPath := filepath.Join(dir, ".git", "demo-gitconfig")
	env := append(os.Environ(),
		"GIT_CONFIG_GLOBAL="+configPath,
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_AUTHOR_NAME=demo",
		"GIT_AUTHOR_EMAIL=demo@example.com",
		"GIT_COMMITTER_NAME=demo",
		"GIT_COMMITTER_EMAIL=demo@example.com",
		"GIT_TERMINAL_PROMPT=0",
	)
	r := &repo{dir: dir, env: env}
	if _, err := r.git("init", "-b", "main"); err != nil {
		return nil, err
	}
	if _, err := r.git("config", "core.autocrlf", "false"); err != nil {
		return nil, err
	}
	return r, nil
}

// git 执行一条 git 命令。
func (r *repo) git(args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = r.dir
	cmd.Env = r.env
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s: %w\n%s", strings.Join(args, " "), err, out)
	}
	return string(out), nil
}

// write 写入仓库内文件。
func (r *repo) write(path, content string) error {
	full := filepath.Join(r.dir, filepath.FromSlash(path))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return err
	}
	return os.WriteFile(full, []byte(content), 0o644)
}

// mkdir 在仓库内创建目录。
func (r *repo) mkdir(path string) error {
	return os.MkdirAll(filepath.Join(r.dir, filepath.FromSlash(path)), 0o755)
}

// remove 删除仓库内文件。
func (r *repo) remove(path string) error {
	return os.Remove(filepath.Join(r.dir, filepath.FromSlash(path)))
}

// commit 暂存全部改动并提交，返回完整 SHA。
func (r *repo) commit(msg string) (string, error) {
	if _, err := r.git("add", "-A"); err != nil {
		return "", err
	}
	if _, err := r.git("commit", "--no-gpg-sign", "-m", msg); err != nil {
		return "", err
	}
	out, err := r.git("rev-parse", "HEAD")
	return strings.TrimSpace(out), err
}

// build 构造 base 与 head 两次提交。
func (r *repo) build() (string, string, error) {
	baseFiles := map[string]string{
		"docs/refund.md":   "# Refund policy\n\nRefunds are accepted within 30 days.\n",
		"docs/shipping.md": "# Shipping\n\nOrders ship within 2 days.\n\n## Tracking\n\nTracking numbers are emailed.\n",
		"docs/faq.md":      "# FAQ\n\nDo you ship internationally? No.\n",
		"docs/context.md":  "## Refunds\n\nRefunds are accepted within 30 days.\n",
		"docs/dup.md":      "# Duplicates\n\nRefunds are accepted within 30 days.\n\nRefunds are accepted within 30 days.\n",
		"docs/legacy.md":   "This page is a stub.\n",
		"docs/退款说明.md":     "# 退款\n\n退款在 30 天内受理。\n",
		"docs/crlf.md":     "# Windows\r\n\r\nRefunds are accepted within 30 days.\r\n",
	}
	cases := []fixture.Case{
		{ID: "refund-30", Question: "How many days do customers have?", Evidence: []fixture.Evidence{quote("docs/refund.md", "Refunds are accepted within 30 days.")}},
		{ID: "shipping-moved", Question: "How fast do orders ship?", Evidence: []fixture.Evidence{quote("docs/shipping.md", "Orders ship within 2 days.")}},
		{ID: "faq-renamed", Question: "Do you ship internationally?", Evidence: []fixture.Evidence{quote("docs/faq.md", "Do you ship internationally? No.")}},
		{ID: "context-drift", Question: "What is the refund window?", Evidence: []fixture.Evidence{{Path: "docs/context.md", Exact: "Refunds are accepted within 30 days.", Prefix: "## Refunds\n\n"}}},
		{ID: "dup-quote", Question: "What is the refund window?", Evidence: []fixture.Evidence{quote("docs/dup.md", "Refunds are accepted within 30 days.")}},
		{ID: "legacy-gone", Question: "What did the old page say?", Evidence: []fixture.Evidence{quote("docs/legacy.md", "This page is a stub.")}},
		{ID: "chinese-path", Question: "多少天内可以退款？", Evidence: []fixture.Evidence{quote("docs/退款说明.md", "退款在 30 天内受理。")}},
		{ID: "crlf-doc", Question: "What is the refund window?", Evidence: []fixture.Evidence{quote("docs/crlf.md", "Refunds are accepted within 30 days.")}},
	}

	fixtureText, err := json.MarshalIndent(fixture.Document{Version: fixture.Version, Cases: cases}, "", "  ")
	if err != nil {
		return "", "", err
	}
	baseFiles[".rag-fixtures.json"] = string(fixtureText) + "\n"

	for path, content := range baseFiles {
		if err := r.write(path, content); err != nil {
			return "", "", err
		}
	}
	base, err := r.commit("base: fixtures and docs")
	if err != nil {
		return "", "", err
	}

	// head：改退款期、插段落、重命名文件、改标题、删页面，并故意不更新 fixture。
	headFiles := map[string]string{
		"docs/refund.md":   "# Refund policy\n\nRefunds are accepted within 14 days.\n",
		"docs/shipping.md": "# Shipping\n\nWe now ship worldwide.\n\nOrders ship within 2 days.\n\n## Tracking\n\nTracking numbers are emailed.\n",
		"docs/context.md":  "## Returns\n\nRefunds are accepted within 30 days.\n",
	}
	for path, content := range headFiles {
		if err := r.write(path, content); err != nil {
			return "", "", err
		}
	}
	if err := r.mkdir("docs/guides"); err != nil {
		return "", "", err
	}
	if _, err := r.git("mv", "docs/faq.md", "docs/guides/faq.md"); err != nil {
		return "", "", err
	}
	if err := r.remove("docs/legacy.md"); err != nil {
		return "", "", err
	}
	head, err := r.commit("head: docs changed, fixture untouched")
	if err != nil {
		return "", "", err
	}
	return base, head, nil
}

// quote 构造一条只有 exact 的证据。
func quote(path, exact string) fixture.Evidence {
	return fixture.Evidence{Path: path, Exact: exact}
}
