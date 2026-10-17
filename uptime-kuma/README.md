# Uptime Kuma v2 部署到 Heroku

独立的 Composite Action：直接继承官方 Uptime Kuma v2 镜像，保留 Node、前端和系统依赖。启动时映射 Heroku 动态 `PORT`，把 `AIVEN_CA_BASE64` 解码为 PEM，并通过 `UPTIME_KUMA_DB_CA_FILE` 开启 Aiven MySQL 证书验证。本目录的构建、部署和测试均可独立使用。

## 首次部署

1. 将本目录和 `.github/workflows/deploy_uptime_kuma.yml` 提交到 **Docker2Heroku 仓库**，保持目录结构。GitHub Actions 从该仓库根目录查找 `.github/workflows`，本地外层工作目录不是发布根目录。
2. 在 GitHub 仓库 **Settings → Secrets and variables → Actions** 添加以下配置。已有同名 Secrets 可直接沿用。

   | 名称 | 建议位置 | 内容 |
   | --- | --- | --- |
   | `HEROKU_API_KEY` | Secret | 有权限创建或更新应用的 Heroku API Key |
   | `UPTIME_KUMA_DB_HOSTNAME` | Variable 或 Secret | Aiven 提供的数据库域名，不含协议 |
   | `UPTIME_KUMA_DB_PORT` | Variable 或 Secret | Aiven 分配的端口，不默认假设为 3306 |
   | `UPTIME_KUMA_DB_NAME` | Variable 或 Secret | 原 Kuma 数据库名称 |
   | `UPTIME_KUMA_DB_USERNAME` | Secret | 数据库用户名 |
   | `UPTIME_KUMA_DB_PASSWORD` | Secret | 数据库密码，原样填写，无需 URL 编码 |
   | `AIVEN_CA_BASE64` | Secret | Aiven 项目 CA PEM 文件的 Base64 |

3. 打开 **Actions → Deploy Uptime Kuma to Heroku → Run workflow**。填写 Heroku 应用名、精确 v2 镜像 tag 或 digest，首次保留允许创建。区域默认 `eu`，套餐默认 `basic`，固定只运行一个 web 实例。
4. 根据数据库备份/副本完成验证后，停止 Orkestr 正式实例，再让 Heroku 连接正式数据库。避免两个 Kuma 同时执行监控和发送通知。
5. 成功时日志显示 `Deployment health check passed`，运行摘要提供应用名称、URL、源镜像和构建镜像 ID。检查原账号、监控记录和通知；新数据库则访问应用创建管理员。

**镜像必须匹配旧实例的实际版本。** 支持 `louislam/uptime-kuma:2.x.y` 形式的精确版本、相应 slim/rootless tag 或 `louislam/uptime-kuma@sha256:...`。填写真实完整版本或 digest，不要原样填写 `2.x.y`。拒绝浮动 `:2` / `:latest`，容器 smoke check 还会验证镜像内部的版本为 v2。本地相邻的 Kuma 源码不参与构建。

应用不存在时创建 Cedar `container` stack 应用；已存在时复用，区域和团队保持原值。应用名留空并允许创建时生成 `uptime-kuma-xxxxxx`；记下名称用于后续更新，每次留空都会准备新名称。已有应用必须使用 `container` stack，且不能有运行中的 worker 等其他进程。

## 准备 Aiven CA

在 Aiven 数据库 Overview 页面下载项目 CA，假设文件名为 `ca.pem`。PowerShell 转为单行 Base64：

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path .\ca.pem).Path))
```

将输出完整保存为 `AIVEN_CA_BASE64` Secret。Base64 是传递格式，运行时会还原成 PEM；不要把 PEM 路径、服务端证书私钥或双重编码内容填进来。支持同一 PEM 中的 CA bundle，Aiven 轮换项目 CA 时更新 Secret 并重新运行 workflow。

数据库须预先可达，账号须有 Kuma 所需的建库检查、建表和后续迁移权限。Kuma 的 `mariadb` 配置分支实际通过 `mysql2` 连接 Aiven MySQL，保持 `UPTIME_KUMA_DB_TYPE=mariadb`。Aiven 若使用来源 IP 白名单，还需处理 Heroku Common Runtime 动态出口 IP；不能仅沿用 Orkestr 的来源 IP。

## 在 workflow 中配置

默认 workflow 已明确列出所有数据库输入。普通配置可直接修改 `with`，例如将 `db_hostname`、`db_port`、`db_name` 改为实际非敏感值；密码仍从 Secrets 读取。也可以不编辑 YAML，使用上表中的同名 Variables / Secrets，主机、端口、数据库名同名时 Secret 优先。

额外环境变量在 workflow 的 `heroku_config_vars` 中一行一个 `KEY=VALUE`。值不会经过 shell 求值，支持空格、`$`、反引号和 `=`；首尾引号可选。不要在一行拼多个赋值。保留首尾空格、多行值或删除变量时使用 JSON。

独立配置文件是本目录的 `heroku-config.json`，默认保存生产模式、数据库类型和 TLS 开关。可以在文件内增加非敏感配置，再提交仓库。`config_file` 路径相对于调用方的 `GITHUB_WORKSPACE`，也支持绝对路径；配置文件只在 CI 读取，不复制到镜像。

覆盖顺序从低到高：

```text
已有 Heroku 配置 → 默认值 → config_file → 非空数据库专用输入
→ workflow 的 heroku_config_vars → heroku_config_vars_json
```

可选 Secret `UPTIME_KUMA_HEROKU_CONFIG_JSON` 对应最高优先级 JSON 对象。值只能为字符串或 `null`，`null` 删除对应的 Heroku 配置。未提及的普通键保留原值；数据库专用输入留空也保留原值，因此后续更新可以沿用已配置的数据库。首次缺少有效数据库配置或 CA 会在创建应用和构建镜像前失败。

端口、监听地址、`DATA_DIR`、`UPTIME_KUMA_DB_CA` / `UPTIME_KUMA_DB_CA_FILE`、数据库 `_FILE` 变量和本地 socket 由入口管理，不允许在额外配置中设置。部署会自动清理已有的这些旧配置，运行时设置 `DATA_DIR=/tmp/uptime-kuma`。数据库类型必须是 `mariadb`，TLS 必须开启。旧的 `NODE_TLS_REJECT_UNAUTHORIZED=0` 默认删除，入口也会清除它，使用真实 CA 进行验证。

不要将凭证作为公开的 workflow_dispatch 参数。配置通过 Heroku JSON API 写入；凭证不拼进 shell、不用 Docker build args 传递。Action 会遮盖已知密码、用户名、Token 和 Base64 CA 的诊断输出。

## 在其他仓库调用

本目录发布到 `Bookpple/Docker2Heroku` 后可直接引用；生产环境建议将 `@main` 替换为已审核的 commit SHA。下面从调用仓库 Variable `UPTIME_KUMA_IMAGE` 读取与旧实例一致的精确 v2 tag 或 digest：

```yaml
name: Deploy Kuma
on:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: uptime-kuma-production
  cancel-in-progress: false
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: Bookpple/Docker2Heroku/uptime-kuma@main
        with:
          heroku_api_key: ${{ secrets.HEROKU_API_KEY }}
          docker_image: ${{ vars.UPTIME_KUMA_IMAGE }}
          heroku_app_name: ${{ vars.HEROKU_APP_NAME }}
          create_new_app: 'true'
          heroku_region: eu
          dyno_type: basic
          db_hostname: ${{ vars.UPTIME_KUMA_DB_HOSTNAME }}
          db_port: ${{ vars.UPTIME_KUMA_DB_PORT }}
          db_name: ${{ vars.UPTIME_KUMA_DB_NAME }}
          db_username: ${{ secrets.UPTIME_KUMA_DB_USERNAME }}
          db_password: ${{ secrets.UPTIME_KUMA_DB_PASSWORD }}
          aiven_ca_base64: ${{ secrets.AIVEN_CA_BASE64 }}
          heroku_config_vars: |
            TZ=Asia/Shanghai
```

不使用配置文件时调用方无需 checkout。若使用调用仓库的配置文件，先 checkout，再传 `config_file`；Action 的 Python、Dockerfile 和启动脚本始终从自己的目录读取。

## 发布检查、更新和限制

- 需要 Linux runner、Docker、Python 3、可访问的上游镜像和 Heroku 账号；需要时自动安装 Heroku CLI。`dockerhub_username` / `dockerhub_token` 可选，但必须一起传入。
- 镜像构建后，分别用 Docker 参数数组和 Heroku 单个命令字符串运行同一套检查：任意非 root UID、关闭容器网络、版本为 v2、`PORT` 覆盖旧的 3001、数据目录可写、CA 可解析、TLS 关闭标志已清除。任一启动形式失败都会中止发布。该检查不连接真实数据库。
- 更新时先完成构建、检查和镜像推送，再把旧 web 缩到 0 并等待退出，然后修改配置、发布、启动单个 web。会有短暂监控暂停，防止旧实例先用新配置启动及重复执行监控。
- 若停止旧 web 后发布失败，应用可能保持停止状态；检查日志、修正配置后重新运行 workflow。不自动回滚镜像或数据库迁移。跨版本更新前须备份数据库，必要时按备份恢复。
- 健康检查默认等待 300 秒，同时要求仅一个本次 release 的 web 为 `up`，以及 `/setup-database-info` 返回 `runningSetup=false` 和 `needSetup=false`。只打开首页不算成功。此接口证明启动阶段完成，不能证明所有监控都正常或提供持续的数据库探测。
- Heroku 默认要求 60 秒内绑定端口，Action 的等待时长不会修改该限制。若首次数据库准备超时，先在副本验证迁移耗时，再安排数据库迁移或评估临时提高启动时限。
- 默认 `basic`；拒绝会休眠的 `eco`。不提供多 web 实例扩容入口。浏览器监控、ICMP Ping 和通知通道受内存、权限及网络限制影响，迁移时分别验证。不要依赖宿主机 Docker socket 或内网访问能力。
- Heroku 文件系统是临时的。账号、监控项和历史数据保存在 Aiven；数据库配置和 CA 每次启动重新生成。上传 Logo、截图、Docker TLS 文件等仍在本地，重启会丢失；迁移前备份，需要长期保留时另行设计文件存储。
- 上游 README 明确把 Heroku 列为不支持的平台，本目录属于自行适配。Heroku 会周期性重启 dyno，单实例监控会有短暂中断。

Action 还支持 `heroku_team`、`wait_for_health`（默认 `true`）、`healthcheck_timeout`（10–1800 秒）、`show_logs`（默认 `true`）、`log_lines`（1–1500，默认 100）。`dyno_type` 留空时保留现有非 Eco 类型，新建建议保持 `basic`。输出为 `app_name`、`app_url`、`image`、`image_id`。

从 Docker2Heroku 仓库根目录检查：

```bash
python3 -B -m unittest discover -s uptime-kuma/tests -v
sh -n uptime-kuma/heroku-entrypoint.sh
sh -n uptime-kuma/exec-command.sh
```

测试的 `tests/fixtures/ca.pem` 是公开 CA 证书，仅用于离线解析测试，不含私钥，也不是实际 Aiven 配置。启动器集成测试使用真实 POSIX shell 和 Node，分别验证 Heroku 单命令字符串、Docker 参数数组、引号和退出状态。Windows 可用 `KUMA_TEST_SHELL` 指定 Git 安装目录中的 `usr/bin/sh.exe`。真实 Docker smoke check 和 Heroku 发布在 GitHub runner 执行。

## `exec: node server/server.js: not found` 排查

这个完整报错表示 shell 把 `node server/server.js` 当成了一个可执行文件名。Heroku 可把镜像的 CMD 合成单个命令字符串，而 Docker 本地运行通常传入可执行文件和独立参数。`exec-command.sh` 对前者使用 shell 解析，对后者保留参数边界；入口最后通过 `exec` 交接进程。配置值通过环境变量传递，不拼接到启动命令中。

出现该错误时，提交包含 `exec-command.sh`、Dockerfile、入口和部署脚本的修正，然后从最新分支重新 **Run workflow**，应用名填写原名称。重启 dyno 仍使用旧镜像；GitHub 的 **Re-run jobs** 也会继续使用原运行的旧提交。重新构建发布后才会使用修正。

官方依据：[Heroku 容器要求](https://devcenter.heroku.com/articles/container-registry-and-runtime)、[启动行为](https://devcenter.heroku.com/articles/dyno-startup-behavior)、[Aiven CA 与轮换](https://aiven.io/docs/platform/concepts/tls-ssl-certificates)、[GitHub Composite Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax#runs-for-composite-actions)。
