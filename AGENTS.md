# AGENTS.md — AstrBot 插件项目工作说明书

> 本文件是任何 AI 编码代理（agent）进入本项目时的**必读**工作说明书。
> 它约定本插件的架构、代码规范、文档维护规则。任何对代码、配置、行为的改动，都必须同时遵守本文档的规则。
>
> 项目：`astrbot_plugin_baidu_netdisk`（百度网盘下载）
>
> **来源**：本文件由 [agent-templates](https://github.com/XTsat/agent-templates) 的 `templates/astrbot-plugin/AGENTS.md` 派生，并按本项目实际情况回填；最新版见该仓库 `main` 分支。
> 用户说「更新 AGENTS」时按此流程：① 拉取来源模板最新版 → ② 与本地逐节比对 → ③ 合并（保留本项目已回填的插件名、命令前缀、目录结构与项目特有约定）→ ④ 展示 diff 与改动清单，等用户确认后再落盘。**禁止整文件覆盖**。

---

## 1. 项目概览

**AstrBot 插件**是基于 `astrbot.api.star` 框架的 Python 插件，以 `Star` 子类为核心，通过装饰器注册指令组、事件监听器、过滤器，在 AstrBot 运行时中加载。

本插件为百度网盘分享下载插件：一个百度账号登录后全群共享高速下载能力，支持扫码 / 账密 / BDUSS / Cookie 登录，支持目录浏览、按需转存与下载、下载后自动发送，并注册 LLM 工具供 AI 调用。

核心文件清单：

| 文件 | 必选 | 职责 |
|------|------|------|
| `main.py` | ✅ | 插件主体，全部后端逻辑 |
| `metadata.yaml` | ✅ | 插件元数据（name、version、author、desc、repo、support_platforms） |
| `_conf_schema.json` | ✅ | WebUI 配置 Schema（定义所有可配置项的类型、默认值、枚举） |
| `README.md` | ✅ | 中文文档（功能说明、命令表、配置说明、常见问题） |
| `CHANGELOG.md` | ✅ | 变更日志 |
| `LICENSE` | ✅ | 许可证（AGPL-3.0） |
| `logo.png` | ✅ | 插件图标（256×256） |
| `requirements.txt` | 可选 | 运行时依赖（本项目为 `requests>=2.28`） |
| `.gitignore` | 可选 | Git 忽略规则 |

---

## 2. 目录结构与架构

### 2.1 项目结构

插件以目录形式存放于 AstrBot 的 `addons/` 目录下，目录名即插件名。

```
astrbot_plugin_baidu_netdisk/
├── main.py                 # 全部后端逻辑（本项目为单文件风格）
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # 插件配置 Schema
├── CHANGELOG.md            # 变更日志
├── README.md               # 中文文档
├── LICENSE                 # 许可证
├── logo.png                # 插件图标
├── requirements.txt        # 运行时依赖
├── AGENTS.md               # 本文件
└── .gitignore
```

### 2.2 架构倾向

AstrBot 插件有两种主流架构风格，**新建项目时二选一，并在整个生命周期内保持一致**：

| 风格 | 适用场景 | 参考项目 |
|---|---|---|
| **单文件** | 逻辑简单（< 1500 行） | `astrbot_plugin_msg_forward_cc`、`astrbot_plugin_model_status` |
| **分层多文件** | 逻辑复杂，需模块化 | `astrbot_plugin_help_panel_typst`（main.py → core → utils → domain） |

**本项目采用单文件风格**（`main.py`，约 3800 行，已远超模板建议的 1500 行阈值）。**单文件风格绝不等于允许重复实现**：同一件事只允许有一处实现，见下方「单文件风格」第 3 条。

**单文件风格**：
- 所有逻辑在 `main.py` 一个文件中，通过注释分隔条组织：`# 内部工具方法 → # Web API → # 数据持久化 → # 执行与调度 → # 聊天命令 → # 生命周期`
- 新增方法按职责放入对应分区；当前 `main.py` 尚未全面铺设 `# ---- 分区名 ----` 分隔条，新增分区时补上
- **同一逻辑只能有一处实现（强制）**：跨入口复用的校验、文案、发送链路、文件读写、路径解析必须抽成模块级 helper 或类内 `_x` 方法，由各调用点复用。典型必须抽取的场景：
  - 同一段用户文案出现在 ≥2 个入口 → 抽成模块级 `_MSG_*` 常量（命令路径自行加 `❌` 前缀，常量本身不带）
  - 多个 LLM 工具/命令分支的「受理前奏」（黑名单 → 解析链接 → 取缓存 → 校验）→ 抽成一个受理 helper
  - 多个「执行 → 成功/失败回应」骨架 → 抽成接收动作回调的 helper，成功文案由调用方传入
  - 同一份单位表 / 正则 / host 白名单 → 抽成单一常量，禁止在函数内重抄一份
- **不合并「形状相似但语义不同」的代码**：例如平台判定（`_send_file` 平台优先 vs `_send_folder` 配置优先）、互补的多层校验、故意保留的双实现路径（API 优先 + 页面兜底）——合并会改变行为。判断标准是「合并后行为是否逐字等价」，不等价就保留并在注释里写明差异理由
- **多条实现路径共享的部分要抽出来，外层差异保留**：如「命令路径 yield / 后台任务 send」两种发射方式，只抽中间的解析与分类逻辑，不强行统一发射方式
- 新增 helper 必须写清 non-obvious reason（见 7.3），尤其是「为什么这里不能和某处合并」

**分层多文件风格**（本项目当前不适用，保留规范供拆分后参考）：
- 依赖方向严格单向：`main.py → core → utils / domain`；`utils → domain`；`domain` 不依赖任何人
- `domain/`：数据定义层（constants、config、schemas），最底层，无内部依赖
- `utils/`：通用工具层（font、image、hash 等），禁止 import core/ 或 main
- `core/`：核心业务层（analyzer、renderer、worker），纯 Python 逻辑
- 新增常量 → 放 `domain/constants.py`，禁止散落硬编码
- 新增配置项 → 改 `domain/config.py` 的 dataclass 字段 + 兜底逻辑 + `_conf_schema.json`

### 2.3 本项目特有约定与偏差

> 以下是本项目相对来源模板的**已知偏差**，改文档时不得按模板默认值「纠偏」回去。

1. **单文件风格**：全部逻辑在 `main.py`，不拆分模块（见 2.2）。但「单文件」只约束**文件数量**，不豁免 2.2 的「同一逻辑只能有一处实现」：跨入口共享的文案、校验、发送链路、读写事务必须抽成 helper 复用，禁止复制粘贴出第二份。用户已明确「先把重复逻辑合并」，拆分文件（`core/` + `utils/`）仍待用户单独确认。
2. **仅维护中文 README**：本项目**只有 `README.md`，未创建 `README_en.md`**（用户已确认）。7.2 中关于英文版的要求在创建英文版之前不适用；新增文档章节时只落中文版，不要凭空新建英文文件。
3. **外部依赖 BaiduPCS-Go**：插件不内置该二进制，按平台自动下载（Windows / Linux / macOS，x64 / arm64），国内镜像兜底仅 Windows 版；相关行为变更必须同步 README 与 CHANGELOG。
4. **插件数据目录**：`data/plugin_data/astrbot_plugin_baidu_netdisk/`（经 `StarTools.get_data_dir()` 解析，另有旧版 AstrBot 兼容回退）。

---

## 3. 技术栈与依赖

- **Python 3.10+**（AstrBot 4.x 最低要求）
- **AstrBot API**：`astrbot.api.star`（Context, Star, register, StarTools）、`astrbot.api.event`（filter, AstrMessageEvent）、`astrbot.api`（logger, AstrBotConfig）
- **依赖管理**：`requirements.txt` 中声明，复杂依赖优先 **惰性 import**（在方法内 import，避免启动时强制加载）
- **禁止引入**：不引入与原功能无关的依赖；非必要不新增 `requirements.txt` 声明
- AstrBot 运行时已自带的依赖（如 `aiohttp`、`certifi`）可在惰性 import 中引用，import 失败时优雅降级

---

## 4. 核心代码规范

### 4.1 插件骨架

```python
import ...
from collections.abc import AsyncGenerator

from astrbot.api.star import register, Star, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import AstrBotConfig, logger

PLUGIN_NAME = "astrbot_plugin_baidu_netdisk"
DEFAULT_TIMEOUT = 30

@register(
    "astrbot_plugin_baidu_netdisk",
    "XTsat",
    "百度网盘分享文件自动下载发送",
    "2.0.0",
    "https://github.com/XTsat/astrbot_plugin_baidu_netdisk",
)
class BaiduNetdiskPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        # 初始化其它资源

    @filter.command("pan", "下载百度网盘分享文件")
    async def on_pan(
        self, event: AstrMessageEvent, args: GreedyStr
    ) -> AsyncGenerator[MessageEventResult, None]:
        yield event.plain_result("帮助信息")
```

### 4.2 命名约定

| 元素 | 约定 |
|------|------|
| 模块级常量 | `UPPER_SNAKE`（`DEFAULT_TIMEOUT`、`MAX_CONCURRENCY`） |
| 私有方法 | `_` 前缀 + `snake_case`（`_rebuild_media_component`） |
| Web API 方法 | `api_` 前缀 |
| 命令方法 | `cmd_` 前缀 |
| 类名 | `PascalCase`（`BaiduNetdiskPlugin`） |
| 文件名 | `snake_case`（`main.py`、`_conf_schema.json`） |

### 4.3 类型与语法

- Python 3.10+ 现代联合类型写法（`dict \| None`、`list[dict]`、`tuple[str, str]`）
- 函数签名**必须完整类型注解**（参数 + 返回类型）；用 `yield` 返回结果的命令/事件处理方法标注为 `AsyncGenerator[MessageEventResult, None]`
- 禁止 `Any` 作为类型注解（除非调用方签名强制要求）
- 禁止 `# type: ignore`（无理由的类型作弊）

### 4.4 注释规范

- 注释规范见第 7 章「文档维护规范」的 [7.3 注释规范](#73-注释规范)

### 4.5 日志

- 统一 `logger.info/warning/error`，**必须带 `[baidu_netdisk]` 前缀**（本插件日志前缀统一为 `[baidu_netdisk]`）
- 错误信息带 emoji 前缀（如 `❌`、`⚠️`）
- 精确捕获异常类型（`asyncio.TimeoutError`），禁止空 `except: pass`（除非注释说明理由）
- 异常分类记录：ValueError = 非法参数，OSError = 文件/IO 错误，各自记录，单规则失败不影响其它规则

### 4.6 错误处理与防御性编程

- 面对不规范的输入必须优雅 fallback（参考 `_get_safe_plugin_info` 风格）
- 外部 IO 必须 try/except 并记录日志，**不允许静默吞异常**
- 配置值防御式校验：`max(1, int(...))`、`(TypeError, ValueError)` → 默认值
- 降级链路清晰：首选方案 → 兜底方案 → 占位文本

### 4.7 并发

- 异步方法使用 `async/await`，**禁止阻塞事件循环**
- 并发限制用 `asyncio.Semaphore(MAX_CONCURRENCY)` + `asyncio.as_completed`，`finally` 中取消未完成任务
- 渲染/计算重活必须走 `asyncio.to_thread` 或子进程
- `ProcessPoolExecutor` 即用即销（`with ProcessPoolExecutor(...)`），子进程结束强制释放内存

### 4.8 回复消息

- 统一 `yield event.plain_result(...)`
- 用户可读输出用中文 + emoji 图标风格

### 4.9 权限控制

- 管理类命令加 `@filter.permission_type(filter.PermissionType.ADMIN)`

---

## 5. 配置与数据文件

### 5.1 配置 Schema（_conf_schema.json）

- 新增配置项 → 先在 `_conf_schema.json` 补 schema（type/hint/default/options），再在代码中实现读写
- 配置项在文档（README 配置表、CHANGELOG）中引用时，**中文名在前、英文 key 在后**：统一写成「中文名（`key`）」形式，例如「最大下载大小（`max_file_size`）」「发送模式（`send_mode`）」；中文名取自 `_conf_schema.json` 的 `hint` 字段，`key` 用反引号包裹。
- 配置持久化：`self.config["rules"]` 读写 → `self.config.save_config()` 保存
- 数据文件存于 `astrbot/data/plugin_data/<插件名>/`（通过 `StarTools.get_data_dir("插件名")` 获取路径）
- 写操作持 `asyncio.Lock`，读写用 `encoding="utf-8"` + `ensure_ascii=False`
- 原子写入：先写 `.tmp` 再 replace

### 5.2 配置读取模式

```python
DEFAULT_SETTINGS = {
    "timeout": 30,
    "max_retries": 3,
}

# 合并：只保留已知字段，丢弃未知字段
settings = {k: self.config.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
```

---

## 6. 指令注册与命令清单

### 6.1 指令注册模式

本插件为单入口指令 `/pan`，子命令由参数首词分派（`login` / `down` / `look` / `pcs` / `help`）：

```python
@filter.command("pan", "下载百度网盘分享文件")
async def on_pan(
    self, event: AstrMessageEvent, args: GreedyStr
) -> AsyncGenerator[MessageEventResult, None]:
    parts = [p for p in str(args).split() if p]
    if not parts or parts[0] == "help":
        yield event.plain_result("帮助信息")
        return
```

若后续改为指令组形式，按下列模式注册：

```python
@filter.command_group("pan")           # 指令前缀 /pan
def pan_group(self) -> None:
    pass

@pan_group.command("help")             # 子命令 /pan help
async def cmd_help(
    self, event: AstrMessageEvent
) -> AsyncGenerator[MessageEventResult, None]:
    yield event.plain_result("结果")
```

### 6.2 事件监听

```python
@filter.event_message_type(filter.EventMessageType.ALL)
async def on_message(
    self, event: AstrMessageEvent
) -> AsyncGenerator[MessageEventResult, None]:
    # 处理全部消息（若不 yield，则标注 -> None）
    pass
```

### 6.3 命令表格式（README 中呈现）

| 命令 | 权限 | 说明 |
|------|------|------|
| `/pan help` | 全部 | 显示帮助 |
| `/pan <链接> [提取码]` | 全部 | 查看分享目录树 |
| `/pan down <路径>` | 全部 | 下载文件夹或单个文件 |

---

## 7. 文档维护规范（强制，不可省略）

> 本项目的**硬性要求**：任何改动在合并前，必须同步维护 `README.md` 与 `CHANGELOG.md`。未同步文档 = 任务未完成。

### 7.1 CHANGELOG 维护

#### 流程

- 日常改动先记在 `## [Unreleased]` 段（顶部固定）。**发版时**再把内容移动到带版本号的新段。
- 多个不相关改动在同一工作周期内，各自独立记入 `[Unreleased]`，**不要**合并为一条。
- **发版日期**：版本段日期 `(YYYY-MM-DD)` 填**当前日期**（以发版当天的系统时间为准，例如 `2026-09-22`）。

#### 格式

统一使用 **分组式**（与 scaffold 的 `CHANGELOG.md.template` 一致）：按分类用 `###` 子标题分组，条目用「标题 + 冒号 + 说明」，**仅重点条目/重点内容才加粗**：

```markdown
# Changelog

## [Unreleased]

> 一句话总结这个版本（可选）

### 新增
- 功能 A：说明
- **功能 B**：说明

### 修复
- 问题 C：说明

## v0.3.0 (2026-07-06)

> 一句话总结这个版本（可选）

### 新增
- 功能 D：说明
```

分组子标题分类：`新增` / `修复` / `变更` / `移除` / `性能`（只保留本次有内容的分类，无内容的分类不写空标题）

**加粗规则**：条目默认不加粗；写 changelog 时先识别本次改动中的**重点改动**（核心新功能、重要修复、破坏性变更等），只对重点改动用 `**加粗**` 标题强调，其余条目一律不加粗。

**版本总结**：版本标题（`## [Unreleased]` / `## vX.Y.Z (YYYY-MM-DD)`）与分类子标题之间，可加一行 `> 引用` 形式的一句话总结（概括该版本/该批改动的主旨），可选，不强制。

#### 写作视角（面向用户）

CHANGELOG 的读者是**插件使用者**（服务器管理员、群成员），不是开发者。条目只写「用户能感知到的变化」，不写内部实现：

- **写用户视角的结果**：用户能做什么、会看到什么、行为/默认值/配置有什么不同。
- **禁止内部实现细节**：类名/函数名、方法拆分、数据结构、算法、缓存与并发策略、日志埋点、依赖升级、重构过程，一律不写。
- **禁止出现代码标识符与文件路径**（如 `_rebuild_media_component`、`core/worker.py`）；用户直接接触的**命令名**、**配置项 key** 除外。
- **无用户感知的改动不记入 CHANGELOG**：纯内部重构、格式化、注释与测试调整等，写了用户也读不懂、也用不上。
- 判断标准：使用者读完一条，能决定「要不要升级、升级后有什么不同」。

正反例：

```markdown
### 新增
- ✅ 转发图片到游戏内（`forward_image_to_mc`）：开启后图片以地图画形式发送
- ❌ 新增 `_rebuild_media_component` 并重构消息构造链路，改用 dataclass 缓存节点

### 修复
- ✅ 修复超时后不再重试、消息被静默丢弃的问题
- ❌ 修复 `as_completed` 回调未捕获 `asyncio.TimeoutError` 导致的 `finally` 泄漏
```

#### 版本号联动

涉及功能新增/删除、行为变化、配置项变化时，**三处同步递增**：

| 文件 | 位置 |
|------|------|
| `metadata.yaml` | `version` 字段（`vX.Y.Z` 格式） |
| `CHANGELOG.md` | 发版时从 `[Unreleased]` 移入 `## vX.Y.Z (YYYY-MM-DD)` |
| `README.md` | 版本徽章（本项目徽章取 git tag，发版打 tag 后自动更新） |

纯修复/内部重构可只更新 `[Unreleased]` 条目，不递增版本号。

#### 多会话并行开发

**⚠️ 更新 `CHANGELOG.md` 前必须先读取当前文件内容**，识别并保留其他会话已写入的既有条目（含 `[Unreleased]` 下未提交的功能），只追加自己的条目，**严禁整文件覆盖或删除他人记录**。

### 7.2 README 维护

> 本项目**仅维护中文版 `README.md`**，未创建 `README_en.md`（见 2.3）。以下 7.2.2 与 7.2.5 涉及英文版的规则，在英文版创建之前不适用；其余规则全部适用。

#### 7.2.1 写作视角（面向用户）

README 的读者是**插件使用者**（服务器管理员、群成员），不是开发者。只写用户能直接用到的东西：

- **该写的**：能做什么（能力清单）、怎么装、怎么配（配置表）、怎么用（指令表与示例）、出问题怎么办（常见问题、前置条件与限制）。
- **不该写的**：实现原理与设计理念、架构分层与模块划分、算法与数据结构、内部调用流程、类名/函数名/文件路径、性能优化手段、依赖内部实现。
- **代码只贴用户要用的最小片段**：安装命令、配置项、指令示例；不贴内部实现代码。
- **技术细节翻译成用户语言**：不写「用 `asyncio.Semaphore` 限制并发」，写「同时处理的任务数量上限（`max_concurrency`），超出后排队」。
- 判断标准：使用者只读 README，就能独立完成安装 → 配置 → 使用 → 排障。

正反例：

```markdown
- ✅ 支持把图片以地图画形式转发到游戏内（需在配置中开启）
- ❌ 图片经 `_rebuild_media_component` 转为 dataclass 节点，再由 worker 渲染为地图画
```

#### 7.2.2 头部的表头格式

每个插件维护 **两个** README（中文 `README.md` + 英文 `README_en.md`），**中文版为权威版本（源）**，任何修改先落中文版、再同步到英文版（方向规则见 7.2.5）。表头格式如下（**元素间不留空行**）：

**中文版（`README.md`）：**

```html
<div align="center">
<h1>DisplayName</h1>
<p><strong>插件描述（desc）</strong></p>
<p><sub>标签1 &nbsp;&nbsp; 标签2 &nbsp;&nbsp; 标签3</sub></p>
<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a></p>
</div>
```

**英文版（`README_en.md`）：**

```html
<div align="center">
<h1>Plugin Name</h1>
<!-- 英文 h1 取插件名后缀首字母大写：astrbot_plugin_model_status → Model Status -->
<p><strong>Plugin description</strong></p>
<p><sub>tag1 &nbsp;&nbsp; tag2 &nbsp;&nbsp; tag3</sub></p>
<p><a href="README.md">中文</a> &nbsp;/&nbsp; <strong>English</strong></p>
</div>
```

命名规则：
- **中文版 h1**：直接取 `metadata.yaml` 的 `display_name` 字段（本项目为「百度网盘下载」）。
- **英文版 h1**：取插件名 `astrbot_plugin_xxx` 中的 `xxx` 部分，拆分为单词后每个首字母大写。例如 `astrbot_plugin_reread` → `Reread`；`astrbot_plugin_model_status` → `Model Status`。
- **desc**：中文版用 `metadata.yaml` 的 `desc` 字段；英文版做地道翻译。
- **标签（tags）**：取自 `metadata.yaml` 的 `tags` 字段（若有），用 `&nbsp;&nbsp;` 分隔。中文版用中文标签，英文版用英文标签。

#### 7.2.3 README 四大结构

README 正文严格按以下 4 大板块组织，但不强制使用 `##` 标题层级，可灵活组合：

| 板块 | 内容 | 说明 |
|------|------|------|
| **一、功能/介绍** | 功能介绍、特性列表、用户使用场景、截图/演示 | 独立板块 |
| **二、指令** | 命令表格（命令、权限、说明）、使用示例 | 独立板块 |
| **三、配置** | 配置项表格（配置项、类型、默认值、说明）、`_conf_schema.json` 说明 | 独立板块 |
| **四、其它** | 安装、快速开始、运行环境与依赖要求、贡献指南、常见问题、许可证、鸣谢等 | 剩余所有内容放这里 |

注意点：
- 板块 4 是兜底区域，不强制子标题顺序，但**安装/快速开始**建议放在最前。
- **板块一/板块四只写用户视角**：写「运行环境与依赖要求」而非框架选型理由、架构分层、模块职责；实现原理一律不进 README（写作视角见 7.2.1）。
- 更新后通读一遍 README，确保与新行为一致（示例：默认值、超时说明、数据目录等）。

#### 7.2.4 何时必须更新 README

| 改动类型 | 必须更新的板块 |
|----------|--------------|
| 功能新增/删除 | 一、功能/介绍 |
| 使用方式/命令变化 | 二、指令 |
| 配置项变化 | 三、配置 |
| 依赖/版本要求变化 | 四、其它（安装/依赖部分） |
| 已知问题/行为变化 | 四、其它（常见问题部分） |

**何时不需要更新 README：**
- 纯内部重构（行为完全不变）、样式调整、bug 修复（除非修复改变了用户可见行为）

#### 7.2.5 双语文档同步

- 中文版用 `README.md`，英文版用 `README_en.md`。
- **中文为主（单向同步）**：中文版是**权威版本（源）**，英文版跟随中文版。所有改动**先落中文版，再从中文同步到英文版**；**禁止反向**——不得先改英文版再回填中文，不得用英文版覆盖中文版。
- 章节结构必须保持一致，新增章节两个文件都要加。
- 中文版使用自然中文，英文版使用地道英文，**禁止**逐字机翻、禁止留下半翻译的句子。
- 头部的语言切换链接互相指向对方文件。
- 修改时逐条 diff，确保无内容遗漏或错位。
- **本项目现状**：未创建 `README_en.md`，故暂不执行本节；若日后创建，本节规则立即生效。

#### 7.2.6 格式与多会话约束

- **禁止无意义的换行**：HTML 头部区块内元素间不留空行；正文长句不要人为软换行（一段文字保持一行，由 Markdown 自动折行显示）；空行只用于分隔标题、表格、列表、代码块等结构性区块。
- **多会话并行修改**：更新 `README.md` / `README_en.md` 前必须先读取当前文件内容，识别并保留其他会话已写入的既有内容，只追加/修改自己的部分，**严禁整文件覆盖或删除他人记录**。

### 7.3 注释规范

- **注释只写 non-obvious reason**：解释「为什么这么做」，而不是「做了什么」（代码本身应自文档化）
  - 坏：`# 设置超时时间为30秒`（代码已写 `timeout = 30`）
  - 好：`# 30秒超时，因为上游API的SLA承诺P99为25秒`
- **禁止残留 intermediate attempts**：不保留被注释掉的旧代码、TODO 但未完成的方案、调试输出
- 方法级 docstring 用中文说明用途/参数/返回（仅对 `_` 开头私有方法可省略，如果用途足够明显）
- 代码分区用 `# ---- 分区名 ----` 分隔条

### 7.4 PR 描述规范

- **PR 描述只写最终行为**：描述最终完成了什么，而不是过程中尝试了什么
- 禁止提及：尝试过的方案、被否定的设计、从未合入的中间状态
- 只写 diff 里看不出来的取舍理由（如「选用方案A因为性能比B高20%」）

---

## 8. 提交与发布流程

### 8.1 提交规范

- 提交信息遵循 **Conventional Commits**：`<type>(<scope>): <中文短句式 subject>`（如 `feat: 开放颜色配置项`、`fix: 失败发送通知补重试`、`docs: 补充配置说明`）
- **禁止**将未完成的功能、多轮迭代的中间状态、被废弃的方案写进提交信息
- **提交前必须先向用户展示改动内容和提交标题**，等用户确认后再执行 commit；展示内容至少包含：待提交文件清单、改动概要（如 `git diff --stat`）、提交标题；涉及推送时另附提交 hash 与目标分支
- 推荐顺序：完成功能代码 → 跑通验证 → 更新 CHANGELOG 与 README → 提交
- 文档更新与代码改动**在同一提交中完成**（不单独拆「补文档」提交）
- 只有用户明确要求时才 commit / push
- **授权一次性，不延续**（「上一轮说过推送」≠ 本轮可以提交）：
  - 用户的确认**只对当次动作生效**；下一次改动必须重新展示改动内容与提交标题，重新确认
  - 展示后改动内容又变了（用户提了新要求、你又改了文件，哪怕只改一行），**原确认立即失效**，必须重新展示并再次确认
  - 下列情形**都不算授权**：上一轮 / 上一个任务 / 上一个会话说过「提交」「推送」；会话恢复（resume / fork）后继承的历史授权；用户对功能或代码表示满意；用户正在讨论其它需求；用户未回复
  - 授权范围逐项独立：确认 commit ≠ 授权 push；确认推送某分支 ≠ 授权后续继续提交或推送；确认一个提交 ≠ 授权 `--amend` 或追加提交
  - 执行前自检：能指出用户**本轮**对**本次改动**的明确同意原话；指不出来就先问，不要执行

### 8.2 版本号规范

- 采用 **SemVer**：新功能 → `minor` 递增；bug 修复/小改进 → `patch` 递增
- `metadata.yaml` version 与 `CHANGELOG.md` 最新版本必须一致
- 发版时打 `git tag vX.Y.Z`，tag 推送同样需用户确认

### 8.3 测试与验证

- 项目无强制测试框架，**改动后自检语法与逻辑即可**
- 若项目已补充单测，改动后需保证测试通过
- 格式检查：`python -m ruff format .`（如果项目历史有 ruff 提交）

---

## 9. 最终检查清单（每次任务完成前）

- [ ] 架构：所选风格（单文件 / 分层多文件）与既有代码一致
- [ ] 注释：只写 non-obvious reason，无残留中间状态、无被注释掉的旧代码
- [ ] 日志：`[baidu_netdisk]` 前缀，异常分类记录，无空 catch
- [ ] CHANGELOG：`[Unreleased]` 已追加条目，条目面向用户（无内部实现细节），保留了他人的既有记录
- [ ] README：涉及用户可见变更时已同步更新，且面向用户书写（无实现原理与内部细节）
- [ ] 版本号：`metadata.yaml` 与 `CHANGELOG` 最新版本一致
- [ ] 配置：新增配置项已在 `_conf_schema.json` 和代码中同步
- [ ] 提交信息：Conventional Commits 格式（`type: 中文短句式`）
- [ ] 提交/推送：已取得用户对本次改动的明确确认（一次性授权，未沿用上一轮）
- [ ] 本地测试：`python -c "import main"` 无语法错误（或 ruff 格式通过）
