// Package fixture 定义 .rag-fixtures.json 的结构、解析与校验契约。
//
// 本包只回答"这份 fixture 结构是否合法"：不接触 Git，也不判断引文是否真的
// 存在于文档里。Git 读取由 internal/gitx 负责，锚点判定由 internal/check 负责。
package fixture

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"unicode/utf8"
)

// 版本与限额常量。
//
// Notes:
//
//	所有限额都是硬上限，超出即视为输入错误（退出码 2），不做静默截断。
//	截断会让"引文被改短"这类真实问题变得不可见，这是本工具明确拒绝的行为。
const (
	// Version 是当前支持的 fixture schema 版本。
	Version = 1

	// DefaultPath 是未显式指定 --fixture 时读取的仓库相对路径。
	DefaultPath = ".rag-fixtures.json"

	// MaxDocumentBytes 限制 fixture 文档大小。
	MaxDocumentBytes = 4 << 20

	// MaxExactBytes 限制单条引文长度。
	MaxExactBytes = 16 << 10

	// MaxSelectorBytes 限制单个 prefix/suffix 长度。
	MaxSelectorBytes = 4 << 10

	// MaxQuestionBytes 限制 question 长度。
	MaxQuestionBytes = 4 << 10

	// MaxPathBytes 限制 evidence 路径长度。
	MaxPathBytes = 1024

	// MaxIDBytes 限制 case id 长度。
	MaxIDBytes = 200

	// MaxCases 限制 case 数量。
	MaxCases = 5000

	// MaxEvidencePerCase 限制单个 case 的 evidence 数量。
	MaxEvidencePerCase = 200

	// MaxEvidenceTotal 限制整份 fixture 的 evidence 总数。
	MaxEvidenceTotal = 20000

	// MaxReportedProblems 限制一次性报告的校验问题条数。
	MaxReportedProblems = 20
)

// Document 对应 fixture 文件的顶层对象。
//
// Notes:
//
//	Version 必须等于 Version，缺失 version 字段会得到 0 并被拒绝。
//	未知字段会被拒绝（解析器使用 DisallowUnknownFields），这样拼写错误不会
//	被静默忽略成一个空引文。
type Document struct {
	Schema  string `json:"$schema,omitempty"`
	Version int    `json:"version"`
	Cases   []Case `json:"cases"`
}

// Case 是一条评测题及其证据集合。
type Case struct {
	ID       string     `json:"id"`
	Question string     `json:"question"`
	Evidence []Evidence `json:"evidence"`
}

// Evidence 是锚定在某个仓库文件里的字面引文。
//
// Notes:
//
//	Exact 是必须出现的字面文本；Prefix/Suffix 是紧邻 Exact 前后、可缺省的
//	字面文本，用来在同文件出现多处相同引文时消歧。
//	空字符串表示"这条选择器不约束该侧"。
type Evidence struct {
	Path   string `json:"path"`
	Exact  string `json:"exact"`
	Prefix string `json:"prefix,omitempty"`
	Suffix string `json:"suffix,omitempty"`
}

// ValidationError 汇总一次校验中发现的全部结构问题。
//
// Notes:
//
//	Error() 最多输出 MaxReportedProblems 条问题，剩余条数以计数形式提示，
//	避免一份坏文件刷屏。
type ValidationError struct {
	Problems []string
}

// Error 返回人类可读的问题清单。
//
// Returns:
//
//	以 "invalid fixture:" 开头的多行字符串，永不返回空串。
func (e *ValidationError) Error() string {
	var b strings.Builder
	b.WriteString("invalid fixture:")
	shown := e.Problems
	if len(shown) > MaxReportedProblems {
		shown = shown[:MaxReportedProblems]
	}
	for _, p := range shown {
		b.WriteString("\n  - ")
		b.WriteString(p)
	}
	if extra := len(e.Problems) - len(shown); extra > 0 {
		fmt.Fprintf(&b, "\n  - ... and %d more problem(s)", extra)
	}
	return b.String()
}

// Parse 把 JSON 字节解析成 Document。
//
// Args:
//
//	data: fixture 文件内容，调用方需保证已经过大小检查；本函数内部会再查一次。
//
// Returns:
//
//	解析成功的文档指针，或 nil 加错误（空文档、超限、非法 JSON、多余尾随数据）。
//
// Notes:
//
//	本函数只做语法解析，不做语义校验；调用方必须接着调用 Validate。
//	JSON 顶层必须是对象，尾随任何非空白内容都判为错误。
func Parse(data []byte) (*Document, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, errors.New("document is empty")
	}
	if len(data) > MaxDocumentBytes {
		return nil, fmt.Errorf("document is %d bytes, exceeds limit %d", len(data), MaxDocumentBytes)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var doc Document
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	var extra json.RawMessage
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return nil, errors.New("invalid JSON: unexpected data after the top-level object")
		}
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	return &doc, nil
}

// Validate 检查文档的版本、唯一性、必填字段与各项限额。
//
// Args:
//
//	doc: Parse 返回的文档；传 nil 会被判为错误。
//
// Returns:
//
//	全部合法返回 nil；否则返回 *ValidationError，Problems 至少含一条。
//
// Notes:
//
//	校验是"全量收集"而不是遇到第一处就返回，这样一次 PR 能把所有问题一起修掉。
//	evidence 为空数组是允许的（代表该题暂时没有可核查证据），会在报告里显示为
//	0 条锚点，但不属于结构错误。
func Validate(doc *Document) error {
	if doc == nil {
		return errors.New("document is null")
	}
	var problems []string
	add := func(format string, args ...any) {
		problems = append(problems, fmt.Sprintf(format, args...))
	}

	if doc.Version != Version {
		add("version: got %d, want %d", doc.Version, Version)
	}
	if doc.Cases == nil {
		add("cases: required array is missing")
	}
	if len(doc.Cases) > MaxCases {
		add("cases: %d entries exceed limit %d", len(doc.Cases), MaxCases)
	}

	seen := make(map[string]int, len(doc.Cases))
	total := 0
	for ci := range doc.Cases {
		c := &doc.Cases[ci]
		where := fmt.Sprintf("cases[%d]", ci)
		if c.ID == "" {
			add("%s.id: required and must be non-empty", where)
		} else {
			where = fmt.Sprintf("case %q", c.ID)
			if err := validateLabel(c.ID); err != nil {
				add("%s.id: %v", where, err)
			}
			if prev, dup := seen[c.ID]; dup {
				add("%s.id: duplicate of cases[%d]", where, prev)
			} else {
				seen[c.ID] = ci
			}
		}
		if len(c.Question) > MaxQuestionBytes {
			add("%s.question: %d bytes exceed limit %d", where, len(c.Question), MaxQuestionBytes)
		}
		if c.Evidence == nil {
			add("%s.evidence: required array is missing", where)
		}
		if len(c.Evidence) > MaxEvidencePerCase {
			add("%s.evidence: %d entries exceed limit %d", where, len(c.Evidence), MaxEvidencePerCase)
		}
		for ei := range c.Evidence {
			e := &c.Evidence[ei]
			ew := fmt.Sprintf("%s.evidence[%d]", where, ei)
			if err := ValidatePath(e.Path); err != nil {
				add("%s.path: %v", ew, err)
			}
			switch {
			case e.Exact == "":
				add("%s.exact: required and must be non-empty", ew)
			case len(e.Exact) > MaxExactBytes:
				add("%s.exact: %d bytes exceed limit %d", ew, len(e.Exact), MaxExactBytes)
			}
			if len(e.Prefix) > MaxSelectorBytes {
				add("%s.prefix: %d bytes exceed limit %d", ew, len(e.Prefix), MaxSelectorBytes)
			}
			if len(e.Suffix) > MaxSelectorBytes {
				add("%s.suffix: %d bytes exceed limit %d", ew, len(e.Suffix), MaxSelectorBytes)
			}
		}
		total += len(c.Evidence)
	}
	if total > MaxEvidenceTotal {
		add("evidence: %d anchors exceed limit %d", total, MaxEvidenceTotal)
	}
	if len(problems) == 0 {
		return nil
	}
	return &ValidationError{Problems: problems}
}

// ValidatePath 检查一个证据路径是否是可安全传给 Git 的仓库相对路径。
//
// Args:
//
//	p: 待检查路径，必须是使用正斜杠的仓库相对路径。
//
// Returns:
//
//	合法返回 nil，否则返回说明具体违规点的错误。
//
// Notes:
//
//	拒绝：空串、绝对路径、反斜杠、冒号（挡住 Windows 盘符与 URL 形式）、
//	控制字符、非法 UTF-8、空段（"a//b" 或末尾斜杠）、"." 与 ".." 段。
//	中文与空格是允许的；路径会以参数列表形式传给 Git，且前面永远有 "--"。
func ValidatePath(p string) error {
	switch {
	case p == "":
		return errors.New("path is empty")
	case len(p) > MaxPathBytes:
		return fmt.Errorf("path is %d bytes, exceeds limit %d", len(p), MaxPathBytes)
	case !utf8.ValidString(p):
		return errors.New("path is not valid UTF-8")
	case strings.HasPrefix(p, "/"):
		return fmt.Errorf("path must be repo-relative, got absolute path %q", p)
	case strings.ContainsRune(p, '\\'):
		return errors.New("path must use forward slashes")
	case strings.ContainsRune(p, ':'):
		return errors.New("path must not contain ':'")
	}
	for _, r := range p {
		if r < 0x20 || r == 0x7f {
			return errors.New("path contains control characters")
		}
	}
	for _, seg := range strings.Split(p, "/") {
		switch seg {
		case "":
			return errors.New("path has an empty segment (double slash or trailing slash)")
		case ".":
			return errors.New("path must not contain a '.' segment")
		case "..":
			return errors.New("path must not contain a '..' segment")
		}
	}
	return nil
}

// validateLabel 检查 case id 这类单行标签。
//
// Args:
//
//	s: 待检查的标签文本。
//
// Returns:
//
//	合法返回 nil，否则返回错误。
//
// Notes:
//
//	标签会进入报告与 GitHub 注解，因此不允许换行等控制字符。
func validateLabel(s string) error {
	switch {
	case len(s) > MaxIDBytes:
		return fmt.Errorf("%d bytes exceed limit %d", len(s), MaxIDBytes)
	case !utf8.ValidString(s):
		return errors.New("value is not valid UTF-8")
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7f {
			return errors.New("value contains control characters")
		}
	}
	return nil
}

// EqualEvidence 判断两条证据的所有字段是否完全一致。
//
// Args:
//
//	a: 第一条证据。
//	b: 第二条证据。
//
// Returns:
//
//	四个字段全部相同返回 true。
//
// Notes:
//
//	只做逐字节比较，不做换行归一；换行归一发生在匹配阶段。这样"fixture 里把
//	CRLF 改成 LF"会被记为一次选择器更新，而不是被悄悄当成没变。
func EqualEvidence(a, b Evidence) bool {
	return a == b
}
