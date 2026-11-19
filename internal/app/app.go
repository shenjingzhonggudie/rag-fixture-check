// Package app 负责 CLI 参数解析、错误映射与输出分发。
//
// 职责边界：把 os.Args 变成 check.Options，调用 internal/check，再交给
// internal/report 渲染。它不实现匹配逻辑，也不直接调用 git。
package app

import (
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/check"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/fixture"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/gitx"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/report"
)

// ToolName 是命令名，出现在所有面向用户的文本里。
const ToolName = "rag-fixture-check"

// Version 是发布版本号；与 v0.2 的 Python 导入器保持同一条版本线。
const Version = "0.2.0"

// 退出码契约。
const (
	// ExitOK 表示所有证据锚点有效。
	ExitOK = 0

	// ExitReview 表示至少一条锚点需要审阅，或 fixture 里出现无法锚定的内容。
	ExitReview = 1

	// ExitUsage 表示输入错误或 Git 调用失败。
	ExitUsage = 2
)

// usage 是完整的帮助文本。
const usage = `rag-fixture-check - verify RAG fixture evidence anchors before a docs PR merges

Usage:
  rag-fixture-check check --repo <path> --base <commit-ish> --head <commit-ish> [flags]
  rag-fixture-check version
  rag-fixture-check help

Flags:
  --repo <path>          repository to inspect; a nested directory is fine (required)
  --base <commit-ish>    baseline revision, resolved to a full commit SHA (required)
  --head <commit-ish>    revision under review, resolved to a full commit SHA (required)
  --fixture <path>       repo-relative fixture path (default ".rag-fixtures.json")
  --format <fmt>         output format: text, json, github (default "text")
  --version              print the version and exit
  --help                 print this help and exit

Exit codes:
  0  every evidence anchor is intact or relocated
  1  at least one anchor needs review (missing, ambiguous, context_changed,
     file_missing, unreadable)
  2  invalid input or a Git failure

Notes:
  Results are computed from committed trees only; uncommitted worktree changes
  are ignored. Only literal text matching is performed. Whether a quote still
  supports the expected answer is a human review decision.
`

// Run 执行一次 CLI 调用并返回退出码。
//
// Args:
//
//	args: 不含程序名的参数列表，例如 ["check", "--repo", ".", ...]。
//	stdout: 正常报告的输出目标。
//	stderr: 错误与用法提示的输出目标。
//
// Returns:
//
//	0/1/2 三者之一，语义见 ExitOK、ExitReview、ExitUsage。
//
// Notes:
//
//	参数错误不会打印堆栈，也不会泄露仓库内容；只输出一行原因加一句提示。
//	任何 panic 都不应该由调用方捕获，本函数只返回可预期的错误。
func Run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return ExitUsage
	}
	switch args[0] {
	case "check":
		return runCheck(args[1:], stdout, stderr)
	case "help", "-h", "--help":
		fmt.Fprint(stdout, usage)
		return ExitOK
	case "version", "-version", "--version":
		fmt.Fprintf(stdout, "%s %s\n", ToolName, Version)
		return ExitOK
	default:
		fmt.Fprintf(stderr, "%s: unknown command %q\n\n%s", ToolName, args[0], usage)
		return ExitUsage
	}
}

// options 是解析后的 check 子命令参数。
type options struct {
	repo    string
	base    string
	head    string
	fixture string
	format  string
	help    bool
	version bool
}

// runCheck 执行 check 子命令。
func runCheck(args []string, stdout, stderr io.Writer) int {
	opts, err := parseCheckArgs(args)
	if err != nil {
		fmt.Fprintf(stderr, "%s: %v\n\nrun '%s help' for usage\n", ToolName, err, ToolName)
		return ExitUsage
	}
	if opts.help {
		fmt.Fprint(stdout, usage)
		return ExitOK
	}
	if opts.version {
		fmt.Fprintf(stdout, "%s %s\n", ToolName, Version)
		return ExitOK
	}
	if err := validateOptions(opts); err != nil {
		fmt.Fprintf(stderr, "%s: %v\n\nrun '%s help' for usage\n", ToolName, err, ToolName)
		return ExitUsage
	}

	result, err := check.Run(check.Options{
		Repo:    opts.repo,
		Base:    opts.base,
		Head:    opts.head,
		Fixture: opts.fixture,
	})
	if err != nil {
		fmt.Fprintf(stderr, "%s: error: %v\n", ToolName, err)
		if errors.Is(err, gitx.ErrNotRepository) {
			fmt.Fprintf(stderr, "hint: pass --repo pointing at a Git working tree, for example --repo .\n")
		}
		return ExitUsage
	}

	switch opts.format {
	case "text":
		err = report.Text(stdout, result, Version)
	case "json":
		err = report.JSON(stdout, result, Version)
	case "github":
		err = report.GitHub(stdout, result)
	}
	if err != nil {
		fmt.Fprintf(stderr, "%s: error: writing report: %v\n", ToolName, err)
		return ExitUsage
	}
	return result.ExitCode()
}

// parseCheckArgs 解析 check 子命令的参数。
//
// Args:
//
//	args: check 之后的全部参数。
//
// Returns:
//
//	解析结果；遇到未知参数、缺值或空值返回错误。
//
// Notes:
//
//	同时接受 "--flag value" 与 "--flag=value"。只识别白名单里的参数名，
//	因此 "--upload-pack=..." 这类注入尝试会以"未知参数"被拒绝。
func parseCheckArgs(args []string) (*options, error) {
	opts := &options{fixture: fixture.DefaultPath, format: "text"}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !strings.HasPrefix(arg, "-") {
			return nil, fmt.Errorf("unexpected argument %q", arg)
		}
		name, value, hasValue := strings.Cut(arg, "=")
		name = strings.TrimLeft(name, "-")
		switch name {
		case "repo", "base", "head", "fixture", "format":
			if !hasValue {
				if i+1 >= len(args) {
					return nil, fmt.Errorf("flag --%s requires a value", name)
				}
				i++
				value = args[i]
			}
			if value == "" {
				return nil, fmt.Errorf("flag --%s requires a non-empty value", name)
			}
			switch name {
			case "repo":
				opts.repo = value
			case "base":
				opts.base = value
			case "head":
				opts.head = value
			case "fixture":
				opts.fixture = value
			case "format":
				opts.format = value
			}
		case "help", "h":
			opts.help = true
		case "version":
			opts.version = true
		case "":
			return nil, fmt.Errorf("unexpected argument %q", arg)
		default:
			return nil, fmt.Errorf("unknown flag %q", arg)
		}
	}
	return opts, nil
}

// validateOptions 检查必填项与取值范围。
func validateOptions(opts *options) error {
	if opts.repo == "" {
		return errors.New("--repo is required")
	}
	if opts.base == "" {
		return errors.New("--base is required")
	}
	if opts.head == "" {
		return errors.New("--head is required")
	}
	if err := gitx.ValidateRevision(opts.base); err != nil {
		return fmt.Errorf("--base: %w", err)
	}
	if err := gitx.ValidateRevision(opts.head); err != nil {
		return fmt.Errorf("--head: %w", err)
	}
	if err := fixture.ValidatePath(opts.fixture); err != nil {
		return fmt.Errorf("--fixture: %w", err)
	}
	switch opts.format {
	case "text", "json", "github":
		return nil
	default:
		return fmt.Errorf("--format must be one of text, json, github (got %q)", opts.format)
	}
}
