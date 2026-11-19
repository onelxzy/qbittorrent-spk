# qBittorrent for Synology DSM 7

把 [qBittorrent 官方](https://github.com/qbittorrent/qBittorrent) 的 `qbittorrent-nox` 标准版封装成群晖 DSM 7 套件（SPK），由 GitHub Actions 定时构建并发布。

- 上游二进制：[userdocs/qbittorrent-nox-static](https://github.com/userdocs/qbittorrent-nox-static)（直接用官方 qBittorrent 源码编译的静态构建）
- **仅标准官方版，不含 Enhanced 增强补丁**
- 不依赖 Docker，直接运行原生静态二进制，无需额外库

## 特性

- 跟随上游 Release 自动构建，通常在上游发版后数小时内产出新包
- 同时提供 x86_64 与 ARM（aarch64）两种架构
- 构建过程固定时间戳与 gzip 头，同版本产物字节级可重现
- 版本号遵循群晖规范：主版本一致时横线后构建序号递增（见下）
- 从矿神 SPK 版覆盖升级时沿用原有数据目录，做种列表与 WebUI 设置保留

## 安装

### 手动安装

在 [Releases](../../releases) 下载对应架构的安装包，然后到套件中心 →「手动安装」：

| 架构 | 文件 |
| --- | --- |
| x86_64 机型 | `qBittorrent_<version>_x86_64.spk` |
| ARM 机型 | `qBittorrent_<version>_armv8.spk` |

若不确定机型对应的架构，可在 DSM「控制面板 → 信息中心」查看，或参考[群晖机型列表](https://kb.synology.com/DSM/tutorial/What_kind_of_CPU_does_my_NAS_have)。

### 通过套件源安装

把本仓库的 Release 汇总到自建套件源后，即可在套件中心直接搜索安装。仓库本身不提供在线套件源。

## 配置

安装向导会要求填写 Web 管理界面的用户名与密码（密码至少 8 位，需包含大小写字母、数字与符号），安装后即可用该账号登录。

- Web 管理界面：`http://<NAS地址>:8085`
- 套件数据目录：`/var/packages/qBittorrent/var`
- qBittorrent 配置：`/var/packages/qBittorrent/var/qBittorrent_conf/config/qBittorrent.conf`
- 共享文件夹 `Download` 会自动授权给套件用户，可作默认下载目录

> 若安装时未成功写入凭据，qBittorrent 会使用临时密码并在套件日志中给出；也可在 WebUI 中修改后保存，配置位于上述路径。

## 版本号规则

群晖只接受简单的版本号格式，因此采用 `<qBittorrent 主版本>-<构建序号>`：

| 上游变化 | 版本号 |
| --- | --- |
| 首次打包 | `5.3.0-1` |
| 上游同一 qBittorrent 版本，仅更新 libtorrent | `5.3.0-2` |
| 上游发布新的 qBittorrent 主版本 | `5.4.0-1`（序号重置） |

上游 Release 的 tag 记在 `build.py` 的 `UPSTREAM_MARKER` 中并写入 Release 正文，构建器据此判断某个上游版本是否已打包，避免重复构建与版本号复用。

## 构建

```bash
python build.py
```

产物输出到 `dist/`。构建流程：

1. 查询上游最新正式 Release（忽略 prerelease）
2. 解析 tag（`release-<qBittorrent>_v<libtorrent>`），推导下一个版本号
3. 按架构匹配资产、下载并校验 ELF 架构
4. 组装 `package.tgz` 与 `INFO`，打成 SPK
5. 生成 `dist/manifest.json` 与发布说明

需要强制重建某个已发布的上游版本时，在 Actions 页面手动触发并勾选 `force_rebuild`（构建序号继续递增，不复用旧号）。

仓库结构：

```
build.py               版本编排、上游抓取与构建入口
spk_builder.py         SPK 封装实现
app.json               套件元数据与上游资产匹配规则
template/              INFO、conf、scripts、UI 等套件框架文件
.github/workflows/     自动构建与发布
```

最低 DSM 版本：7.1-42661

## 免责声明

本项目是社区维护的封装脚本，与上游项目及群晖科技均无隶属关系。软件本身版权归上游作者所有，使用前请阅读上游项目的许可证。

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
