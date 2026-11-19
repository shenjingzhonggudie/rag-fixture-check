// Package report 把检查结果渲染成 text/json/github 三种格式。
//
// 职责边界：只做展示，不改变判定。三种格式共享同一份 check.Result，因此
// 同一份输入在任何格式下给出的结论必须一致。
package report

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/check"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/match"
)

// SchemaVersion 是 JSON 报告的 schema 版本，与 fixture 版本相互独立。
const SchemaVersion = 1

// Disclaimer 说明工具的判定边界，会出现在每一种输出里。
const Disclaimer = "This tool verifies evidence anchoring only. Whether a quote still supports the expected answer is a human review decision."

// 展示层限额。
const (
	// maxQuestionRunes 限制 text 输出里的问题长度。
	maxQuestionRunes = 160

	// maxDetailRunes 限制单行细节长度。
	maxDetailRunes = 240

	// maxAnnotations 限制 github 格式的注解数量，避免触发平台限流。
	maxAnnotations = 50
)

// Text 输出适合终端阅读的报告。
//
// Args:
//
//	w: 目标写入器。
//	r: 检查结果。
//	version: CLI 版本号，打印在首行。
//
// Returns:
//
//	写入错误；判定本身不通过不算错误。
//
// Notes:
//
//	输出是纯 ASCII 状态标记加 UTF-8 文本，不含 ANSI 颜色，便于重定向到文件
//	或粘贴进 PR 评论。
func Text(w io.Writer, r *check.Result, version string) error {
	var b strings.Builder
	fmt.Fprintf(&b, "rag-fixture-check %s\n", version)
	fmt.Fprintf(&b, "repo:     %s\n", sanitize(r.Repo))
	fmt.Fprintf(&b, "base:     %s\n", r.Base)
	fmt.Fprintf(&b, "head:     %s\n", r.Head)
	fmt.Fprintf(&b, "fixture:  %s (read from head)\n", r.FixturePath)
	switch {
	case !r.BaselinePresent:
		b.WriteString("baseline: absent (every case counts as new)\n")
	case !r.BaselineReadable:
		b.WriteString("baseline: present but unusable (see notes)\n")
	default:
		b.WriteString("baseline: present (read from base)\n")
	}
	b.WriteString("\n")

	for _, c := range r.Cases {
		title := c.ID
		if c.Change != check.ChangeUnchanged {
			title += "  (" + string(c.Change) + ")"
		}
		fmt.Fprintf(&b, "CASE %s\n", sanitize(title))
		if c.Question != "" {
			fmt.Fprintf(&b, "  question: %s\n", truncate(sanitize(c.Question), maxQuestionRunes))
		}
		if len(c.Evidence) == 0 {
			b.WriteString("  (no evidence anchors)\n")
		}
		for _, a := range c.Evidence {
			fmt.Fprintf(&b, "  %-17s %s\n", "["+string(a.Status)+"]", describeAnchor(a))
		}
		b.WriteString("\n")
	}

	if len(r.RemovedCases) > 0 {
		b.WriteString("REMOVED CASES (confirm the deletion was intentional)\n")
		for _, rc := range r.RemovedCases {
			line := fmt.Sprintf("  %s  evidence=%d", sanitize(rc.ID), rc.EvidenceCount)
			if len(rc.EvidencePaths) > 0 {
				line += "  paths=" + sanitize(strings.Join(rc.EvidencePaths, ", "))
			}
			b.WriteString(truncate(line, maxDetailRunes+80) + "\n")
		}
		b.WriteString("\n")
	}

	s := r.Summary
	b.WriteString("SUMMARY\n")
	fmt.Fprintf(&b, "  cases=%d evidence=%d\n", s.Cases, s.Evidence)
	fmt.Fprintf(&b, "  intact=%d relocated=%d missing=%d ambiguous=%d context_changed=%d file_missing=%d unreadable=%d\n",
		s.Intact, s.Relocated, s.Missing, s.Ambiguous, s.ContextChanged, s.FileMissing, s.Unreadable)
	fmt.Fprintf(&b, "  fixture: added=%d modified=%d removed=%d\n", s.AddedCases, s.ModifiedCases, s.RemovedCases)
	if s.NeedsReview {
		fmt.Fprintf(&b, "VERDICT: review needed (%d anchor(s) are not intact)\n", problemCount(s))
	} else {
		b.WriteString("VERDICT: all evidence anchors are valid\n")
	}
	fmt.Fprintf(&b, "NOTE: %s\n", Disclaimer)
	if len(r.Notes) > 0 {
		b.WriteString("NOTES\n")
		for _, note := range r.Notes {
			fmt.Fprintf(&b, "  - %s\n", truncate(sanitize(note), maxDetailRunes))
		}
	}
	_, err := io.WriteString(w, b.String())
	return err
}

// JSON 输出带版本号的稳定 JSON 报告。
//
// Args:
//
//	w: 目标写入器。
//	r: 检查结果。
//	version: CLI 版本号。
//
// Returns:
//
//	编码或写入错误。
//
// Notes:
//
//	字段顺序由结构体固定，切片永远输出 [] 而不是 null，因此同样的输入会产生
//	逐字节相同的输出，可以直接做快照测试。关闭 HTML 转义以免 <,>,& 被改成
//	Unicode 转义序列，方便人读。
func JSON(w io.Writer, r *check.Result, version string) error {
	payload := buildJSON(r, version)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	return enc.Encode(payload)
}

// GitHub 输出 GitHub Actions 注解，可直接在 PR 的 Files changed 里显示。
//
// Args:
//
//	w: 目标写入器。
//	r: 检查结果。
//
// Returns:
//
//	写入错误。
//
// Notes:
//
//	只有 missing/ambiguous/context_changed/file_missing/unreadable 发 error，
//	relocated 与 fixture 增删改发 notice。消息里的 %, CR, LF 会被转义，避免
//	恶意或意外的文本注入额外的 workflow command。
func GitHub(w io.Writer, r *check.Result) error {
	var b strings.Builder
	emitted := 0
	truncated := false

	emit := func(line string) {
		if emitted >= maxAnnotations {
			truncated = true
			return
		}
		b.WriteString(line)
		b.WriteString("\n")
		emitted++
	}

	for _, c := range r.Cases {
		for _, a := range c.Evidence {
			title := "rag-fixture-check (" + string(a.Status) + ")"
			message := fmt.Sprintf("[%s] evidence %d: %s %s", c.ID, a.Index, describeAnchorPlain(a), a.Detail)
			file := a.Path
			if a.NewPath != "" {
				file = a.NewPath
			}
			switch a.Status {
			case match.Intact:
				continue
			case match.Relocated:
				emit(annotation("notice", file, a.NewLine, title, message))
			default:
				emit(annotation("error", file, a.NewLine, title, message))
			}
		}
	}

	for _, id := range r.Changes.Added {
		emit(annotation("notice", "", 0, "rag-fixture-check (fixture)", fmt.Sprintf("case %q was added to the fixture", id)))
	}
	for _, id := range r.Changes.Modified {
		emit(annotation("notice", "", 0, "rag-fixture-check (fixture)", fmt.Sprintf("case %q was modified in the fixture", id)))
	}
	for _, rc := range r.RemovedCases {
		emit(annotation("notice", "", 0, "rag-fixture-check (fixture)",
			fmt.Sprintf("case %q was removed from the fixture (had %d evidence items)", rc.ID, rc.EvidenceCount)))
	}

	if truncated {
		b.WriteString("::notice title=rag-fixture-check::additional annotations were suppressed; see the text or JSON report\n")
	}
	s := r.Summary
	summary := fmt.Sprintf("cases=%d evidence=%d intact=%d relocated=%d missing=%d ambiguous=%d context_changed=%d file_missing=%d unreadable=%d",
		s.Cases, s.Evidence, s.Intact, s.Relocated, s.Missing, s.Ambiguous, s.ContextChanged, s.FileMissing, s.Unreadable)
	if s.NeedsReview {
		fmt.Fprintf(&b, "::error title=rag-fixture-check::%d evidence anchor(s) need review; %s\n", problemCount(s), summary)
	} else {
		fmt.Fprintf(&b, "::notice title=rag-fixture-check::all evidence anchors are valid; %s\n", summary)
	}
	_, err := io.WriteString(w, b.String())
	return err
}

// jsonReport 是 JSON 输出的顶层结构。
type jsonReport struct {
	SchemaVersion  int           `json:"schemaVersion"`
	Tool           string        `json:"tool"`
	Version        string        `json:"version"`
	Repo           string        `json:"repo"`
	Base           string        `json:"base"`
	Head           string        `json:"head"`
	Fixture        string        `json:"fixture"`
	Baseline       jsonBaseline  `json:"baseline"`
	Summary        jsonSummary   `json:"summary"`
	FixtureChanges jsonChanges   `json:"fixtureChanges"`
	RemovedCases   []jsonRemoved `json:"removedCases"`
	Cases          []jsonCase    `json:"cases"`
	Notes          []string      `json:"notes"`
	Disclaimer     string        `json:"disclaimer"`
}

// jsonBaseline 描述 baseline fixture 的可用性。
type jsonBaseline struct {
	Present  bool `json:"present"`
	Readable bool `json:"readable"`
}

// jsonSummary 是汇总计数。
type jsonSummary struct {
	Cases          int  `json:"cases"`
	Evidence       int  `json:"evidence"`
	Intact         int  `json:"intact"`
	Relocated      int  `json:"relocated"`
	Missing        int  `json:"missing"`
	Ambiguous      int  `json:"ambiguous"`
	ContextChanged int  `json:"contextChanged"`
	FileMissing    int  `json:"fileMissing"`
	Unreadable     int  `json:"unreadable"`
	AddedCases     int  `json:"addedCases"`
	ModifiedCases  int  `json:"modifiedCases"`
	RemovedCases   int  `json:"removedCases"`
	NeedsReview    bool `json:"needsReview"`
}

// jsonChanges 是 fixture 层面的增删改清单。
type jsonChanges struct {
	Added    []string `json:"added"`
	Modified []string `json:"modified"`
	Removed  []string `json:"removed"`
}

// jsonRemoved 描述一条被删掉的评测题。
type jsonRemoved struct {
	ID            string   `json:"id"`
	Question      string   `json:"question"`
	EvidenceCount int      `json:"evidenceCount"`
	EvidencePaths []string `json:"evidencePaths"`
}

// jsonCase 是单个评测题的报告。
type jsonCase struct {
	ID       string       `json:"id"`
	Question string       `json:"question"`
	Change   string       `json:"change"`
	Evidence []jsonAnchor `json:"evidence"`
}

// jsonAnchor 是单条证据的报告。
//
// Notes:
//
//	所有字段恒定出现，不做 omitempty：消费者可以依赖字段存在性，缺失值用
//	零值表达（0 行号、空路径）。
type jsonAnchor struct {
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

// buildJSON 把内部结果转换成可序列化结构。
func buildJSON(r *check.Result, version string) jsonReport {
	out := jsonReport{
		SchemaVersion: SchemaVersion,
		Tool:          "rag-fixture-check",
		Version:       version,
		Repo:          r.Repo,
		Base:          r.Base,
		Head:          r.Head,
		Fixture:       r.FixturePath,
		Baseline:      jsonBaseline{Present: r.BaselinePresent, Readable: r.BaselineReadable},
		Summary: jsonSummary{
			Cases:          r.Summary.Cases,
			Evidence:       r.Summary.Evidence,
			Intact:         r.Summary.Intact,
			Relocated:      r.Summary.Relocated,
			Missing:        r.Summary.Missing,
			Ambiguous:      r.Summary.Ambiguous,
			ContextChanged: r.Summary.ContextChanged,
			FileMissing:    r.Summary.FileMissing,
			Unreadable:     r.Summary.Unreadable,
			AddedCases:     r.Summary.AddedCases,
			ModifiedCases:  r.Summary.ModifiedCases,
			RemovedCases:   r.Summary.RemovedCases,
			NeedsReview:    r.Summary.NeedsReview,
		},
		FixtureChanges: jsonChanges{
			Added:    nonNil(r.Changes.Added),
			Modified: nonNil(r.Changes.Modified),
			Removed:  nonNil(r.Changes.Removed),
		},
		RemovedCases: make([]jsonRemoved, 0, len(r.RemovedCases)),
		Cases:        make([]jsonCase, 0, len(r.Cases)),
		Notes:        nonNil(r.Notes),
		Disclaimer:   Disclaimer,
	}
	for _, rc := range r.RemovedCases {
		out.RemovedCases = append(out.RemovedCases, jsonRemoved{
			ID:            rc.ID,
			Question:      rc.Question,
			EvidenceCount: rc.EvidenceCount,
			EvidencePaths: nonNil(rc.EvidencePaths),
		})
	}
	for _, c := range r.Cases {
		jc := jsonCase{
			ID:       c.ID,
			Question: c.Question,
			Change:   string(c.Change),
			Evidence: make([]jsonAnchor, 0, len(c.Evidence)),
		}
		for _, a := range c.Evidence {
			jc.Evidence = append(jc.Evidence, jsonAnchor{
				Index:           a.Index,
				Path:            a.Path,
				Status:          string(a.Status),
				OldPath:         a.OldPath,
				NewPath:         a.NewPath,
				OldLine:         a.OldLine,
				NewLine:         a.NewLine,
				Occurrences:     a.Occurrences,
				Selected:        a.Selected,
				SelectorChanged: a.SelectorChanged,
				Detail:          a.Detail,
			})
		}
		out.Cases = append(out.Cases, jc)
	}
	return out
}

// annotation 组装一条 workflow command。
//
// Notes:
//
//	属性之间用逗号分隔，第一个属性用空格引导。当没有 file/line 时不会产生
//	前导逗号，避免生成 "::notice,title=..." 这种空属性形式。
func annotation(kind, file string, line int, title, message string) string {
	var b strings.Builder
	b.WriteString("::")
	b.WriteString(kind)
	sep := " "
	if file != "" {
		b.WriteString(sep)
		b.WriteString("file=")
		b.WriteString(escapeProperty(file))
		sep = ","
	}
	if line > 0 {
		b.WriteString(sep)
		fmt.Fprintf(&b, "line=%d", line)
		sep = ","
	}
	b.WriteString(sep)
	b.WriteString("title=")
	b.WriteString(escapeProperty(title))
	b.WriteString("::")
	b.WriteString(escapeData(message))
	return b.String()
}

// escapeData 转义 workflow command 的消息部分。
func escapeData(s string) string {
	s = strings.ReplaceAll(s, "%", "%25")
	s = strings.ReplaceAll(s, "\r", "%0D")
	s = strings.ReplaceAll(s, "\n", "%0A")
	return s
}

// escapeProperty 在 escapeData 基础上继续转义属性分隔符。
func escapeProperty(s string) string {
	s = escapeData(s)
	s = strings.ReplaceAll(s, ":", "%3A")
	s = strings.ReplaceAll(s, ",", "%2C")
	return s
}

// nonNil 把 nil 切片换成空切片，保证 JSON 里是 [] 而不是 null。
func nonNil(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

// describeAnchor 生成 text 格式的单行锚点说明。
func describeAnchor(a check.Anchor) string {
	parts := []string{anchorLocation(a)}
	if a.OldPath != "" && a.OldPath != a.Path {
		parts = append(parts, "was "+a.OldPath)
	}
	parts = append(parts, fmt.Sprintf("matches=%d selected=%d", a.Occurrences, a.Selected))
	if a.Detail != "" {
		parts = append(parts, a.Detail)
	}
	return truncate(sanitize(strings.Join(parts, "  ")), maxDetailRunes)
}

// describeAnchorPlain 生成 github/短消息用的锚点位置描述。
func describeAnchorPlain(a check.Anchor) string {
	return truncate(sanitize(anchorLocation(a)), maxDetailRunes)
}

// anchorLocation 组合 旧路径:旧行 -> 新路径:新行 的展示形式。
//
// Notes:
//
//	同一路径内移动时显示 "path:新行 (was line 旧行)"，跨路径移动时显示
//	"旧路径:旧行 -> 新路径:新行"，让 reviewer 一眼看到行号变化。
func anchorLocation(a check.Anchor) string {
	if a.NewPath != "" {
		old := a.Path
		if a.OldLine > 0 {
			old = fmt.Sprintf("%s:%d", a.Path, a.OldLine)
		}
		target := a.NewPath
		if a.NewLine > 0 {
			target = fmt.Sprintf("%s:%d", a.NewPath, a.NewLine)
		}
		return old + " -> " + target
	}
	if a.NewLine > 0 {
		loc := fmt.Sprintf("%s:%d", a.Path, a.NewLine)
		if a.OldLine > 0 && a.OldLine != a.NewLine {
			loc += fmt.Sprintf(" (was line %d)", a.OldLine)
		}
		return loc
	}
	if a.OldLine > 0 {
		return fmt.Sprintf("%s (last seen at line %d)", a.Path, a.OldLine)
	}
	return a.Path
}

// problemCount 统计需要审阅的锚点数。
func problemCount(s check.Summary) int {
	return s.Missing + s.Ambiguous + s.ContextChanged + s.FileMissing + s.Unreadable
}

// sanitize 去掉控制字符，把换行与制表符折成空格。
//
// Notes:
//
//	报告里会出现来自 fixture 的 id 与 question，它们可能带控制字符；终端控制
//	序列与 GitHub workflow command 都依赖换行，必须在渲染前清掉。
func sanitize(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch {
		case r == '\n' || r == '\t' || r == '\r':
			b.WriteRune(' ')
		case r < 0x20 || r == 0x7f:
			continue
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// truncate 按 rune 截断过长文本。
func truncate(s string, limit int) string {
	if limit <= 0 {
		return ""
	}
	count := 0
	for i := range s {
		if count == limit {
			return s[:i] + "..."
		}
		count++
	}
	return s
}
