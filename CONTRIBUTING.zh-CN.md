# 贡献指南

[English](CONTRIBUTING.md) | 简体中文

提交的改动应保持 BTAP 的核心行为：操作用户正在使用的真实浏览器会话，优先使用后台
页面/CDP 能力，只有在明确且确实必要时才使用前台物理输入。

## 开发环境

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"  # Windows PowerShell
python -m pip install -e ".[dev,desktop]"                       # 其他已激活的虚拟环境
browsertap extension-path
```

将命令输出的目录作为未打包扩展加载。editable 安装会立即读取 Python server 改动；
bridge 改动需要重启 bridge；扩展源码改动需要在浏览器扩展管理页手动重新加载。

## 测试

常规测试为离线测试，不会操作浏览器：

```text
python -m ruff check src tests scripts
python -m pytest tests -q
python -m pytest tests -q --cov=browsertap_mcp --cov-fail-under=85
python -m scripts.tool_coverage_report --format markdown
python -m scripts.check_tool_docs --format markdown
python -m scripts.versioning check
python -m build --wheel --sdist --outdir artifacts/dist
python -m scripts.check_distribution artifacts/dist
python -m scripts.check_install artifacts/dist --no-deps
```

这就是 `scripts/finalize_change.py` 与 `.github/workflows/test.yml` 使用的顺序；最后三条
必须连在一起执行：build 写出的归档正是 `check_distribution` 读取、`check_install` 安装的
那批，单独运行任一条只会报 `no wheel found`，不是通过。

`check_distribution` 与 `check_install` 回答的不是同一个问题。前者读归档**内部**有什么；
后者把 wheel 装进一个全新虚拟环境（路径上没有本仓库）并在那里真的用起来 —— 这是让
"陌生人 `pip install browsertap-mcp` 之后手里的东西能不能跑"从猜测变成结论的唯一办法。
本地跑的是 `--no-deps`：不访问索引，因此只证明**布局** —— 元数据版本、命令入口、随包
skills、扩展文件。CI 不带这个开关，会真的执行 `browsertap --version`、`skill-path`、
`extension-path`。报告里的 `mode` 与 `proves_cli` 会说明跑的是哪一种，所以只过了布局
的那次不会被当成"CLI 可用"。

`--cov-fail-under=85` 管的是**总体**，而总体就是一个均值，均值会把“某个模块已经没人
测了”盖过去。所以 `scripts/acceptance_report.py` 还会从已封存的 `artifacts/coverage.json`
里读每个文件的覆盖率，只要有一个文件低于 `PER_FILE_COVERAGE_FLOOR`，就判同一个
`code_coverage` 门禁失败 —— 这是 coverage.py 自己表达不了的东西。它是“腐坏探测器”
而不是指标：目前它就压在最弱那个模块下面，所以该做的是等那个模块改善后把它
调高，不是把它调低把红灯变绿。覆盖率文件里根本没有 per-file 那一段时也算失败，
不会因为没数据而算通过。

门禁规则集是 `ruff check`。`ruff format` 不是门禁，且现有源码大多不符合它的格式，
对只做局部修改的文件跑一遍会让无关的重排淹没本次改动。请按周围代码的既有风格书写。

live 测试必须显式运行：

```text
python -m pytest tests -q -m live
```

live 测试会操作已连接的真实浏览器，并可能暂时影响前台。只能在准备好的机器上运行，
并复用共享 scratch fixture，不要每个测试各开一个标签页。不得为 live 测试增加 headless 或
Playwright 回退路径，因为它们验证的是另一套产品契约。

有两个前置条件原本写在这里、靠人自己遵守：跑的时候不能有人在用那个浏览器；
标签页清单进去什么样、出来就要是什么样。还有第三条原本写在 agent 笔记里：桥守护进程
和扩展跑的必须就是当前这份代码。现在 `tests/conftest.py` 的 session fixture
会强制检查三者（判定逻辑在 `tests/live_preflight.py`）：

- 最先做的事：向 `get_setup_status()` 问清三个进程各自跑的是哪个构建。桥守护进程和
  扩展都是长活进程，起来时是哪个构建就一直是哪个构建，所以 live 跑绿了也可能证明的是
  仓库里并不存在的代码——而在这个检查之前，没有任何门禁、也没有任何封存产物记录过
  “回答的是哪个构建”。一旦版本错位，在采样浏览器之前就直接失败，并逐个点出哪个组件旧了、
  各自怎么修。这一条没有 override：重载扩展是点一下，重启桥是一条命令。
- 第一个 live 测试之前，相隔 1.5 秒取两次标签页快照。期间只要有标签页新开、
  关闭、跳转或切前台，就说明有人在用，此时直接 skip 整个 live 层，而不是对着一个
  不断变动的目标硬跑。skip 不等于通过：`scripts/acceptance_report.py` 只要看到有
  skipped 就会判 live 门禁失败。
- 最后一个测试之后，拿当时的基线再比一次。测试套件留下的标签页、关掉的标签页、
  或把用户正在看的页面弄跳转了，都会在 teardown 里失败。前台焦点变动不算：
  把标签页提到前台本身就是它要做的事。
- 每个判定、三个进程各自跑的构建，以及“当时浏览器到底空不空”，都会写进
  `artifacts/live-preflight.json`，由 `live.yml` 跟 junit 一起上传，证据 manifest 也会
  把它一起哈希绑定。这层绑定才能拦住“一边给出通过的套件、一边配一份更早那轮的前置
  记录”；live 封存时这个文件不存在，封存直接报错并点名它，而不是把碰巧存在的
  那半边封进去。

机器实在没有空闲的时候，可以设 `BTAP_LIVE_ALLOW_BUSY_BROWSER=1` 照跑：结束时的检查降为
警告，报告里会记下“这份证据是对着有人在用的浏览器跑出来的”。

公开的 `test.yml` 只在 GitHub 托管 runner 上运行离线门禁。`live.yml` 只能手动触发，
目标是预先配置的 Windows 自托管 runner。若 runner 的 `python` 不是指定解释器，应设置
仓库变量 `BTAP_LIVE_PYTHON`。不要把日常交互使用的桌面配置为定时运行 live 的 runner。
live job 仅允许规范仓库和 `btap-live` GitHub environment；注册 runner 前，应为该
environment 配置 required reviewer。workflow 文件本身无法创建或强制执行仓库的
environment 保护规则。

本地生成发布候选时，应在准备发布的同一棵源码树上运行完整离线管线：

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.evidence_manifest --check
python -m scripts.check_release_tag --allow-missing-tag
```

finalizer 会先把上一轮证据移动到带时间戳的 `artifacts/archive/`，再生成一套规范证据：
offline JUnit、覆盖率、工具证据，以及各一份 wheel/source archive。
`artifacts/evidence-manifest.json` 将这些文件与 Git HEAD、dirty 状态和公开源码树内容哈希
绑定。此后任何源码编辑或提交都会使证据过期；公开发布前必须在最终 commit 上重新运行
完整管线。

这条绑定里有两个性质决定报告是否可信：

- manifest 记录 `schema_version`。构成指纹的字段发生变化时，校验器按**版本**直接拒绝旧
  manifest，而不是报告内容不一致 —— 输入不同的指纹之间没有可比性。输出里的
  `re-seal with the current tooling` 就是这个含义：重新生成证据，不要去找那个"改动了"
  某个文件的编辑。
- 在 dirty 工作树上封存的证据会记录 `git_dirty: true`，验收报告把它直接算作证据问题。
  存在未提交或未跟踪文件时，仅凭 `git_head` 无法确定产出这批证据的代码，因此 dirty
  封存永远不可能达到 `release_ready`。先提交发布面，再封存。

这些产物只应由 finalizer 写入。手工把 `--junitxml`/`--cov` 指向 `artifacts/` 跑单个门禁，
会覆盖已封存证据中的一部分而留下其余部分，`--check` 随后会把它报成不一致。

## 工具契约变更

工具名称、参数、默认值或行为发生变化时，必须在同一个改动中同步：

1. `README.md` 与 `README.zh-CN.md` 中作为权威列表的 55 个工具说明；
2. 工具自身的 MCP `description=` 文本；
3. 调用方契约 `src/browsertap_mcp/skills/browsertap-default/SKILL.md`
   （先调哪个工具、什么时候必须带 `session_id`）；
4. `src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md`
   （桥本身连不上时调用方该怎么恢复）。

两份 skill 互相引用，因此各自按「一组副本」独立做哈希校验 —— 只更新其中一份，读者会被
指向已经不成立的说明。两份都不得写入本机专属路径或只对某台机器成立的断言；写了绝对路径
会被 `tests/test_documentation_contract.py` 拦下。

它们以 package data 形式随包发布，因此 `pip install browsertap-mcp` 就带着它们，
`browsertap skill-path` 会打印存放目录（形如 `<name>/SKILL.md`）。`MANIFEST.in`
的规则与 `pyproject.toml` 的 `package-data` 通配**两者都必需**：前者管 source archive，
后者管 wheel；只写一处会得到「sdist 里有、wheel 里没有」，而 `pip install` 用的正是 wheel。
`scripts/check_distribution.py` 要求两个归档里都有这两份文件，并拒绝归档中其他位置出现的
`SKILL.md`。

skill 管理器应**指向随包发布的那个目录**，不要复制文件。复制出来的副本在内容恰好一致期间
看不出问题，之后就静默收不到更新 —— 哈希校验就是为了抓这种漂移。如果确实保留了副本，在加
`--check-installed-skills` 的同时给出副本所在目录：

```bash
python -m scripts.check_tool_docs --check-installed-skills \
    --skill-mirror /path/to/installed/skills
# 或：BROWSERTAP_SKILL_MIRRORS="dir1:dir2" python -m scripts.check_tool_docs --check-installed-skills
```

每个目录下应有 `<skill-name>/SKILL.md`。agent 客户端把 skill 装在哪属于本机配置，
本仓库不记录这些路径；只加开关却不给目录会直接失败，不会静默通过。不加任何开关的默认门禁
校验四件事：随包发布的 skill、工具注册、文档里的参数与默认值，以及版本一致性 —— 也就是
没有已安装副本的贡献者能验证的全部内容。

## 版本与发布卫生

- Python 包、bridge 协议、扩展 manifest、两份 README 与 CHANGELOG 最新版本必须一致。
  提交改动前运行 `python -m scripts.versioning check`。用户可见改动先写入
  `[Unreleased]`；`python -m scripts.versioning bump|sync` 会在发布时生成新版本段并
  更新比较链接。
- 普通 pull request 不需要提升发布版本。CI 的版本增量门禁仅适用于 `release/*` 分支的
  push，使发布协调者统一管理共享版本与 CHANGELOG 文件，避免所有贡献者在这些文件上
  产生冲突。
- `python -m scripts.finalize_change` 会在本地跑同一条增量检查，基线取最近一个发布 tag，
  比较对象是**工作树**。CI 那条是拿一次 push 与它前一个提交比，仓库从未 push 过时它永远
  不触发；而提交范围比较在改动尚未提交时看到的是"什么都没改" —— 于是一整轮真实行为变更
  可能沿用起始版本号被封存。本地这条会直接拒绝，并打印它用的基线；仅在仓库还没有任何 tag
  时跳过。
- `python -m scripts.finalize_change` 会先同步目标版本，再针对该版本运行门禁。
  finalization 后不得修改版本；确需修改时，必须在准确的最终源码树上重新运行测试、
  覆盖率、文档、构建和发行检查。
- 不提交缓存、coverage/JUnit 生成物、日志、本地截图或构建输出。这些可再生或本机文件应
  由 `.gitignore` 排除。
- 旧版 `src/browsertap_mcp/chrome_extension/config.js`/TID 页面命令通道已删除。
  该文件不得进入 Git 或 Python 发行包，发行门禁会拒绝它。
- 不得包含 bridge token、Cookie、`.env` 文件、浏览器 profile 或复制的用户内容。
  `.github/workflows/supply-chain.yml` 每次 push 都会扫描工作树与完整 Git 历史，
  用的 gitleaks 同时锁定版本**和** sha256。公开仓库发布前，本地也跑同样两条：

  ```bash
  gitleaks git . --no-banner --redact
  gitleaks dir . --no-banner --redact
  ```

  事后真正起作用的是历史那一半：提交过又删掉的 secret 依然是公开的，只有改写历史能
  移除它，再补一个提交不行。`--redact` 保证扫描器不会把它发现的 secret 打进任何人都
  能读到的构建输出。
- 同一个 workflow 还会解析 `pip install` 实际拉进来的依赖闭包、对着漏洞库审计它，并把
  CycloneDX SBOM 作为构建产物发布。该审计在这里是提示性的，在 `release.yml` 里是阻断性
  的：一夜之间新增的公告不该让所有分支变红，但它确实是"这个版本先别发"的正当理由。
- wheel 与 source distribution 应作为 GitHub Release 资产上传。不要把它们、本地验收
  报告或 live 浏览器证据提交到 Git。

## 发布到 PyPI

本包尚未发布到 PyPI，因此 `pip install browsertap-mcp` 现在不可用，两份 README 也
如此写明；那句话只有在真正上传成功之后才改。

`.github/workflows/release.yml` 负责构建、门禁与上传。它**不会**被 push 触发，只能手动
运行或由已发布的 GitHub Release 触发。原因是上传不可撤销：PyPI 上的文件名永不可复用，
一次错误的上传会永久占掉那个版本号，只能改用下一个 patch 版本。

上传之前必须先具备三样东西，且都无法从本仓库内部创建：

1. 一个 PyPI 账号，且项目名 `browsertap-mcp` 可用或已归属自己。先查
   <https://pypi.org/project/browsertap-mcp/>；已被他人占用的名字无法接管。
2. PyPI 上为本仓库配置的 **Trusted Publisher**：仓库 `LinVireo/browsertap-mcp`、
   workflow `release.yml`、environment `pypi`。Trusted Publishing 的含义是 workflow 在
   请求时用短期 GitHub OIDC token 换取上传凭据，仓库里不存任何 API token —— 没有可泄露
   的东西，也不需要轮换。TestPyPI 上按同样方式再配一份，environment 用 `testpypi`。
3. GitHub 上名为 `pypi` 与 `testpypi` 的 environment。给 `pypi` 加上 required reviewer：
   environment 是人工确认这次不可逆上传的最后一道关口。

然后按顺序执行：

```bash
# 1. 先在本地证明这棵树和构建出的归档可发布。
python -m scripts.finalize_change --bump none
python -m scripts.evidence_manifest --check

# 2. 先在 TestPyPI 演练（Actions -> BTAP publish to PyPI -> index: testpypi），再装进一个
#    一次性虚拟环境验证。依赖仍从正式索引取，只有本包来自演练索引。
python -m pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ "browsertap-mcp[desktop]"

# 3. 发布该 tag 对应的 GitHub Release，完成正式上传。
```

workflow 会针对**即将上传的那批归档**重跑离线测试、文档检查、
`scripts.check_distribution`（必需文件、无本机数据，以及索引渲染与分类所需的元数据）
与 `twine check --strict`。手工构建过归档的话，本地也跑一次
`python -m twine check --strict dist/*`：只有它会按索引的方式渲染长描述，而且是最后一次
还免费的检查。

之后它会把刚构建出的 wheel 装进一个全新虚拟环境、在那里运行命令入口
（`scripts.check_install`），审计这个 wheel 会拉到用户机器上的依赖闭包，并把该闭包的
CycloneDX SBOM 作为**单独**产物写出 —— 单独是因为 publish 作业会把 `dist/` 下的所有东西
上传到索引。

tag 指向的提交若不是封存验收证据的那个提交，发布出去的就是没人验证过的东西。
`python -m scripts.check_release_tag` 用机械方式回答这件事：`v<源码版本>` 不存在、指向
别的提交（会同时报出两个 sha、相差几个提交、哪些生产文件不同），或者仍有未提交的生产
文件（任何 tag 都无法描述还没进提交的文件）时，它都会失败。`release.yml` 在安装和构建
任何东西之前先跑它。打完 tag、发布 Release 之前请自己也跑一次，并确认封存报告里的
`verified_at` 就是那个提交。

## Pull Request 检查表

- diff 只覆盖声明的行为，并保留无关的本地改动。
- 离线测试以及文档/版本检查通过。
- 新行为具有成功、边界与清理路径测试。
- 用户标签页不会被登记到 Agent-owned 清理集合。
- 后台操作不会激活标签页或移动光标。
- 公开文档已同步更新英文与中文版本。
- 不包含生成物或 secret。
