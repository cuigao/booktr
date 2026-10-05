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
| `analyze.py` | `--result`：算 e2e **问题级复现率** / 裁决分布 / diff 段数；`--audit`：判官漂移审计；`--prod`：生产实例评估 |
| `_samples/analysis.txt` | 归档的 e2e 分析结果（可读证据） |
| `_samples/prod_analysis.txt` | 归档的生产实例评估结果（可读证据） |
| `_out/` | 运行输出（gitignore：结果 JSON、`drift_audit.txt` 与 `logs/`） |

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

`--prod`（对生产实例做只读评估，不改任何数据；QA2 默认取 `version≥2` 且
`checked` 最多的一份报告，可用 `--qa2 <ts>` 指定）：

```bash
python tools/qa_auto_probe/analyze.py --prod ../../instance/data-deepseek-v4.1-flash
```

输出 `_out/prod_analysis.txt`（同屏）：applied/rejected **问题级复现率**（按来源拆分 +
阈值 0.5/0.6/0.7 敏感性）、QA2 问题三分类（复现 / 新且落在已改段 / 新且他处）、
apply 前后 **diff 相似度分布**、残存 high 清单、qa-auto 运行汇总。

`--emit-category`（只读）按**问题类别**筛出 QA2 的 open 条目，产出 `qa-auto --only-ids`
可用的 id 白名单（用于"只对某类问题跑 qa-auto"）：

```bash
python tools/qa_auto_probe/analyze.py --emit-category ../../instance/data-deepseek-v4.1-flash \
    --category DE --out ../../instance/data-deepseek-v4.1-flash/work
```

类别 A–G：A 标点/全半角、B 术语/专名未译、C 前后不一致、D 措辞/语气/翻译腔、
E 漏译/语义偏移/增译、F 术语选词、G 星期日期数字（关键词启发式，A→G 取首个命中）。
输出 `qa2_DE_ids.json`（`["id",...]`，人工增删后交 `qa-auto --only-ids`）与
`qa2_DE_report.txt`（按类可读清单）。

`variant`（提示词变体实验，需**可写沙箱副本**）：对指定页按变体逐次运行 `translate`/`qa`/`judge`
任一端，详细记录命令、耗时、token。变体定义 `V0..V5`（policy×style×history）：

```bash
# QA 端：V0..V5 各 3 次
python tools/qa_auto_probe/probe.py variant --data-dir ../../instance/exp_prompt/tr \
    --end qa --pages welcome/welcome.html today/today12.html --variant V0 V1 V2 V3 V4 V5 --runs 3
# 翻译端（--style auto 用变体风格；每次运行前重置目标页）
python tools/qa_auto_probe/probe.py variant --data-dir ../../instance/exp_prompt/tr \
    --end translate --pages today/today12.html --runs 3
# 汇总
python tools/qa_auto_probe/analyze.py --variant tools/qa_auto_probe/_out \
    --ref-data-dir ../../instance/data-deepseek-v4.1-flash
```

结果写 `_out/variant_runs/<end>_<V>_run<N>.json` + `variant_summary_<end>.json`；
`analyze.py --variant` 汇总类别问题数、跨次稳定性、振荡敏感度、判官分布与成本。

`--transcripts`（可读对话，三端）：把各 run 的 translate/qa/judge 对话渲染为中文 Markdown——
结构化摘要（QA 逐条问题、judge 逐条 Q→A 卡、translate 每段译文）+ **逐调用记录
（user 摘要 / response / **thinking 全文**，`--thinking full|excerpt|none`，默认 full）**。

```bash
python tools/qa_auto_probe/analyze.py --transcripts tools/qa_auto_probe/_out \
    [--end translate|qa|judge] [--tvar V0 V4] [--trun 1 2] \
    [--thinking full] [--report-dir <dir>]
```
依赖 `log_index.json`（缺则自动 `link_logs`）。

`--embed-thinking`：把 `_out/logs` 的完整调用日志（含 reasoning/response/system/user/messages
全文）**回填进 `variant_runs/*.json` 的 `calls` 字段**，使运行 JSON 自包含（无需重跑模型）：

```bash
python tools/qa_auto_probe/analyze.py --embed-thinking tools/qa_auto_probe/_out
```

`--audit`（只读）审计判官日志，检测重置态调用 / 索引回显不符 / 响应错位：

```bash
python tools/qa_auto_probe/analyze.py --audit ../../instance/data-deepseek-v4.1-flash
```

输出到 stdout 与 `--out-dir/drift_audit.txt`。

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
