# Muteki Geocache E2E 复盘

最后更新：2026-08-21

## 结论

`mode="geocache"` 已完成一次真实 Web 端到端闭环：

```text
Web dispatch
  → gc show listing bootstrap
  → SharedGraph verified facts
  → OpenCode worker 解题和真实命令输出
  → blackboard submit-coord
  → 本地 shape / anchor / provenance gate
  → candidate fact
  → operator 只确认既有 candidate
  → verified result projection
  → Coordinator solved / RUN_FINISHED
```

最终验证坐标为 `N 39 54.770 E 116 12.696`，距 posted 坐标约 222 米。该值先由
worker 通过 `submit-coord` 进入 candidate gate；operator 未直接创建坐标，只把图中既有
candidate 升级为 verified。

## 环境

| 项 | 值 |
|---|---|
| Muteki 分支 | `cursor/muteki-gc-geocache-mode-4ace` |
| GC CLI | root-owned 隔离环境 `/opt/muteki-gccli` |
| GC 会话 API | `http://127.0.0.1:8765/api/status` |
| Worker transport | OpenCode 1.18.19 |
| Worker model | `glm-5.2` |
| Worker endpoint | OpenAI-compatible custom endpoint（URL/key 不写入本文件） |
| Worker backend | local |
| 题目 | `GCBNQ1V`，D3 / T2 Mystery |

凭据仅存在于 gitignore 的 `.env` / gccli session 环境；事件、文档、提交和 artifact 不包含
API key 或 Geocaching cookie。

## Run 1：无解法提示基线

- Run：`run-0001`
- 预算：900 秒
- 结果：`budget_exhausted`，未产生 candidate
- bootstrap：
  - 真实执行 `gc show GCBNQ1V --json`
  - 注入 posted、名称/D-T、listing、hint、coord skeleton 共 5 类 verified facts
- Worker：
  - 启动 OpenCode / GLM worker
  - 读取 directives、review、deadends、facts
  - 识别 Wingdings 并开始构造映射
  - stall reclaim 后重派 explore worker
- 消耗（事件中的 Worker totals）：
  - 约 256,288 tokens
  - 约 USD 0.021624

该 run 证明 bootstrap、图、worker transport、tool events、stall reclaim 和 budget shutdown
都能正常工作；没有为了演示而伪造 candidate。

## Run 2：方法引导 + candidate/verify 闭环

- Run：`run-0002`
- 时长：约 862 秒
- 结果：`solved=true`，`reason="solved"`
- 消耗（最终两个活跃 worker 的 totals）：
  - 约 509,059 tokens
  - 约 USD 0.043450

### 关键事件

| Seq | 事件 |
|---:|---|
| 3 | `gc_bootstrap_complete`，5 个初始 facts |
| 208–209 | worker 调 `blackboard.py submit-coord`，API 不回显 candidate |
| 210 | `coord_candidate`：本地 shape/anchor gate 通过，距 posted 222 米 |
| 212 | candidate fact：`source=gc_gate`、`verified=false`、有 witness |
| 222–225 | operator `verify_coord` 命令持久化/路由 |
| 226 | `coord_found`，`verification=operator` |
| 227 | Insight `FlagFound`，actor 为 `operator-coordinate-verifier` |
| 228 | control effect observed |
| 243 | verified result 同步到共享结果投影 |
| 247 | `run.finished`，`solved=true` |

### Operator 权限边界

`verify_coord` 只接受同时满足以下条件的值：

- 图中已有 exact normalized candidate；
- candidate 来源为 `gc_gate`；
- candidate 仍为 unverified；
- candidate 有 worker gate witness；
- 未被 operator 标为 false。

因此 operator 文本本身不是证据，也不能用任意坐标绕过 worker provenance。控制命令 journal
和 `flag_found` actor 构成审计链。

## 解题结果与评测口径

并行 Grok 解题用于开发验证：

| 题目 | 结果 |
|---|---|
| `GCBQRNY` | 得到高置信完整坐标，距 posted 约 1092 米 |
| `GCBNQ1V` | 得到高置信完整坐标，距 posted 约 222 米；用于 E2E |
| `GC71C4T` | 证据不足，保持 UNSOLVED，没有生成 candidate |

重要限制：三个辅助解题 agent 与本地 Muteki worker 运行在同一 VM。`run-0002` 的一个 worker
看到了 `/tmp/gc-solves/GCBNQ1V` 的辅助产物，随后又用 Ghostscript Wingdings 映射、Base64、
leet/Tzolkin 和独立距离命令复核。故 `run-0002` 可以证明**产品工作流和 gate**，但不能作为
严格隔离条件下的 solve-rate 样本。正式求解率评测必须把辅助产物放到另一台隔离机器，或在
worker 启动前清空不可见的临时目录。

## 死路共享效果

- worker 每轮都先读 review/deadends/facts，协议执行正确；
- operator directive 将旧路线标为 superseded，后续 worker 能看到该 dead end；
- 这次题目的主要瓶颈是 Wingdings 映射工具和模型长上下文，而不是重复的假设路线；
- 两个 worker 仍做了较多重复映射工作，因此本次无法声称死路共享显著节省 token。

## Gate 误判 / 漏判

- 真实 candidate：
  - slot 合成成功；
  - 规范化为 DMM；
  - anchor 距离 222 米；
  - exact candidate 出现在真实 tool output；
  - candidate 正确保持 `verified=false`，未提前结束。
- operator verify 后只产生一个 verified result；没有重复结果。
- 本次未观察到格式或距离 gate 的误判/漏判。
- 没有 checker URL / listing checksum，因此自动 verified 路径未在该题实测；其离线 fixture、
  SSRF/redirect、并发序列化和结果绑定由自动测试覆盖。

## E2E 发现并修复的问题

1. **Endpoint credential precedence**
   - 带真实 `.env` 跑全量测试时，health probe 的 ambient `OPENAI_API_KEY` 覆盖显式
     `file:` credential；
   - 修复为 profile-resolved credential 显式覆盖 ambient env。
2. **系统 Python 下 coord_calc 导入**
   - local worker 用 `/usr/bin/python3 skills/gc-blackboard/coord_calc.py` 时，源码 checkout
     的 repo root 不在 `sys.path`；
   - 修复为解析真实 skill 路径并发现 canonical vendored coordinate core；无 symlink
     文件系统的 copy fallback 同时 staging vendor。

## Walkthrough artifacts

- `/opt/cursor/artifacts/muteki_geocache_live_run.mp4`
- `/opt/cursor/artifacts/geocache_dispatch_form.webp`
- `/opt/cursor/artifacts/geocache_live_event_stream.webp`
- `/opt/cursor/artifacts/muteki_geocache_solved_run.mp4`
- `/opt/cursor/artifacts/geocache_solved_result.webp`

