---
name: page-centric-doc-book
description: Generate a page-centric documentation book from one backend repo and one frontend repo.
metadata:
  short-description: Generate page-centric docs from one backend and one frontend
---

# Page-Centric Doc Book Skill

本 Skill 的定义在 `skills/page-centric-doc-book/SKILL.md`，其 Python 实现包位于 `skills/page_centric_doc_book`（注意：一个用连字符 `-`，一个用下划线 `_`，避免路径分隔风格混用）。

## 运行契约与边界（必须遵守）

### 独立文档仓库
- `output_path` 指向独立文档仓库（doc repo）的工作目录：所有生成物、`progress.json`、成书产物都只写入这里。
- 文档仓库必须与业务仓库分离：`output_path` 不能位于 `backend_path` 或 `frontend_path` 之下，也不能与二者相同路径（违反将直接报错）。
- `run_pipeline()` 在主入口会先执行隔离检查，再执行 `output_root.mkdir(...)`；若冲突会立即报错并终止，不会开始生成流程。

### 不碰业务仓库
- `backend_path` 与 `frontend_path` 仅用于“读取发现/构建索引”，Skill 不会向业务仓库写入任何文件，不修改其 Git 状态，不做提交。
- 若你希望产物进入业务仓库，请在 Skill 之外手工同步；Skill 只保证在独立文档仓库内生成可发布内容。

### 自动续跑
- Skill 支持断点续跑：当 `resume=true` 且 `output_path/progress.json` 存在时，会加载进度并跳过已完成任务，继续未完成部分。
- 进度文件是运行快照而非“完美恢复点”：中断时处于 `running` 的任务会在下次加载时回退为可重试状态（避免卡死在运行中）。
- 若重新扫描后发现关键源码输入发生变化，Skill 会保守地清空已完成任务状态并全量重建，避免复用过期结果。
- 若存在最终失败任务，Skill 会先继续跑完其他独立任务，再统一汇总失败项；后续可使用 `resume=true` 只补跑失败和未完成部分。

### 正文生成依赖
- 页面、接口、完整功能主线、专题、知识卡片、引用文档的正文由本地 `codex` 命令生成，不再回退到占位正文。
- 运行环境必须能够直接执行 `codex "提示词"`；如果本地没有可用的 Codex CLI，正文任务会失败。
- 正文任务默认并发 `10` 执行，单任务最多尝试 `3` 次。

### 页面主线 + 完整功能主线
- 页面主线：以 `page.<page_id>` 为中心，产出页面说明、页面子流程、页面主流程（Mermaid 流程图）等文档。
- 完整功能主线：仅在检测到跨页跳转关系时生成 `feature.<feature_id>` 文档；如果没有足够的跨页证据，README/BOOK 会保留空状态而不是伪造端到端章节。

### 接口文档 / 流程图 / 成书
- 接口文档：为后端 API 生成文档（按模块组织）。
- 流程图：页面主流程、页面子流程、功能主线均以 Mermaid 形式渲染到 Markdown。
- 成书：生成 `README.md` 与 `BOOK.md` 作为目录与导航入口。

## 输入参数
- `backend_path:str`
- `frontend_path:str`
- `output_path:str`（独立文档仓库路径）
- `resume:bool`（是否从 `output_path/progress.json` 续跑）

## 执行顺序
1. 校验 `backend_path` 与 `frontend_path` 存在且为目录；校验 `output_path` 为独立文档仓库（不在业务仓库内）。
2. 对业务仓库做只读发现，构建“页面/接口/跨页跳转/告警”索引数据（不会写入业务仓库）。
3. 基于索引规划任务图（TaskRecords），包含：接口文档、页面文档、页面子流程、页面主流程、完整功能主线、成书等任务。
4. 根据 `resume` 决定是否加载 `output_path/progress.json`；若加载，则跳过已完成任务并恢复可重试任务状态。
5. 将当前进度序列化到 `output_path/progress.json` 作为续跑快照。
6. 为页面、接口、完整功能主线、专题、知识卡片、引用文档构造事实 JSON，并调用本地 `codex "提示词"` 生成正文；非正文类任务继续走模板输出。
7. 驱动成书器生成 `README.md` 与 `BOOK.md`，并保存最终进度快照到 `output_path/progress.json`。

## Example Input

```json
{
  "backend_path": "C:/projects/backend",
  "frontend_path": "C:/projects/frontend",
  "output_path": "C:/projects/doc-repo",
  "resume": true
}
```
