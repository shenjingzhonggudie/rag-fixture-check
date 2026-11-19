// Package check 编排一次完整的证据检查。
//
// 职责边界：读 head 的 fixture、可选地读 base 的 baseline、把每条 evidence
// 锚回 head tree，产出确定性的报告数据。它不做渲染（internal/report 负责），
// 也不解析 CLI 参数（internal/app 负责）。
package check

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/fixture"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/gitx"
	"github.com/shenjingzhonggudie/rag-fixture-check/internal/match"
)

// Change 描述单个 case 相对 baseline 的变化。
type Change string

// case 变化取值。
const (
	// ChangeUnchanged 表示 case 与 baseline 完全一致。
	ChangeUnchanged Change = "unchanged"

	// ChangeAdded 表示 baseline 里没有这个 id。
	ChangeAdded Change = "added"

	// ChangeModified 表示 id 相同但问题或证据被改过。
	ChangeModified Change = "modified"
)

// Options 是一次检查的输入。
type Options struct {
	Repo    string
	Base    string
	Head    string
	Fixture string
}

// Anchor 是单条证据的检查结果。
//
// Notes:
//
//	OldLine 是 baseline 中该引文的行号（0 表示无法确定），NewLine 是 head 中
//	的行号。NewPath 非空表示真实 Git 重命名把引用带到了另一个路径。
//	SelectorChanged 表示这条 evidence 的 path/exact/prefix/suffix 在 fixture
//	里被改过——那是作者已审阅的更新，不再按行号比较。
type Anchor struct {
	Index           int
	Path            string
	Status          match.Status
	OldPath         string
	NewPath         string
	OldLine         int
	NewLine         int
	Occurrences     int
	Selected        int
	SelectorChanged bool
	Detail          string
}

// Case 是一个评测题及其全部锚点结果。
type Case struct {
	ID       string
	Question string
	Change   Change
	Evidence []Anchor
}

// RemovedCase 描述一条从 fixture 中删掉的评测题。
//
// Notes:
//
//	删除的题没有 head 锚点可查，因此只汇报身份与原有证据路径，供 reviewer
//	确认删除是有意为之。
type RemovedCase struct {
	ID            string
	Question      string
	EvidenceCount int
	EvidencePaths []string
}

// Changes 汇总 fixture 层面的增删改。
type Changes struct {
	Added    []string
	Modified []string
	Removed  []string
}

// Summary 是可直接用于 CI 判定的计数。
type Summary struct {
	Cases          int
	Evidence       int
	Intact         int
	Relocated      int
	Missing        int
	Ambiguous      int
	ContextChanged int
	FileMissing    int
	Unreadable     int
	AddedCases     int
	ModifiedCases  int
	RemovedCases   int
	NeedsReview    bool
}

// Result 是一次检查的完整输出，也是 JSON 报告的数据来源。
type Result struct {
	Repo             string
	Base             string
	Head             string
	FixturePath      string
	BaselinePresent  bool
	BaselineReadable bool
	Cases            []Case
	RemovedCases     []RemovedCase
	Changes          Changes
	Summary          Summary
	Notes            []string
}

// ExitCode 把结果映射成 CLI 退出码。
//
// Returns:
//
//	0 表示全部锚点有效；1 表示至少一条锚点需要审阅。
//
// Notes:
//
//	输入与 Git 错误不会走到这里，它们在 Run 里直接返回 error（退出码 2）。
func (r *Result) ExitCode() int {
	if r.Summary.NeedsReview {
		return 1
	}
	return 0
}

// Run 执行检查并返回结果。
//
// Args:
//
//	opts: 仓库路径、base/head revision 与 fixture 路径。
//
// Returns:
//
//	检查结果；任何输入或 Git 错误都返回 error，调用方应映射为退出码 2。
//
// Notes:
//
//	head 的 fixture 必须存在且结构合法，否则直接报错——无法解析的 anchor 会
//	让"这条题还有没有效"变得不可判定，静默跳过等于掩盖问题。
//	baseline 的 fixture 是可选输入：缺失或损坏只记为一条 note，不影响 head
//	锚点的判定，但会让 added/modified 信息不完整。
func Run(opts Options) (*Result, error) {
	if strings.TrimSpace(opts.Repo) == "" {
		return nil, errors.New("--repo is required")
	}
	if opts.Fixture == "" {
		opts.Fixture = fixture.DefaultPath
	}
	if err := fixture.ValidatePath(opts.Fixture); err != nil {
		return nil, fmt.Errorf("--fixture: %w", err)
	}

	repo, err := gitx.Open(opts.Repo)
	if err != nil {
		return nil, err
	}
	base, err := repo.ResolveCommit(opts.Base)
	if err != nil {
		return nil, fmt.Errorf("--base: %w", err)
	}
	head, err := repo.ResolveCommit(opts.Head)
	if err != nil {
		return nil, fmt.Errorf("--head: %w", err)
	}

	res := &Result{
		Repo:         repo.Root,
		Base:         base,
		Head:         head,
		FixturePath:  opts.Fixture,
		Cases:        []Case{},
		RemovedCases: []RemovedCase{},
		Changes:      Changes{Added: []string{}, Modified: []string{}, Removed: []string{}},
		Notes:        []string{},
	}

	headDoc, err := loadFixture(repo, head, opts.Fixture, "head")
	if err != nil {
		return nil, err
	}
	baseDoc, note := loadBaseline(repo, base, opts.Fixture, res)
	if note != "" {
		res.Notes = append(res.Notes, note)
	}

	renames, rerr := repo.DetectRenames(base, head)
	if rerr != nil {
		res.Notes = append(res.Notes, "rename detection unavailable: "+rerr.Error())
		renames = nil
	}

	c := &checker{
		res:      res,
		renames:  renames,
		headDocs: newDocCache(repo, head),
		baseDocs: newDocCache(repo, base),
	}
	c.run(headDoc, baseDoc)
	summarize(res)

	if res.Summary.Evidence == 0 {
		res.Notes = append(res.Notes, "the head fixture contains no evidence anchors, so nothing could be verified")
	}
	if res.Summary.RemovedCases > 0 {
		res.Notes = append(res.Notes, "cases were removed from the fixture; confirm the deletion was intentional")
	}
	return res, nil
}

// loadFixture 读取并严格校验指定提交里的 fixture。
func loadFixture(repo *gitx.Repo, commit, path, label string) (*fixture.Document, error) {
	data, _, found, err := repo.ReadFile(commit, path, fixture.MaxDocumentBytes)
	if err != nil {
		return nil, fmt.Errorf("fixture at %s: %w", label, err)
	}
	if !found {
		return nil, fmt.Errorf("fixture %q not found in %s commit %s", path, label, commit)
	}
	doc, err := fixture.Parse(data)
	if err != nil {
		return nil, fmt.Errorf("fixture at %s commit %s: %w", label, commit, err)
	}
	if err := fixture.Validate(doc); err != nil {
		return nil, fmt.Errorf("fixture at %s commit %s: %w", label, commit, err)
	}
	return doc, nil
}

// loadBaseline 读取 baseline fixture，所有失败都降级成 note。
func loadBaseline(repo *gitx.Repo, commit, path string, res *Result) (*fixture.Document, string) {
	data, _, found, err := repo.ReadFile(commit, path, fixture.MaxDocumentBytes)
	if err != nil {
		return nil, "baseline fixture ignored: " + err.Error()
	}
	if !found {
		return nil, fmt.Sprintf("no baseline fixture at %s; every case is reported as added", path)
	}
	res.BaselinePresent = true
	doc, perr := fixture.Parse(data)
	if perr != nil {
		return nil, "baseline fixture ignored: " + perr.Error()
	}
	if verr := fixture.Validate(doc); verr != nil {
		return nil, "baseline fixture ignored: " + verr.Error()
	}
	res.BaselineReadable = true
	return doc, ""
}

// checker 持有一次检查所需的共享状态。
type checker struct {
	res      *Result
	renames  *gitx.Renames
	headDocs *docCache
	baseDocs *docCache
}

// run 遍历 head fixture，产出每条 case 与每个锚点的结果。
func (c *checker) run(headDoc, baseDoc *fixture.Document) {
	baseCases := make(map[string]*fixture.Case)
	if baseDoc != nil {
		baseCases = make(map[string]*fixture.Case, len(baseDoc.Cases))
		for i := range baseDoc.Cases {
			baseCases[baseDoc.Cases[i].ID] = &baseDoc.Cases[i]
		}
	}

	seen := make(map[string]bool, len(headDoc.Cases))
	for ci := range headDoc.Cases {
		hc := &headDoc.Cases[ci]
		seen[hc.ID] = true

		out := Case{ID: hc.ID, Question: hc.Question}
		bc, hasBase := baseCases[hc.ID]
		switch {
		case !hasBase:
			out.Change = ChangeAdded
			c.res.Changes.Added = append(c.res.Changes.Added, hc.ID)
		case !sameCase(bc, hc):
			out.Change = ChangeModified
			c.res.Changes.Modified = append(c.res.Changes.Modified, hc.ID)
		default:
			out.Change = ChangeUnchanged
		}

		out.Evidence = make([]Anchor, 0, len(hc.Evidence))
		for ei := range hc.Evidence {
			var baseEv *fixture.Evidence
			if hasBase && ei < len(bc.Evidence) {
				baseEv = &bc.Evidence[ei]
			}
			a := c.checkAnchor(hc.Evidence[ei], baseEv)
			a.Index = ei
			out.Evidence = append(out.Evidence, a)
		}
		c.res.Cases = append(c.res.Cases, out)
	}

	var removed []string
	for id := range baseCases {
		if !seen[id] {
			removed = append(removed, id)
		}
	}
	sort.Strings(removed)
	for _, id := range removed {
		bc := baseCases[id]
		rc := RemovedCase{
			ID:            bc.ID,
			Question:      bc.Question,
			EvidenceCount: len(bc.Evidence),
			EvidencePaths: make([]string, 0, len(bc.Evidence)),
		}
		for _, ev := range bc.Evidence {
			rc.EvidencePaths = append(rc.EvidencePaths, ev.Path)
		}
		c.res.RemovedCases = append(c.res.RemovedCases, rc)
		c.res.Changes.Removed = append(c.res.Changes.Removed, id)
	}
}

// checkAnchor 判定单条证据。
//
// Args:
//
//	ev: head fixture 中的证据。
//	baseEv: baseline 中同一序号的证据，可能为 nil。
//
// Returns:
//
//	填充好的 Anchor；Status 必定是 match 包定义的取值之一。
//
// Notes:
//
//	先判 head 侧；只有 head 侧为 Missing/FileMissing 时才尝试真实 Git 重命名
//	救援。救援只在引文于新路径唯一命中时成立，否则保持原状态并附说明。
func (c *checker) checkAnchor(ev fixture.Evidence, baseEv *fixture.Evidence) Anchor {
	a := Anchor{Path: ev.Path}
	anchor := match.NewAnchor(ev.Exact, ev.Prefix, ev.Suffix)

	var baseLine int
	if baseEv != nil {
		a.OldPath = baseEv.Path
		baseAnchor := match.NewAnchor(baseEv.Exact, baseEv.Prefix, baseEv.Suffix)
		if doc, found, err := c.baseDocs.get(baseEv.Path); err == nil && found {
			if bo := match.Evaluate(doc, baseAnchor); bo.Status == match.Intact {
				baseLine = bo.Line
				a.OldLine = bo.Line
			}
		}
	}

	doc, found, err := c.headDocs.get(ev.Path)
	switch {
	case err != nil:
		a.Status = match.Unreadable
		a.Detail = err.Error()
		return a
	case !found:
		a.Status = match.FileMissing
		a.Detail = "path is not present in the head commit"
		c.rescueRename(&a, anchor)
		return a
	}

	out := match.Evaluate(doc, anchor)
	a.Status = out.Status
	a.Occurrences = out.Occurrences
	a.Selected = out.Selected
	a.NewLine = out.Line
	a.Detail = out.Detail

	if out.Status == match.Missing {
		c.rescueRename(&a, anchor)
		return a
	}
	if out.Status == match.Intact && baseEv != nil {
		c.compareWithBaseline(&a, ev, baseEv, baseLine)
	}
	return a
}

// compareWithBaseline 在引文命中后，用 baseline 判断是否发生了位置迁移。
func (c *checker) compareWithBaseline(a *Anchor, ev fixture.Evidence, baseEv *fixture.Evidence, baseLine int) {
	quoteChanged := !fixture.EqualEvidence(*baseEv, ev)
	a.SelectorChanged = quoteChanged

	switch {
	case baseEv.Path != ev.Path:
		if target, ok := c.renames.Target(baseEv.Path); ok && target == ev.Path {
			a.Status = match.Relocated
			a.Detail = fmt.Sprintf("path updated from %s (git detected a rename)", baseEv.Path)
		} else {
			a.Detail = fmt.Sprintf("path changed from %s", baseEv.Path)
		}
	case quoteChanged:
		a.Detail = "selector updated in this change"
	case baseLine > 0 && baseLine != a.NewLine:
		a.Status = match.Relocated
		a.Detail = fmt.Sprintf("quote moved from line %d to line %d", baseLine, a.NewLine)
	}
}

// rescueRename 在 head 路径失败时尝试用真实 Git 重命名找回锚点。
//
// Args:
//
//	a: 正在填充的锚点，会被就地修改。
//	anchor: 已归一的选择器。
//
// Notes:
//
//	只有 base→head 的重命名映射唯一、且新路径里的引文唯一命中时才会写成
//	Relocated。映射缺失或新路径里同样找不到引文时保持原状态，仅补充说明。
func (c *checker) rescueRename(a *Anchor, anchor match.Anchor) {
	target, ok := c.renames.Target(a.Path)
	if !ok {
		return
	}
	doc, found, err := c.headDocs.get(target)
	if err != nil || !found {
		return
	}
	out := match.Evaluate(doc, anchor)
	switch out.Status {
	case match.Intact:
		a.Status = match.Relocated
		a.NewPath = target
		a.NewLine = out.Line
		a.Occurrences = out.Occurrences
		a.Selected = out.Selected
		a.Detail = fmt.Sprintf("file was renamed to %s (git detected a rename)", target)
	case match.Ambiguous, match.ContextChanged:
		a.Status = out.Status
		a.NewPath = target
		a.NewLine = out.Line
		a.Occurrences = out.Occurrences
		a.Selected = out.Selected
		a.Detail = fmt.Sprintf("in renamed file %s: %s", target, out.Detail)
	default:
		a.Detail = strings.TrimSpace(a.Detail + fmt.Sprintf("; file was renamed to %s but the quote is not there", target))
	}
}

// sameCase 判断两个 case 的问题与证据是否完全一致。
func sameCase(a, b *fixture.Case) bool {
	if a.Question != b.Question || len(a.Evidence) != len(b.Evidence) {
		return false
	}
	for i := range a.Evidence {
		if !fixture.EqualEvidence(a.Evidence[i], b.Evidence[i]) {
			return false
		}
	}
	return true
}

// summarize 汇总计数与审阅标记。
func summarize(res *Result) {
	s := &res.Summary
	for _, c := range res.Cases {
		s.Cases++
		for _, a := range c.Evidence {
			s.Evidence++
			switch a.Status {
			case match.Intact:
				s.Intact++
			case match.Relocated:
				s.Relocated++
			case match.Missing:
				s.Missing++
			case match.Ambiguous:
				s.Ambiguous++
			case match.ContextChanged:
				s.ContextChanged++
			case match.FileMissing:
				s.FileMissing++
			default:
				s.Unreadable++
			}
			if a.Status.Problem() {
				s.NeedsReview = true
			}
		}
	}
	s.AddedCases = len(res.Changes.Added)
	s.ModifiedCases = len(res.Changes.Modified)
	s.RemovedCases = len(res.Changes.Removed)
}

// docCache 按路径缓存同一提交内的文档解析结果。
//
// Notes:
//
//	同一文件常被多条 evidence 引用；缓存保证一次检查里每个文件只读一次。
//	错误与"不存在"同样缓存，避免对同一坏路径反复调用 Git。
type docCache struct {
	repo    *gitx.Repo
	commit  string
	docs    map[string]*match.Document
	bytes   int
	errs    map[string]error
	missing map[string]bool
}

// newDocCache 为某个提交建立空缓存。
func newDocCache(repo *gitx.Repo, commit string) *docCache {
	return &docCache{
		repo:    repo,
		commit:  commit,
		docs:    make(map[string]*match.Document),
		errs:    make(map[string]error),
		missing: make(map[string]bool),
	}
}

// get 返回路径对应的文档。
//
// Args:
//
//	path: 仓库相对路径。
//
// Returns:
//
//	doc 为解析好的文档；found 表示路径是否存在于该提交；err 表示读取失败。
//
// Notes:
//
//	found 为 true 且 err 非 nil 表示"路径存在但不可读"（符号链接、子模块、
//	超限），调用方应报 Unreadable 而不是 FileMissing。
func (c *docCache) get(path string) (*match.Document, bool, error) {
	if doc, ok := c.docs[path]; ok {
		return doc, true, nil
	}
	if err, ok := c.errs[path]; ok {
		return nil, true, err
	}
	if c.missing[path] {
		return nil, false, nil
	}
	raw, _, found, err := c.repo.ReadFile(c.commit, path, gitx.MaxBlobBytes)
	if err != nil {
		c.errs[path] = err
		return nil, true, err
	}
	if !found {
		c.missing[path] = true
		return nil, false, nil
	}
	doc := match.NewDocument(path, raw)
	// The normalized text plus its uint32 line index uses at most five
	// bytes per source byte, plus a single initial line offset.
	const cacheBudget = 64 << 20
	cost := 5*len(raw) + 4
	if c.bytes+cost > cacheBudget {
		clear(c.docs)
		c.bytes = 0
	}
	c.docs[path] = doc
	c.bytes += cost
	return doc, true, nil
}
