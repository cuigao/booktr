# qa-auto 探针 / 评估工具

`qa-auto`（监督式自动 QA 闭环）的**阶段 0 摸底与评估脚本**，留存备查。
正式实现见 `booktr/supervisor.py`、`booktr/prompts.py`（`build_supervisor_*`）、
`booktr/pipeline.py`（`cmd_qa_auto`）；评估报告见
`instance/report/qa_auto_probe_report.md`。

脚本复用 `booktr.supervisor` 的**正式**语境构建与提示词，保证"实验即实现"口径一致。

## 文件

| 文件 | 说明 |
|---|---|
| `probe.py` | 探针：`judge`（判官校准，只读回放）/ `e2e`（端到端 QA1→判官→apply→QA2） |
| `analyze.py` | 读取 e2e 结果，算**问题级复现率** / 裁决分布 / diff 段数，输出 `analysis.txt` |
| `_samples/analysis.txt` | 归档的一次运行分析结果（可读证据） |
| `_out/` | 运行输出（gitignore：结果 JSON 与 `logs/`） |

## 用法

`judge`（对实例中已有人工裁决的页回放判官；只读，不改实例）：

```bash
python tools/qa_auto_probe/probe.py judge \
    --data-dir ../../instance/data-deepseek-v4.1-flash \
    --pages profile/profile.html today/today13.html --resume
```

`e2e`（端到端；**须指向可写的沙箱副本**，会写 state/段缓存/out）：

```bash
# 先复制实例为沙箱（config 的 source_dir 改成绝对路径），再：
python tools/qa_auto_probe/probe.py e2e \
    --data-dir ../../temp/qa_auto_probe/sandbox \
    --pages today/today14.html today/today15.html --resume
python tools/qa_auto_probe/analyze.py \
    --result tools/qa_auto_probe/_out/e2e_result.json
```

`--data-dir` 相对路径相对**项目根 `src/`** 解析（与 CLI 一致）；结果默认写
`_out/`，可用 `--out`/`--out-dir` 覆盖。

## 评估方法（要点）

- **主指标｜问题级复现率**：`QA2` 是否重现 `QA1` 已采纳并 apply 的问题
  （**同段 + 问题文本相似度 ≥0.6**；仅"同段"会误报，见报告 §5.4）。
  复现率低 = 修复有效。QA2 新增问题单列，不混入主指标。
- **客观指标｜diff 复核**：apply 段前后 diff 应仅句内定点、无整段重写。
- **辅助｜人工参照一致率**：**仅对 `rejected` 条目公平**——判官以"当前译文"
  为依据，历史 `applied` 条目修正已在译文里，判官会（正确地）判 reject。
- **辅助｜主观评估**：结合对站点与源文的理解逐页评分（见报告 §4.3）。

## 已知陷阱（实现时已并入防线）

- 判官可能对"源文本本身含字面标签（如 `<!blockquote>`）"的错误建议照单全收，
  把 QA 幻觉写进输出 → 判官提示词已加"核对源文本本身"的 HTML 安全防线。
- 判官响应允许**值字符串内裸引号**，`supervisor._extract_verdict_dict` 兜底修复。
- 到云端 LLM 的连接层抖动会出现 `write/read timed out`；探针用短超时 +
  逐条容错 + `--resume` 续跑应对。
