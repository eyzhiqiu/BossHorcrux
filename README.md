# page-centric-doc-book

`page-centric-doc-book` 是一个用于生成“页面中心化文档书”的 Codex Skill。  
它会读取一个后端仓库和一个前端仓库，抽取页面、接口、Go 调用链、数据库结构、字段映射等信息，并在独立文档仓库中生成可阅读的成书产物。

## Skill 作用

该 Skill 主要生成以下内容：

- 页面文档
- 接口文档
- 页面主流程 / 子流程
- 完整功能主线
- 数据字典
- `README.md` / `BOOK.md`
- 机读索引 `indexes/*.json`

## 目录说明

- Skill 定义文件：[`page-centric-doc-book/SKILL.md`](/page-centric-doc-book/SKILL.md)
- Python 实现包：[`page_centric_doc_book`](/page_centric_doc_book)
- 发布脚本：[`page-centric-doc-book`](/page-centric-doc-book)

注意：

- `page-centric-doc-book` 使用连字符 `-`
- `page_centric_doc_book` 使用下划线 `_`
- 两者是同一个 Skill 的不同路径层

## 安装方式

如果你要安装发布包版本：

1. 构建发布包

```bash
python release/page-centric-doc-book/build_release.py --repo-root "." --output-root "./release/dist"
```

2. 解压生成的 zip，进入发布目录

```bash
cd page-centric-doc-book
```

3. 安装到 Codex skills 目录

```bash
python install.py
```

如需指定安装路径或强制覆盖：

```bash
python install.py --target "C:/path/to/skills" --force --no-backup
```

## 使用前提

运行这个 Skill 前，需要满足这些条件：

1. 本机可执行 `codex`
2. 有一个后端仓库目录
3. 有一个前端仓库目录
4. 有一个独立的文档输出目录
5. 若要生成数据库字典，必须能访问真实 MySQL，且账号有目标 schema 权限

数据库部分当前是硬约束：

- 不再使用项目内 `sql/*.sql` 伪造数据库结构
- 只读取真实 MySQL `information_schema`
- 数据库不可达、权限不足、schema 无法解析时，流程会直接失败，不会跳过生成

## 输入参数

Skill 使用以下输入参数：

```json
{
  "backend_path": "C:/projects/backend",
  "frontend_path": "C:/projects/frontend",
  "output_path": "C:/projects/doc-repo",
  "resume": true
}
```

字段说明：

- `backend_path`：后端业务仓库路径
- `frontend_path`：前端业务仓库路径
- `output_path`：独立文档仓库路径
- `resume`：是否从已有 `progress.json` 继续执行

## 输出约束

这个 Skill 严格遵守以下边界：

- 只读扫描业务仓库
- 不修改 `backend_path`
- 不修改 `frontend_path`
- 所有生成物只写到 `output_path`
- `output_path` 不能位于前后端仓库内部

## 典型使用流程

1. 准备三个目录

- 一个后端仓库
- 一个前端仓库
- 一个独立文档仓库

2. 确保本机可执行 `codex`

3. 确保数据库连接可用

如果你要排查 Skill 实际使用的是哪组数据库环境变量，可以运行：

```bash
python -m skills.page_centric_doc_book.scripts.test_mysql_connection --table news.duanping_group_candidate
```

这个脚本会打印：

- 每个连接参数的候选环境变量
- 当前环境变量值
- 最终命中的变量来源
- 最终解析出的连接参数
- 当前连接数据库
- schema 解析结果
- 可选的单表探测结果

4. 运行 Skill

实际触发方式取决于你的 Codex 环境如何调用 Skill，但核心输入就是这四个参数：

- `backend_path`
- `frontend_path`
- `output_path`
- `resume`

5. 查看输出目录

生成结果通常包括：

- `volumes/`
- `dictionary/`
- `topics/`
- `knowledge/`
- `references/`
- `indexes/`
- `README.md`
- `BOOK.md`
- `progress.json`

## 断点续跑

当 `resume=true` 且 `output_path/progress.json` 存在时，Skill 会：

- 读取历史进度
- 跳过已完成任务
- 继续失败或未完成任务

如果源码关键输入发生变化，系统会保守地重建任务状态，避免复用旧结果。

## 数据库连接说明

当前数据库连接逻辑优先从环境变量解析连接参数。  
连接参数解析代码位于：

- [`database_schema_loader.py`](/page_centric_doc_book/scripts/database_schema_loader.py)

探测脚本位于：

- [`test_mysql_connection.py`](/page_centric_doc_book/scripts/test_mysql_connection.py)

### 需要配置哪些环境变量

数据库连接相关环境变量分两组：

1. Skill 专用变量
2. 兼容旧变量

当前解析优先级如下。

#### Host

- `PAGE_DOC_BOOK_DB_HOST`
- `MYSQL_HOST`
- `MYSQL_IP`
- `MYSQLIP`

#### Port

- `PAGE_DOC_BOOK_DB_PORT`
- `MYSQL_PORT`

默认值：

- `3306`

#### User

- `PAGE_DOC_BOOK_DB_USER`
- `MYSQL_USER`
- `MYSQL_USER_NAME`
- `MYSQLUSERNAME`

#### Password

- `PAGE_DOC_BOOK_DB_PASSWORD`
- `MYSQL_PASSWORD`
- `MYSQL_USER_PASS`
- `MYSQLUSERPASS`

#### Default Database

- `PAGE_DOC_BOOK_DB_NAME`
- `MYSQL_DATABASE`

#### Schema 列表

- `PAGE_DOC_BOOK_DB_SCHEMAS`
- `MYSQL_SCHEMAS`
- `MYSQL_SCHEMA`
- `MYSQL_DATABASE`
- `MYSQL_DB_NAME`

### 最少要配哪些

当前版本里，以下两项是硬要求：

- `host`
- `user`

也就是说，至少要能让程序解析出：

- `PAGE_DOC_BOOK_DB_HOST` 或兼容 Host 变量
- `PAGE_DOC_BOOK_DB_USER` 或兼容 User 变量

通常实际使用时，建议至少完整配置这几项：

- `PAGE_DOC_BOOK_DB_HOST`
- `PAGE_DOC_BOOK_DB_PORT`
- `PAGE_DOC_BOOK_DB_USER`
- `PAGE_DOC_BOOK_DB_PASSWORD`
- `PAGE_DOC_BOOK_DB_NAME`
- `PAGE_DOC_BOOK_DB_SCHEMAS`

### 推荐写法

推荐优先使用 Skill 专用变量，避免和其他项目冲突：

```bash
export PAGE_DOC_BOOK_DB_HOST="your-mysql-host"
export PAGE_DOC_BOOK_DB_PORT="3306"
export PAGE_DOC_BOOK_DB_USER="your-mysql-user"
export PAGE_DOC_BOOK_DB_PASSWORD='your-password'
export PAGE_DOC_BOOK_DB_NAME="news"
export PAGE_DOC_BOOK_DB_SCHEMAS="news"
```

如果你沿用旧变量名，也可以：

```bash
export MYSQL_IP="your-mysql-host"
export MYSQL_PORT="3306"
export MYSQL_USER_NAME="your-mysql-user"
export MYSQL_USER_PASS='your-password'
export MYSQL_DATABASE="news"
```

### `PAGE_DOC_BOOK_DB_NAME` 和 `PAGE_DOC_BOOK_DB_SCHEMAS` 的区别

- `PAGE_DOC_BOOK_DB_NAME`
  - 用于建立连接时指定默认数据库
- `PAGE_DOC_BOOK_DB_SCHEMAS`
  - 用于告诉 Skill 要读取哪些 schema

如果你的目标就是单库 `news`，最稳妥的写法是两者都设成 `news`。

如果你要读取多个 schema，可以这样写：

```bash
export PAGE_DOC_BOOK_DB_NAME="news"
export PAGE_DOC_BOOK_DB_SCHEMAS="news,news_archive"
```

常见连接问题：

- `1045 Access denied`
  - 用户名/密码错误
  - 当前机器/IP 未授权
  - 账号无目标 schema 权限

- `Can't connect`
  - 主机或端口不可达
  - 安全组、白名单、网络不通

- `Unknown database`
  - schema 名称错误

## 常见注意事项

1. 不要把 `output_path` 放到业务仓库内部
2. 不要指望项目内 SQL 文件生成数据库字典
3. 数据库连不上时，当前版本会直接失败，不会降级为空快照
4. 如果密码里包含 `$`，写环境变量时要注意转义或使用单引号

例如：

```bash
export MYSQL_USER_PASS='abc$Cj'
```

而不要写成：

```bash
export MYSQL_USER_PASS="abc$Cj"
```

否则 shell 可能把 `$Cj` 当成变量展开，导致密码被改写。

## 参考

- 发布说明：[`release/page-centric-doc-book/README.md`](/page-centric-doc-book/README.md)
- Skill 定义：[`skills/page-centric-doc-book/SKILL.md`](/page-centric-doc-book/SKILL.md)
