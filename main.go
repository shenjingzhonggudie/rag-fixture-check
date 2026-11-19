// Command rag-fixture-check 在文档 PR 合并前检查 RAG 评测题的证据锚点是否仍然成立。
//
// 程序只做一件事：读取 head 提交里的 fixture，把每条 evidence 的字面引文锚回
// head 提交的文件内容，并给出确定性的状态。它不判断引文是否还能支撑答案。
package main

import (
	"os"

	"github.com/shenjingzhonggudie/rag-fixture-check/internal/app"
)

// main 把进程参数与标准输出交给 internal/app，并用返回值作为退出码。
//
// Returns:
//
//	无返回值；退出码 0 表示全部锚点有效，1 表示需审阅，2 表示输入或 Git 错误。
//
// Notes:
//
//	退出码契约是 CLI 的一部分，不要在这里改写，否则 CI 判定会失真。
func main() {
	os.Exit(app.Run(os.Args[1:], os.Stdout, os.Stderr))
}
