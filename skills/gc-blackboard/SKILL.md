---
name: gc-blackboard
description: >
  Geocache mystery-solving lane skill for a Muteki swarm. ALWAYS use this skill
  when Challenge.mode is geocache — before starting a new reading, cipher,
  projection, or lateral direction (read directives, review, then deadends),
  when you confirm a decoded intermediate or coordinate fact, and when a
  hypothesis is disproved. Use submit-coord, never submit-flag or FOUND_FLAG.
  Prefer coord_calc.py and gc show --json over inventing coordinates.
---

# GC 黑板（mystery 求解道）

你是 swarm 里的 **一条 mystery 求解道**，不是独自通关的选手。队友通过共享黑板协调。
本技能目录里的 `blackboard.py` 只是薄封装，真正协议在同级 `muteki-blackboard`。

## 行动前先读（最高价值纪律）

**任何新方向之前**，按这个顺序读，不要先动手：

```
python3 blackboard.py read-directives
python3 blackboard.py read-review
python3 blackboard.py read-deadends
python3 blackboard.py read-facts
python3 blackboard.py read-routes
python3 blackboard.py read-branches
```

`read-deadends` 是最高价值调用。死路表示一条 **解读 / 密码轴 / 投影假设已被证伪**。
已在死路或 Review 压制的轴，不要重试；除非出现新证据，否则不要 reopen suppressed route。

## 事实与死路

- **verified fact** 必须带真实命令或工具输出作 witness。解码后的中间态（明文、偏移、投影点）也是 fact。
- 尚未核对的坐标或解读写成 candidate，并保留来源。
- challenged fact 暂不作为已确认依据。
- 证伪一条解读轴时立刻标记死路：

```
python3 blackboard.py write-fact "Caesar+3 得到可读站名 Greenwich" --verified
python3 blackboard.py mark-deadend "Vigenere 密钥 GREEN 无法还原 posted 骨架"
```

## 角色先验

在死路允许的前提下，优先这三类互不抢道的先验：

- **cipher**：置换、编码、书码、日期/钟面、字母数字映射。
- **projection**：从 posted / 中间点按方位+距离投影、中点、网格换算。
- **lateral**：题面文字游戏、现场对象、非密码的阅读理解，而不是再套一层常见密码。

## 题目与坐标工具

```
gc show <GC> --json          # listing / hint / posted
python3 coord_calc.py project "<coord>" <bearing_deg> <distance_m>
python3 coord_calc.py midpoint "<coord-a>" "<coord-b>"
python3 coord_calc.py checksum "<text>"
python3 coord_calc.py convert "<coord>" --to dd
```

用工具输出，不要手算后用散文宣布坐标。

## 提交坐标（唯一提交口）

使用 `submit-coord`。**不要** 使用 `submit-flag` 或 `FOUND_FLAG`。

```
python3 blackboard.py submit-coord '<candidate>'
```

本地算出并被本机接受的 candidate **不会结束任务**。checksum 或 Muteki host 的 checker 核验通过后才算 verified。
散文、聊天或“我解出来了”都不能宣告完成。

## GeoCheck 限流锁

若 listing 带 `geocheck_url`，在 `submit-coord` 之前先打印 `READY_TO_SUBMIT=<coord>` 并独占领取，结束后释放。
**不要运行 gc check** — Muteki host 只提交一次。

```
python3 blackboard.py claim-resource "verifier:geocheck@<gc_code>" --risk-class rate-limited
# READY_TO_SUBMIT=<coord>
python3 blackboard.py submit-coord '<candidate>'
python3 blackboard.py release-resource "verifier:geocheck@<gc_code>"
```

`claim-resource` 打印 `LOST` 时不要并发提交。

## 会话阻塞

会话过期、需要登录或外部资源时，不要盲扫，输出：

```
NEED_INPUT=<操作员必须提供的一件具体事情>
NEED_KIND=external_blocker
```
