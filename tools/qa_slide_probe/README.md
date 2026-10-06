# QA 滑窗覆盖实验探针

对比 `qa` 的**整页单次送入**与**滑窗分块**两种送文的**问题覆盖率 / 精度 / 成本**，
为"是否把滑窗落地到 `booktr.qa.run_qa`"提供数据。**只读**，不改实例数据。

背景：当前 `booktr.qa.run_qa` 每页仅 **1 次** LLM 调用，把整页原文/译文各截断到
`[:6000]` 后一次送入（`booktr/qa.py:120-126`）。实验检验"同款无段号文本改按
滑窗切块是否提高问题覆盖"，且不引入段号（沿用无段号整块文本 + 机械定位）。

## 文件

| 文件 | 说明 |
|---|---|
| `probe.py` | `coverage`：跑 whole / win\<N\> 各条件 ×N 轮，记录问题、reasoning、token、耗时；`sizes`：列每页段数 |
| `analyze.py` | 聚类各条件×轮次问题，算覆盖率 / 独有问题 / 成本，输出 `coverage_stats.txt` |
| `_out/` | 运行输出（gitignore：结果 JSON、`coverage_stats.*`、`logs/`） |

评估报告见 `instance/report/qa_slide_experiment.md`。

## 用法

```bash
# 跨尺寸自动选 10 页（按已翻译段数分位），每条件 3 轮
python tools/qa_slide_probe/probe.py coverage \
    --data-dir ../../instance/data-deepseek-v4.1-flash --auto-pages 10 --runs 3

# 指定页面与滑窗尺寸
python tools/qa_slide_probe/probe.py coverage \
    --data-dir ../../instance/data-deepseek-v4.1-flash \
    --pages disco/disco.html today/today15.html welcome/welcome.html \
    --windows 1500 2500 --overlap 2 --runs 3 --resume

# 汇总
python tools/qa_slide_probe/analyze.py \
    --result tools/qa_slide_probe/_out/coverage_result.json
```

- **只读**：不写实例任何文件；LLM 日志隔离到本工具 `_out/logs`。
- `--data-dir` 相对路径相对**项目根 `src/`** 解析（与 CLI 一致）。
- `--mock`：用 mock LLM 冒烟测试（不消耗配额、无真实问题）。
- `--resume`：跳过 `coverage_result.json` 中已完成的页；逐页增量落盘。

## 条件口径

- **`whole`**：完整复刻 `run_qa` 的 LLM 送文（拼接 + `[:6000]` 截断，1 次调用）；
  占位符/术语等本地确定性检查复用 `run_qa`（临时关 `deep_llm_check`），与生产一致。
- **`win<N>`**：把**已翻译段**按累积**源文字符**预算 N 切窗，相邻窗口**重叠
  `--overlap` 段**（默认 2）保留邻接语境；每窗一次 LLM 调用；合并后按
  `(段号集合, 归一 reason)` 去重。
- 提示词仍为**无段号整块文本**（复用 `prompts.build_qa_user`）；每窗问题复用
  `qa.locate_segments` 对**整页**段列表定位，故段号口径与生产一致。

## 指标（`analyze.py`）

对所有条件×轮次的问题做**聚类**（同段 + 文本相似度 ≥ `--threshold`，默认 0.6），
再统计：

- **簇覆盖**：各条件命中的簇数 / 全部簇数（多轮并集）。
- **独有 (vs 整页)**：滑窗命中而整页从未命中的簇数（核心问题：滑窗是否带来新覆盖）。
- **成本**：调用数/轮、耗时/轮、prompt/completion token/轮、reasoning/轮、loop 数。
- **每页**：簇数、整页命中数、滑窗独有数。

> 精度说明：滑窗可能提高覆盖也带来噪声。`analyze` 只做覆盖/成本对比；如需精度
> 复核，可对"滑窗独有"问题用既有判官（`tools/qa_auto_probe/probe.py judge`）抽样
> 裁定。
