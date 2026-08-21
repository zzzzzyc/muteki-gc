# Muteki GC 模式实施计划

> 状态：执行中。最后更新：2026-08-21。
>
> 目标：新增 `mode="geocache"`，复用产品 Coordinator、SharedGraph、Review、
> Operator 控制和 Web 指挥台；CTF `flag_ok` 与 pentest 产品路径保持不变。

## 范围

仅支持 D3–D4 Mystery，且最终答案可确定为以下形式之一：

- `AAA BBB` / `AAA-BBB`：填入 `coord_skeleton` 的两个三位槽；
- `+AAA-BBB`：相对 posted 坐标分别增加 `AAA/1000` 纬度分、减少
  `BBB/1000` 经度分；
- 完整 DD / DMM / DMS 坐标。

最终坐标必须在 posted 坐标 `anchor_radius_m`（默认 3200 米）以内。不支持无锚点
自由坐标题、Wherigo 逆向和必须依赖现场信息的题目。

## 仓库和分支

- Muteki：`zzzzzyc/muteki-gc`，
  `cursor/muteki-gc-geocache-mode-4ace`
- GC CLI：`zzzzzyc/geocaching-cli`，
  `cursor/geocache-api-build-4ace`

`geocaching-cli` 继续保持独立仓库。Muteki 的 Cloud 安装脚本从公开仓库安装已发布
接口；本地开发可用 editable install。

## 安全和凭据

本计划不记录任何真实凭据值。两个仓库都是公开仓库，真实 token 进入 Git 会立即泄露。
用户在会话中授权的凭据只按下列落点使用：

| 用途 | 变量/文件 | 规则 |
|---|---|---|
| OpenAI 兼容中转站 | `OPENAI_API_KEY`、`OPENAI_BASE_URL=https://api.zzzzzyc.top/v1` | 仅进进程环境或 gitignore 的 `.env` |
| Worker 模型 | `MUTEKI_WORKER_MODEL` | 首选中转站可用模型；OpenCode transport |
| Geocaching 会话 | `GEOCACHING_COOKIE` / `~/.geocaching-cli/credentials.json` | 文件权限 0600，不进仓库 |
| HTTP API | `GC_SERVE_TOKEN` | 可选，本机回环接口的 Bearer token |

任何日志、测试 fixture、文档、提交和 PR 都不得包含真实 key、cookie 或 Authorization
头。E2E 记录只写凭据是否配置和命令退出状态。

## 正确性模型

### Candidate 门

`coord_ok()` 负责：

1. 候选数字必须出现在 worker 的真实工具输出或真实 artifact；
2. 候选可与骨架合成，或可解析为完整坐标；
3. 坐标合法，且距 posted 锚点不超过半径。

通过以上三条仅表示 `accepted=True`。Candidate 作为 candidate fact 写入共享图，不结束
run。

### Verified 门

只有以下任一条件成立才 `verified=True`：

- `digit_checksum` 与规范化完整 DMM 坐标的所有数字之和一致；
- `gc check` 的真实工具输出包含绑定到同一规范化坐标的成功 receipt。

只有 verified 坐标进入共享结果投影和 Coordinator 结束条件。普通聊天、operator 指令、
listing 文本和未经工具来源核验的 `ok=true` 均不能升级。

## 任务

### Task 0：环境和基线

- Linux Cloud VM 作为正式执行环境，Windows 仅保留兼容性说明；
- Muteki：`uv run --extra dev python -m pytest -q tests/test_mock_solver.py tests/test_shared_graph.py`；
- GC CLI：`uv sync --extra dev && uv run python -m pytest -q`；
- 验证 OpenCode transport 可接 OpenAI 兼容 endpoint。

### Task 1：GC CLI 本机 HTTP API

- 新增 `geocaching_cli.server`；
- `GET /api/status`：配置、会话、在线状态、60 秒 TTL；
- `GET /api/show/<gc_code>`：`CacheRecord.to_dict()`；
- 仅允许 `127.0.0.1` / `::1`，可选 Bearer token；
- 所有业务异常返回 HTTP 200 JSON，未知路径仍返回 404；
- 注册 `gc serve`，增加单元和 HTTP 集成测试。

### Task 2：Challenge 和 Web mode

- `Challenge.mode` 增加 `geocache`；
- 增加 `gc_code`、posted 坐标、骨架、checksum、checker URL、半径；
- 修正后端 infer/standby/parse 白名单；
- 前端 Dispatch 类型、事件归约、输入表单和结果标签支持 GC；
- 默认仍为 CTF，空 `swarm_class` 仍解析到产品 `Swarm`。

### Task 3：坐标门和提交链

- 新增 `muteki.solver.gc_gate`；
- `submit-coord` 使用独立请求字段和 mode 检查；
- Candidate 记录为共享 candidate fact；
- Verified 坐标通过独立 `_accept_coordinate` 进入结果投影；
- CTF `_flag_ok`、`submit-flag` 和 `_accept_flag` 不改语义；
- 覆盖三种候选、距离、溯源、checksum、重复和伪 receipt 测试。

### Task 4：GC worker 工具和提示词

- 新增 `skills/gc-blackboard/`，保留“先读死路”的纪律；
- 提供 `submit-coord`、候选事实、解读死路和 verifier resource lock 指引；
- `coord_calc.py` 薄包装 gccli coordinate API；
- 在 `cli_solver.py` 的实际 prompt 分支加入 cipher / projection / lateral 先验；
- 不接入实验框架或修改产品默认 Swarm。

### Task 5：GeoCheck / Certitude

- 新增 `geocaching_cli.checker` 和 `gc check`；
- 从 `autoGC` 迁移选择器和 GeoCheck MD5 captcha 逻辑；
- 解析函数使用离线 HTML fixture；
- Playwright 提交默认 headless，可 `--headed`；
- 输出包含站点、规范化坐标、attempts 和不可伪造的进程内 receipt 字段；
- worker 提交前 claim `verifier:geocheck@<gc_code>`，并复用
  `verifier_rate_limited`。

### Task 6：会话 watchdog

- 新增 `gc_session_watchdog`；
- 仅 geocache mode 启动；
- `/api/status` 为 `session_expired` 时发 `HITL_REQUEST`，
  `need_kind="external_blocker"`；
- 状态恢复后发 resolved 事件，去重重复告警；
- Coordinator 所有退出路径 cancel 并 await task。

### Task 7：bootstrap、UI 和 E2E

- 新增 `gc_bootstrap`，调用 `gc show --json`；
- listing、hint、posted、D/T、骨架/checksum 作为带 witness 的初始事实；
- Web 表单可直接输入 GC code 和可选骨架/checker 信息；
- 用 `GCBQRNY`、`GCBNQ1V`、`GC71C4T` 中可访问且符合范围的题目做真实冒烟；
- 至少一次完整 `dispatch → worker → submit-coord → gate → verified → finish`；
- 结果写入 `docs/GC_E2E.md`，不得记录答案之外的凭据。

## 测试与验收

每个行为先写失败测试并确认 RED，再实现到 GREEN。最终必须运行：

```bash
# geocaching-cli
uv run python -m pytest -q

# muteki-gc
uv run --extra dev python -m pytest -q

# Web UI
cd apps/web/ui
npx tsc --noEmit
npm run build
```

再启动 `./run.sh web`，用浏览器完成 geocache dispatch、事件流、候选/verified 结果和
HITL 状态的手动测试，并保存一段成功视频。真实 checker 提交必须遵守资源锁和站点限流。

