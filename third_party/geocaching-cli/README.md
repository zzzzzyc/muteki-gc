# geocaching-cli

个人用的 Geocaching.com 命令行：Playwright 打开官网登录（含 reCAPTCHA），然后 `gc show` / `gc search` / `gc logs`。

`live` 走非官方网页接口，只适合你自己的账号、小流量使用。

## 安装

Python 3.11+。

```bash
python -m venv .venv
.venv\Scripts\activate          # Unix: source .venv/bin/activate
pip install -e .
playwright install chromium
```

入口：`gc` 或 `geocaching-cli`。

## 登录

不要把账号写进仓库。

```bash
set GEOCACHING_USERNAME=你的用户名
set GEOCACHING_PASSWORD=你的密码
gc auth login
```

默认弹出 Chromium，自动填表并点 reCAPTCHA 勾选框。若出现图片题，在窗口里点完即可。成功后会话写到 `~/.geocaching-cli/session.json`（已 gitignore）。

之后同一台机器一般不用再登：

```bash
gc auth status --check
```

## 用法

```bash
gc show GC1PAR2
gc search --near "39.9042,116.4074" -n 10
gc search GC1PAR2
gc logs GC1PAR2 -n 10
gc finds -n 10
gc coord parse "N 39 54.252 E 116 24.444"
```

数据命令都有 `--json`。

## 命令

| 命令 | 说明 |
| --- | --- |
| `gc auth login` / `gc login` | Playwright 登录 |
| `gc show GCXXXX` | 官网详情 |
| `gc search --near …` | 官网附近搜索 |
| `gc logs` / `gc finds` | 日志 / 我的 found |
| `gc coord …` | 坐标工具 |
| `gc local …` | 本地 GPX/SQLite（次要） |
| `gc serve` | 本机回环 HTTP API（`/api/status`、`/api/show/<GC>`） |
| `gc check` | GeoCheck / Certitude 坐标校验（Playwright，默认无头） |

## 开发

```bash
pip install -e ".[dev]"
python -m pytest
```

默认测试不登录官网。要跑真实登录：

```bash
set GEOCACHING_LIVE_TEST=1
python -m pytest tests/test_live_playwright.py -m live -s
```
