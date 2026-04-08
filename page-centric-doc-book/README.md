# page-centric-doc-book 发行包说明

以下说明基于“解压发布包后，在发布包根目录执行”的上下文，步骤中默认当前目录为 `page-centric-doc-book/`。

## 安装
1. 解压发布后的 `page-centric-doc-book-*.zip`，进入目录：`cd page-centric-doc-book`。
2. 运行 `python install.py`，脚本会将 `skills` 目录复制到默认的 `~/.codex/skills`，并生成 `page-centric-doc-book.manifest.json`。
3. 如需指定目录与覆盖策略，可执行：
   `python install.py --target "C:/path/to/skills" --force --no-backup`

## 升级
1. 再次运行 `python install.py` 会读取 manifest 里的版本号，默认只在检测到新版本时替换内容，重复安装会被跳过。
2. 需要降级或覆盖旧数据时，加上 `--force`；不需要留存旧版本才加 `--no-backup`。
3. 安装过程中会根据 manifest 的 `version` 字段比较已安装版本，升级前会先备份旧版本。
4. 默认安装到 `~/.codex/skills` 时，备份会写到同级目录 `~/.codex/skill-backups/`，避免被技能扫描器误识别。
5. 使用 `--target` 安装到自定义目录时，备份仍写到 `<target>/backups/` 以便就地恢复。

## 用法示例
1. 在源码仓库根目录执行：
   `python release/page-centric-doc-book/build_release.py --repo-root "." --output-root "./release/dist"`
   该命令会打包 `skills`、`manifest.json` 并生成 `page-centric-doc-book-{version}.zip`。
2. 将生成的 zip 解压，进入 `page-centric-doc-book/`，再运行 `python install.py` 进行安装。
3. 构建脚本会把 `release/page-centric-doc-book/manifest.json` 复制到发行目录，并将整个 `skills` 目录打包，zip 文件位于 `--output-root` 指定的目录中。

## 正文生成依赖
1. 页面、接口、完整功能主线、专题、知识卡片、引用文档的正文由本地 Codex 生成；运行环境必须能够直接执行 `codex "提示词"`。
2. 默认并发 10 个正文任务，单任务最多尝试 3 次；失败任务不会阻塞其他独立任务继续完成。
3. 如果本次运行仍有失败项，后续可通过 `resume=true` 继续补跑失败和未完成任务。

## 输出结构
- `volumes/`：按卷组织的正文目录，页面正文默认落在 `volumes/<volume>/pages/*.md`。
- `dictionary/`：字段字典目录，包含 `README.md`、`db-fields/`、`form-fields/`、`grid-columns/`、`tables/`、`models/` 等字典页。
- `topics/`：跨页面的专题链路文档，承载“完整一件事”的辅助阅读路径。
- `knowledge/`：可复用的知识卡片目录，用于放置批注式补充说明。
- `references/`：跨书引用与延伸阅读目录。
- `indexes/`：供 AI 读取的结构化索引目录，包含 `book_index.json`、`relations.json`、`navigation.json`。

## 参数说明
- `install.py --package-root`：发布包根目录，默认使用 `install.py` 所在目录。
- `install.py --target`：安装目标目录，默认 `~/.codex/skills`。
- `install.py --force`：强制覆盖或降级安装。
- `install.py --no-backup`：升级时不创建旧版本备份。
- `build_release.py --repo-root`：源码仓库根目录，默认根据脚本路径推导。
- `build_release.py --output-root`：发布产物输出目录（包含发布目录与 zip 文件）。
