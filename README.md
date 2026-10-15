# astrbot_plugin_baidu_pan

百度网盘分享文件自动下载发送插件：一个百度账号登录后，全群成员共享高速下载能力（免会员）；主推扫码登录，不收集、不存储、不传输账号密码，源码完全开源可审计。

> 💡 **核心优势（共享会员）**：一个百度账号登录后，所有群成员共享高速下载能力，人人无需开通网盘会员。

> ⚠️ **重要限制（必读）**：**不支持下载自己的分享链接**（百度 API 限制同一账号不能把自己分享的文件转存回自己的网盘）。分享者本人请从网盘原目录直接下载，或重新分享新链接。

## 功能

- **共享会员**：一个账号登录，全群共享高速下载，无需人人开会员
- **特大分享可用**：TB 级分享也能正常浏览目录；下载时只精准转存选中的文件/文件夹，网盘剩余空间不大也能用
- **抗分享页故障**：转存/列目录不依赖分享页打开，分享页故障时依然可用
- **国内外可用**：服务器在国外也能下载百度网盘文件
- **目录查看与按需下载**：查看分享目录树，按需下载文件夹或单个文件
- **LLM 工具**：`pan_list` / `pan_transfer` / `pan_download_dir` / `pan_download_all`，AI 自动识别网盘链接并执行查看/转存/下载
- **大小与超时控制**：可配置最大下载大小（默认不限制）与下载超时（默认不超时）
- **多平台适配**：napcat / SnowLuma（OneBot）、QQ 官方机器人、其他平台三种发送模式，大文件自动闪传
- **自动清理**：下载完成后自动删除网盘转存文件；可配置定时清理本地下载文件（只清理本插件下载的）
- **扫码登录**：主推 `/pan qrlogin`，无需密码、不怕风控；也可配置 `bduuss` 启动时自动登录
- **自动下载工具**：首次登录自动下载 BaiduPCS-Go，无需手动安装；网络差时自动回退国内镜像
- **用户黑名单**：可配置黑名单，被拉黑用户的消息不回复（仅 OneBot 模式）

## 指令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/pan <链接或surl> [提取码]` | 全部 | 查看分享目录树（并缓存链接） |
| `/pan ls [链接] [提取码]` | 全部 | 查看目录树，不下载文件；无参数重看缓存 |
| `/pan look` | 全部 | 重新查看最近一次的目录树 |
| `/pan dir <文件夹路径>` | 全部 | 下载文件夹 |
| `/pan file <文件路径>` | 全部 | 下载指定文件 |
| `/pan qrlogin` | 全部 | 扫码登录（推荐） |
| `/pan bduss <BDUSS> [STOKEN]` | 全部 | BDUSS 登录 |
| `/pan cookies <完整Cookie字符串>` | 全部 | Cookie 登录 |
| `/pan login <账号> <密码>` | 全部 | 账密登录（建议私聊使用） |
| `/pan unlogin` | 全部 | 退出登录 |
| `/pan download` | 全部 | 下载或重新下载 BaiduPCS-Go 工具（文件损坏时也可修复） |
| `/pan download git` | 全部 | 强制从 GitHub 官方 release 下载（国内镜像失效时使用） |
| `/pan help` | 全部 | 查看帮助 |

**使用示例**：

```
/pan https://pan.baidu.com/s/1abc123?pwd=abcd   # 查看目录树
/pan dir 文件夹路径                              # 下载文件夹
/pan file 文件路径                               # 下载指定文件
```

> 💡 下载前会把选中内容单独精准转存到网盘暂存目录（需剩余空间大于该项大小），超大分享可挑着下；空间不足会明确提示。切换新链接后缓存自动覆盖，且不会清空网盘转存目录，多人查看不同链接互不影响。

## 配置

在 AstrBot WebUI 的插件设置中修改以下配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 登录提示（`login_hint`） | string | read | 仅提示用，无需填写 |
| BDUSS Cookie（`bduuss`） | string | 空 | 填写后启动时自动登录；浏览器 F12 → Application → Cookies 获取 BDUSS |
| 最大下载大小（`max_file_size`） | int | 0 | 0=不限制；official 模式硬限 200MB |
| 发送模式（`send_mode`） | string | onebot | onebot：napcat/SnowLuma；official：QQ 官方；other：其他平台 |
| 用户黑名单（`blacklist`） | string | 空 | 多个 QQ 号用逗号分隔；仅 onebot 模式生效 |
| 本地下载目录（`download_dir`） | string | 空 | 留空用 AstrBot 插件数据目录 `data/plugin_data/astrbot_plugin_baidu_netdisk/downloads/` |
| 网盘转存目录（`cloud_save_dir`） | string | 空 | 留空用默认 `/我的资源/AutoTransfer` |
| 下载后自动删除转存（`auto_delete_cloud`） | bool | true | 文件下载并发送后删除网盘转存文件（回收站可找回） |
| 本地定时清理（`local_cleanup_hour`） | int | 3 | 每天几点清理插件下载的本地文件（只清理本插件下载的；-1=关闭） |
| 下载进度显示（`progress_enabled`） | bool | false | 开启后下载时定期报告进度和速度 |
| 进度更新间隔（`progress_interval`） | int | 30 | 进度报告间隔（秒），建议 30-60 |
| 下载超时（`timeout_seconds`） | int | -1 | -1=不超时；正数超时后自动终止下载进程 |

**发送模式对比**：

| 模式 | `send_mode` 值 | ≤200MB | >200MB |
|------|---------------|--------|--------|
| napcat / SnowLuma（OneBot） | onebot | 直接发送文件 | 走闪传 |
| QQ 官方机器人 | official | 走官方 API 上传 | 返回链接提示 |
| 其他平台 | other | 直接发送文件 | 直接发送文件（失败则返回链接） |

- `max_file_size` 决定「是否下载」，`send_mode` 决定「怎么发」；official 模式超 200MB 会在下载前直接拦截并返回链接
- `send_mode` 未配置时根据当前平台自动选择 onebot 或 official

## 其它

### 安装

- **方式一**：AstrBot WebUI 插件市场中搜索 `baidu_pan` 安装
- **方式二**：将本项目复制到 AstrBot 的 `addons/` 目录，重启 AstrBot

### 登录说明

- **扫码登录（推荐）**：发送 `/pan qrlogin`，用百度网盘 APP 扫码确认，全程无需输入账号密码；二维码 3 分钟有效，凭证保存在本机，重启 AstrBot 无需重新登录
- **BDUSS 登录**：浏览器 F12 → Application → Cookies 复制 BDUSS（和 STOKEN）后发送 `/pan bduss <BDUSS> [STOKEN]`
- **账密登录**：`/pan login <账号> <密码>`，建议私聊执行并用后撤回消息；百度风控可能返回 50052，此时改用扫码或 BDUSS

### BaiduPCS-Go

插件**不内置** BaiduPCS-Go.exe。**无需手动下载**：首次执行登录类命令（`/pan qrlogin`、`/pan login`、`/pan cookies`、`/pan bduss`，或配置 `bduuss` 启动自动登录）时会自动下载 GitHub 官方 **最新 release**（GitHub 不可达时自动回退国内镜像）；也可手动用 `/pan download` 下载/更新，文件损坏用同一命令修复，只走 GitHub 时用 `/pan download git`。

> 数据存放：下载的文件、下载清单、登录二维码等运行时数据存放于 AstrBot 插件数据目录 `data/plugin_data/astrbot_plugin_baidu_netdisk/`（升级/重装插件不丢失）；登录凭证（`pcs_config.json`）沿用 BaiduPCS-Go 默认位置，保存在用户目录，不随插件数据清理。

### 安全声明

- **不收集、不存储、不传输任何账号密码**，源码完全开源可审计
- 主推扫码登录：密码根本不经过插件、机器人或任何第三方，登录凭证只保存在你自己的本机
- BaiduPCS-Go 是第三方开源工具（[qjfoidnh/BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go)），本插件不打包任何二进制文件，exe 从 GitHub 官方最新 release 下载，仅校验文件为有效 PE 程序

### 注意事项

- 需登录百度网盘账号；会员账号可获得更高下载速度
- 下载文件暂存于 `download_dir`（默认 AstrBot 插件数据目录 `data/plugin_data/astrbot_plugin_baidu_netdisk/downloads/`），用 `local_cleanup_hour` 定时清理（只清理本插件下载的文件）
- 下载完成默认自动删除网盘转存文件（回收站可找回），可关闭 `auto_delete_cloud`
- 文件超过 `max_file_size` 限制时返回原分享链接
- 网盘剩余空间不足时整包下载/转存会失败并明确提示，可改用 `/pan file <路径>` 或让 LLM 分批挑选
- 分享链接在百度侧失效（打开报 500 或「链接出错」）时插件会明确报错，需分享者重新分享
- napcat 闪传存在已知 BUG（Issue #2015），大文件闪传可能失败并降级为返回链接
- QQ 官方机器人模式超 200MB 会在下载前直接拦截并返回链接

### 项目信息

- 项目地址：https://github.com/XTsat/astrbot_plugin_baidu_netdisk
- License：AGPL-3.0
- 更新日志见 [CHANGELOG.md](CHANGELOG.md)