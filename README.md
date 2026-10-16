<div align="center">
<h1>百度网盘下载</h1>
<p><strong>百度网盘分享自动下载并发送。登录后全员都可使用。</strong></p>
<p><sub>百度网盘 &nbsp;&nbsp; 下载分享 &nbsp;&nbsp; QQ &nbsp;&nbsp; Astrbot</sub></p>
<p>
<a href="https://github.com/XTsat/astrbot_plugin_baidu_netdisk/tags"><img src="https://img.shields.io/github/v/tag/XTsat/astrbot_plugin_baidu_netdisk?label=version&color=blue" alt="version"></a>
<a href="https://github.com/XTsat/astrbot_plugin_baidu_netdisk/blob/master/LICENSE"><img src="https://img.shields.io/github/license/XTsat/astrbot_plugin_baidu_netdisk?label=license&color=green" alt="license"></a>
<img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue" alt="platform">
</p>
</div>

## 一、功能

- **共享会员**：一个百度账号登录后，全群成员共享高速下载能力，无需人人开通网盘会员
- **多人互不干扰**：分享链接记录每人各存一份，网盘转存目录也按用户隔离，多人同时使用不会串台、也不会误删别人的转存文件
- **特大分享可用**：TB 级分享也能正常浏览目录，下载时只转存选中的文件/文件夹，网盘剩余空间不大也能用
- **全平台可用**：Windows、Linux、macOS 均可部署，x64 / arm64 自动匹配
- **目录查看与按需下载**：查看分享目录树，按需下载文件夹或单个文件
- **文件夹自动发送**：QQ 等平台不能直接发送文件夹，插件会自动处理
- **LLM 工具**：`pan_list` / `pan_transfer` / `pan_download_dir` / `pan_download_all`，AI 自动识别网盘链接并执行查看/转存/下载；下载类工具先受理再后台执行，完成后自动把文件发到当前会话
- **大小与超时控制**：可配置最大下载大小与下载超时
- **多平台适配**：napcat / SnowLuma（OneBot）、QQ 官方机器人、其他平台三种发送模式，大文件自动闪传
- **自动清理**：下载完成后自动删除网盘转存文件；可配置定时清理本地下载文件
- **一键登录**：主推扫码登录，无需密码、不怕泄露
- **自动下载工具**：首次登录自动下载 BaiduPCS-Go，无需手动安装；GitHub 不通时自动挑选可用的加速镜像
- **用户黑名单**：可配置黑名单，被拉黑用户的消息不回复（仅 OneBot 模式）

> ⚠️ **重要限制（必读）**：**不支持下载自己的分享链接**——百度 API 限制同一账号不能把自己分享的文件转存回自己的网盘。分享者本人请从网盘原目录直接下载，或重新分享新链接。

## 二、指令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/pan <链接或surl> [提取码]` | 全部 | 查看分享目录树（并缓存链接）；支持直接粘贴整段分享文案，自动识别链接与提取码 |
| `/pan look` | 全部 | 重新查看最近一次的目录树 |
| `/pan down <路径>` | 全部 | 下载文件夹或单个文件（自动识别类型）；文件夹不超过阈值时逐个发送，更多则打包 zip 发送 |
| `/pan login` | 管理员 | 登录/退出统一入口，发送后显示完整登录菜单 |
| `/pan login 1` 或 `qr` | 管理员 | 扫码登录（推荐） |
| `/pan login 2 <账号> <密码>` 或 `pwd ...` | 管理员 | 账密登录（建议私聊使用） |
| `/pan login 3 <BDUSS> [STOKEN]` 或 `bduss ...` | 管理员 | BDUSS 登录 |
| `/pan login 4 <完整Cookie字符串>` 或 `cookies ...` | 管理员 | Cookie 登录 |
| `/pan login 0` 或 `out` | 管理员 | 退出登录 |
| `/pan pcs` | 管理员 | 下载或重新下载 BaiduPCS-Go 工具（文件损坏时也可修复；直连 GitHub 失败时自动挑选可用加速镜像，已保存代理则资产只走该代理，release 信息仍优先直连 GitHub 官方 API） |
| `/pan pcs git` | 管理员 | 强制直连 GitHub 官方 release 下载（不用代理、也不用加速镜像） |
| `/pan pcs <加速镜像地址>` | 管理员 | 指定加速镜像下载并保存（只接受 `https://` 地址，如 `https://gh-proxy.org/`），后续登录时自动下载也生效 |
| `/pan pcs off` | 管理员 | 清除已保存的加速镜像，回到自动挑选镜像的下载方式 |
| `/pan help` | 全部 | 查看帮助 |

> 💡 `/pan login` 系列与 `/pan pcs` 系列**仅管理员可用**：前者会改变全群共用的那一个百度账号（注销当前账号、或换成另一个账号，都会影响所有人），后者会下载并替换机器人本地运行的程序、也能改写下载来源。普通用户无需手动操作：工具缺失时管理员执行登录命令（如 `/pan login 1`）会自动下载，下载来源由插件固定（已保存的加速镜像或自动探测的可用镜像），用户无法更改。

**使用示例**：

```
/pan https://pan.baidu.com/s/1abc123?pwd=abcd   # 查看目录树（链接自带提取码）
/pan 通过百度网盘分享的文件：xxx.zip 链接:https://pan.baidu.com/s/1abc123 提取码:abcd   # 整段分享文案可直接粘贴
/pan down 文件夹路径                              # 下载文件夹
/pan down 文件路径                                # 下载单个文件
```

> 💡 链接识别很宽松：`/s/xxx`、`share/init?surl=xxx`、无协议头、`yun.baidu.com`、短链都支持；提取码支持 `?pwd=xxxx`、`提取码:xxxx`、`提取码：xxxx`、`密码:`、`访问码:`（半角/全角冒号均可），整段中文分享文案直接粘贴即可。

> 💡 下载前会把选中内容单独精准转存到**你自己的**网盘暂存目录（`<配置的转存目录>/<用户隔离子目录>`，需剩余空间大于该项大小），超大分享可挑着下；空间不足会明确提示。每个人各自记录自己最近查看的链接，多人查看不同链接互不影响。

## 三、配置

在 AstrBot WebUI 的插件设置中修改以下配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| 登录提示（`login_hint`） | string | read | 仅提示用，无需填写 |
| BDUSS Cookie（`bduuss`） | string | 空 | 填写后启动时自动登录；浏览器 F12 → Application → Cookies 获取 BDUSS |
| 最大下载大小（`max_file_size`） | int | 0 | 0=不限制；official 模式硬限 200MB |
| 文件夹打包上限（`zip_max_size`） | int | 10240 | 文件夹打包 zip 的大小上限（MB），0=不限制；超过则不打包，返回本地路径+文件清单 |
| 逐个发送阈值（`send_files_threshold`） | int | 3 | 文件夹内文件数 ≤ 此值时逐个发送（更易命中秒传）；超过则打包 zip；0=总是打包 |
| 发送模式（`send_mode`） | string | onebot | onebot：napcat/SnowLuma；official：QQ 官方；other：其他平台 |
| 用户黑名单（`blacklist`） | string | 空 | 多个 QQ 号用逗号分隔；仅 onebot 模式生效 |
| 本地下载目录（`download_dir`） | string | 空 | 留空使用 AstrBot 插件数据目录下的 `downloads/` |
| 网盘转存目录（`cloud_save_dir`） | string | 空 | 留空使用默认 `/我的资源/AutoTransfer`；每个用户的实际转存目录是它下面的隔离子目录 |
| 下载后自动删除转存（`auto_delete_cloud`） | bool | true | 文件下载并发送后只删除自己转存的网盘文件（回收站可找回），不影响他人 |
| 本地定时清理（`local_cleanup_hour`） | int | 3 | 每天几点清理插件下载的本地文件（只清理本插件下载的；-1=关闭） |
| 下载进度显示（`progress_enabled`） | bool | false | 开启后下载时定期报告进度和速度 |
| 进度更新间隔（`progress_interval`） | int | 30 | 进度报告间隔（秒），建议 30-60 |
| 下载超时（`timeout_seconds`） | int | -1 | -1=不超时（不按大小设限，仅在 12 小时兜底超时）；正数=固定超时秒数，超时后自动终止下载进程 |
| 允许不校验证书（`allow_insecure_tls`） | bool | false | 仅影响工具（BaiduPCS-Go）的下载：开启后证书校验失败不再报错，改为不校验证书继续下载，**存在被中间人替换文件的风险，仅建议在证书链不完整的可信网络里临时开启** |

**发送模式对比**：

| 模式 | `send_mode` 值 | ≤200MB | >200MB |
|------|---------------|--------|--------|
| napcat / SnowLuma（OneBot） | onebot | 直接发送文件 | 走闪传 |
| QQ 官方机器人 | official | 走官方 API 上传 | 返回链接提示 |
| 其他平台 | other | 直接发送文件 | 直接发送文件（失败则返回链接） |

- `max_file_size` 决定「是否下载」，`send_mode` 决定「怎么发」；official 模式超 200MB 时不会上传，直接返回原分享链接（文件已下载到本地，可从本地路径自取）
- `send_mode` 未配置时根据当前平台自动选择 onebot 或 official

## 四、其它

### 安装

- **方式一**：AstrBot WebUI 插件市场中搜索 `baidu_netdisk` 安装
- **方式二**：将本项目复制到 AstrBot 的 `addons/` 目录，重启 AstrBot

### 登录

- 以下登录/退出命令**仅管理员可用**（账号为全群共用，普通用户只需要用 `/pan`、`/pan down` 下载）
- **扫码登录（推荐）**：发送 `/pan login 1`（或 `/pan login qr`），用百度网盘 APP 扫码确认，全程无需输入账号密码；二维码 3 分钟有效，登录状态重启 AstrBot 后仍然有效
- **账密登录**：`/pan login 2 <账号> <密码>`（或 `/pan login pwd <账号> <密码>`），建议私聊执行并用后撤回消息；百度风控可能返回 50052，此时改用扫码或 BDUSS
- **BDUSS 登录**：浏览器 F12 → Application → Cookies 复制 BDUSS（和 STOKEN）后发送 `/pan login 3 <BDUSS> [STOKEN]`（或 `/pan login bduss <BDUSS> [STOKEN]`），**请私聊执行并用后撤回消息**（BDUSS 等同于账号密码，会留在聊天记录里）
- **Cookie 登录**：浏览器 F12 → Network → 任意请求 → 复制 Cookie 请求头后发送 `/pan login 4 <完整Cookies>`（或 `/pan login cookies <完整Cookies>`），同样**请私聊执行并用后撤回消息**
- BDUSS / Cookie 登录时凭证不再经过命令行参数，系统进程列表中不会出现你的凭证（非私聊执行时插件会先提醒撤回消息）
- **退出登录**：发送 `/pan login 0`（或 `/pan login out`）
- 记不住方式？发送 `/pan login` 即可查看完整登录菜单，所有方式都支持数字简写与完整指令两种输入

### BaiduPCS-Go

插件**不内置** BaiduPCS-Go（Windows 下为 `BaiduPCS-Go.exe`，Linux/macOS 下为无扩展名的 `BaiduPCS-Go`）。**无需手动下载**：管理员首次执行登录类命令（扫码、账密、BDUSS、Cookie，或配置 `bduuss` 启动自动登录）时会自动下载 GitHub 官方**最新 release** 中当前平台对应的二进制（Windows / Linux / macOS，x64 / arm64 自动匹配）。GitHub 直连不通时会**自动逐个探测常用的 GitHub 加速镜像**（`gh-proxy.com`、`ghfast.top` 等），用其中可用的那个完成下载，Windows / Linux / macOS 全平台都适用；探测成功的镜像在本次运行内会优先复用。上述自动下载无需用户干预，**下载来源也不可被普通用户更改**。

下载的压缩包会与 GitHub 官方给出的 **sha256** 核对，不一致就丢弃并换下一个来源；**走加速镜像（或管理员指定的代理）时必须能核对官方 sha256，核对不了就不用该来源**。release 信息（含官方 sha256）始终优先直连 GitHub 官方 API，只有直连不通才退回到加速源获取——此时校验信息与压缩包来自同一第三方，插件会在日志和下载结果里明确提示「本次无法抵御该来源替换文件」。下载过程中不校验证书的降级重试已默认关闭，证书校验失败会直接报错换源。

`/pan pcs` 系列会下载并替换机器人本地运行的程序，属管理操作，因此**仅管理员可用**：管理员可用它手动下载/更新/修复工具，也可指定镜像 `/pan pcs https://gh-proxy.org/`（地址自动保存，之后登录时自动下载也走它；`/pan pcs off` 清除后回到自动挑选），只走 GitHub 原始链接时用 `/pan pcs git`。为防下载被指向任意服务器，可填的镜像**只限已知的加速源且必须是 `https://`**（此前保存的明文 `http://` 地址会被自动忽略）；下载只在 GitHub 官方域名与已配置的加速源之间跳转，出现跳向其它主机的情况会直接拒绝。使用自建镜像的管理员可用环境变量 `BAIDU_NETDISK_EXTRA_PROXIES` 追加允许的域名（英文逗号分隔，添加后也会进入自动探测候选）。

### 安全声明

- **不收集、不存储、不传输任何账号密码**，源码完全开源可审计
- 主推扫码登录：密码根本不经过插件、机器人或任何第三方，登录凭证只保存在你自己的本机
- BaiduPCS-Go 是第三方开源工具（[qjfoidnh/BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go)），本插件不打包任何二进制文件，二进制从 GitHub 官方最新 release 按平台下载，下载时校验证书与官方 sha256（校验不过就换源或拒绝），下载后还会校验文件格式（Windows: PE；Linux: ELF；macOS: Mach-O）

### 注意事项

- 需登录百度网盘账号；会员账号可获得更高下载速度
- 下载文件暂存于 `download_dir`（默认 AstrBot 插件数据目录下的 `downloads/`），用 `local_cleanup_hour` 定时清理（只清理本插件下载的文件）
- 下载完成默认自动删除自己转存的网盘文件（回收站可找回），可关闭 `auto_delete_cloud`；转存目录按用户隔离，不会删到别人的文件
- `/pan pcs`（工具下载/更新/修复、指定或清除加速镜像）**仅管理员可用**；普通用户无需操作，登录时工具会自动下载，下载来源由插件固定、不可更改
- 工具下载失败时的处理顺序：① 重试 `/pan pcs`；② 换一个加速源 `/pan pcs https://gh-proxy.org/`；③ 提示 **TLS 证书校验失败**且确认当前网络可信时，开启配置项 `allow_insecure_tls` 再重试（开启后不再校验证书，有被中间人替换文件的风险）；④ 提示 **与官方 sha256 不一致**或**官方未提供 sha256** 时说明该来源给不出可信文件，换源重试即可，不要手工放行
- 文件超过 `max_file_size` 限制时会被拦截并提示实际大小（仅单文件生效；文件夹下载不受此限制，改由 `zip_max_size` 与平台上限控制）
- 同时进行的下载任务最多 2 个，超出的会排队并在前面的任务结束后自动开始（避免多人同时下载互相抢带宽）
- 下载完成后的结果若没能发出去（机器人掉线、会话过期等），插件会自动重试，并在该用户下次使用 `/pan` 时补发；插件重载或命令被中断时，已下载的内容同样会补发结果
- 目录树每个目录最多显示 60 项，超出部分会提示数量；未显示的文件仍可用 `/pan down` 加完整路径下载
- 网盘剩余空间不足时整包下载/转存会失败并明确提示，可改用 `/pan down <路径>` 或让 LLM 分批挑选
- 分享链接在百度侧失效（打开报 500 或「链接出错」）时插件会明确报错，需分享者重新分享
- napcat 闪传存在已知 BUG（Issue #2015），大文件闪传可能失败并降级为返回链接
- QQ 官方机器人模式超 200MB 会在下载前直接拦截并返回链接
- **Docker 部署**：登录凭证默认保存在容器内用户目录，不在 AstrBot 的 data 卷上，重建容器会丢失登录态、需重新扫码；**推荐把登录凭证放到插件目录**（BaiduPCS-Go 与插件会自动优先使用 exe 同目录的配置），插件目录本身在 `data/plugins/` 卷上，重建容器/升级插件都不丢登录态；也可把凭证目录挂载为数据卷，或用 `BAIDUPCS_GO_CONFIG_DIR` 环境变量指向持久化目录

### 项目信息

- 项目地址：https://github.com/XTsat/astrbot_plugin_baidu_netdisk
- License：AGPL-3.0
- 更新日志见 [CHANGELOG.md](CHANGELOG.md)
