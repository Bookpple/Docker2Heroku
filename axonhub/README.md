# AxonHub 部署到 Heroku

使用已经成功部署的方案：从官方镜像复制 `/app/axonhub`，以 Debian 为运行层，启动时把 Heroku 的 `PORT` 映射到 `AXONHUB_SERVER_PORT`。默认固定到已验证的上游镜像 digest，也支持选择其他 tag 或 digest。

## 从零部署

1. 将本目录及 `.github/workflows/deploy_axonhub.yml` 提交到你的 `docker2heroku` 仓库，保持相对路径。
2. 在 GitHub 仓库 **Settings → Secrets and variables → Actions → Secrets** 添加：
   - `HEROKU_API_KEY`：有权限创建或更新目标应用的 Heroku API Key。
   - `AXONHUB_DB_DSN`：远程数据库 DSN，例如 `postgres://USER:PASSWORD@HOST:PORT/axonhub2?sslmode=require`。URL 密码中的特殊字符需要 URL 编码。
3. 在 **Actions → Deploy AxonHub to Heroku → Run workflow** 填写应用名，保留“应用不存在时创建”。已存在且账号有权限访问的应用会直接更新，后续不必取消该选项。
4. 按账户支持的套餐填写 `dyno_type`，如 `eco` 或 `basic`；留空保留现有或 Heroku 默认类型。启动 dyno 使用你的 Heroku 套餐，账户需要对应资格。
5. 执行 workflow。成功时显示 `Deployment health check passed`，运行摘要给出应用名和 URL。首次访问 AxonHub 完成系统初始化。

外部数据库必须事先存在，账号须能在目标 schema 建表，并能修改其拥有的 AxonHub 表。Action 不创建外部数据库，也不自动修改数据库权限。新部署缺少 DSN 会在构建/创建应用前失败；更新时 `db_dsn` 留空沿用现有 Heroku DSN。`db_dialect` 默认 `postgres`，其他数据库需要相应修改。

## 在其他仓库复用

这些文件发布到 `Bookpple/docker2heroku` 的 `main` 分支后，可以这样使用，无需在调用方 checkout：

```yaml
name: Deploy AxonHub
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: Bookpple/docker2heroku/axonhub@main
        with:
          heroku_api_key: ${{ secrets.HEROKU_API_KEY }}
          heroku_app_name: my-axonhub-app
          create_new_app: 'true'
          db_dsn: ${{ secrets.AXONHUB_DB_DSN }}
          heroku_config_vars: |
            AXONHUB_LOG_LEVEL=info
            AXONHUB_SERVER_NAME="My AxonHub"
          heroku_config_vars_json: ${{ secrets.HEROKU_CONFIG_VARS_JSON }}
```

应用名须全球唯一，为 3–30 个小写字母、数字或连字符，开头为字母、末尾不能是连字符。留空且允许创建时自动生成 `axonhub-xxxxxx`，记下摘要中的名称用于后续更新；每次留空都会准备新名称。

## 输入参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `heroku_api_key` | 必填 | 从 Secrets 传入 |
| `docker_image` | 已验证的 digest | 含 `/app/axonhub` 的上游镜像，可换版本或 `latest` |
| `runtime_image` | `debian:bookworm-slim` | 支持 `apt-get` 的 Debian 兼容镜像 |
| `heroku_app_name` | 空 | 创建时可自动生成，更新需指定名称 |
| `create_new_app` | `false` | 为 `true` 时只创建不存在的应用，已存在则复用 |
| `heroku_region` | `us` | 新建区域：`us` / `eu` |
| `heroku_team` | 空 | 新建时所属团队 |
| `dyno_type` | 空 | 保留现有或默认套餐，填写则设置该套餐 |
| `web_dynos` | `1` | 正整数，套餐需支持对应数量 |
| `db_dialect` | `postgres` | PostgreSQL 别名、`mysql` / `tidb` |
| `db_dsn` | 空 | 新建需提供，更新留空沿用现有值 |
| `log_level` | `info` | 日志等级 |
| `log_encoding` | `console` | `console` / `json` |
| `heroku_config_vars` | 空 | 每行 `KEY=VALUE`，或一行多个带引号的键值对 |
| `heroku_config_vars_json` | 空 | JSON 对象，值为字符串或 `null` |
| `dockerhub_username` / `dockerhub_token` | 空 | Docker Hub 凭证，须一起填写 |
| `wait_for_health` | `true` | 检查当前 release 的 web 实例及 HTTP 健康 |
| `healthcheck_path` | `/health` | 相对于 `AXONHUB_SERVER_BASE_PATH` 的路径 |
| `healthcheck_timeout` | `180` | 超时秒数：10–1800 |
| `show_logs` | `true` | 成功或失败后打印最近日志 |
| `log_lines` | `200` | 日志行数：1–1500 |

附带 workflow 的常用参数在 Run workflow 填写；其余可修改 `with`，或设置示例对应的 Repository Variables：`HEROKU_TEAM`、`AXONHUB_DB_DIALECT`、`AXONHUB_LOG_LEVEL`、`AXONHUB_LOG_ENCODING`、`AXONHUB_HEALTHCHECK_PATH`、`AXONHUB_SHOW_LOGS`、`AXONHUB_LOG_LINES`。更新已有应用不会改变区域或团队。

## 环境变量与 Secrets

覆盖顺序：现有 Heroku 配置 → 日志/数据库专用输入 → `heroku_config_vars` → `heroku_config_vars_json`。未指定的键保留原值，日志默认输出到 `stdio`。

文本配置支持两种写法，可以混用；重复键以后面的值为准：

```text
AXONHUB_DB_DIALECT="postgresql" AXONHUB_LOG_LEVEL="info" AXONHUB_LOG_OUTPUT="stdio"
AXONHUB_SERVER_NAME=My AxonHub
```

一行多个键值对兼容原版 Action，按 shell 引号规则分词，但不执行 shell 或展开 `$` / 命令替换；值含空格时加引号。每行单个键值对仍按第一处 `=` 分割，支持未加引号的空格值、空值、注释和可选外层引号。值含字面量 `KEY=VALUE` 片段时需加引号或使用 JSON，避免被当成另一个键。多行值、保留首尾空格的值，或删除变量时使用 JSON：

```json
{
  "AXONHUB_LOG_LEVEL": "debug",
  "CUSTOM_MESSAGE": "a value with spaces, $ and =",
  "OLD_VARIABLE": null
}
```

敏感配置放入 `HEROKU_CONFIG_VARS`（文本）或 `HEROKU_CONFIG_VARS_JSON`（JSON）Secret，不要填写公开的 Run workflow 参数。配置通过 Heroku JSON API 更新，不打印配置响应或值；已知凭证在诊断日志中脱敏。

`PORT`、`AXONHUB_SERVER_PORT`、`AXONHUB_SERVER_HOST` 由启动入口管理，不允许额外配置覆盖。构建固定为 `linux/amd64`，发布前用非 root 用户验证动态端口。

输出为 `app_name`、`app_url`、`image`、`image_id`，例如 `${{ steps.deploy.outputs.app_url }}`。

## 检查和限制

- 需要 Ubuntu/Linux runner、Docker 和 Python 3；必要时自动安装 Heroku CLI。
- Smoke check 不连接真实数据库；数据库连接和迁移由实际启动及健康检查确认。
- 健康检查失败令 Action 失败并打印日志，不自动回滚镜像或数据库迁移。
- `wait_for_health=false` 仅表示发布命令成功，不能据此判断应用启动成功。
- 默认 digest 来自已成功的部署，不自动升级。升级时修改 `docker_image` 并查看 smoke check 及健康检查结果。

从仓库根目录执行本地逻辑测试：`python -m unittest discover -s axonhub/tests -v`。Docker/Heroku 检查在 Linux runner 执行。

官方依据：[GitHub Composite Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax#runs-for-composite-actions)、[Heroku 容器要求](https://devcenter.heroku.com/articles/container-registry-and-runtime)、[Heroku Config Vars API](https://devcenter.heroku.com/articles/platform-api-reference#config-vars-update)。
