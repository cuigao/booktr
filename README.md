# booktr — 古早网站本地化翻译 agent

将无 JS/CSS 的静态网站镜像（如 90 年代末个人主页）逐页翻译成目标语言，输出**结构与原站完全一致**的镜像，并附带词汇表、翻译笔记、译者注等可溯源数据。

## 目录结构

```
src/                       # 项目根（发布单元 / git 仓库根 / 运行时根）
├── booktr/                # 全部源码包
│   ├── __main__.py        # python -m booktr 入口
│   ├── crawler.py         # 站点扫描：编码探测、链接图、日期提取
│   ├── planner.py         # 翻译顺序规划（确定性排序 + 可选 LLM survey 层）
│   ├── segments.py        # HTML 段切分 + 偏移映射 + 占位符 + 原位拼回
│   ├── llm.py             # LLM 适配（OpenAI 兼容接口 + mock）
│   ├── prompts.py         # 各类 prompt 模板
│   ├── translate.py       # 逐段翻译引擎（TM/词汇表/风格/上下文注入、检查点）
│   ├── glossary.py        # 词汇表
│   ├── notes.py           # 翻译笔记
│   ├── tm.py              # 翻译记忆
│   ├── styles.py          # 风格锚定（规则抽取 + 相似样例检索）
│   ├── review.py          # 交互审核队列
│   ├── qa.py              # 一致性 QA pass
│   ├── annotator.py       # 译者注生成
│   ├── phrases.py         # 短语记忆（导航短语精确复用）
│   ├── pipeline.py        # CLI 编排
│   └── config.py          # 配置加载与路径解析
├── booktr-cli.py          # 启动脚本（等效 python -m booktr）
├── config.json.template   # 配置模板（用户复制为 data/config.json）
├── requirements.txt       # 依赖（requests）
├── tests/                 # 测试
└── data/                  # 用户数据（完全 git 忽略，见 .gitignore，即默认数据根）
    ├── config.json        # 实际配置（从模板生成）
    ├── style_refs.json    # 风格对照样例（原文-某译者译文）
    ├── <站点镜像>/        # 待翻译的网站镜像目录
    ├── work/              # 中间数据（自动生成）
    └── out/               # 翻译后镜像（自动生成）
```

## 数据根目录（data_dir）与多站点

**config.json 与数据总是同处一处**（数据根目录内），数据根由 `--data-dir` 决定：

```
--data-dir CLI 参数（缺省为 <项目根>/data = src/data）
```

所有相对路径（`source_dir`、`output_dir`、`work_dir` 及 `work/...` 子路径）都相对数据根解析；config.json 不含 `data_dir` 字段。

- **默认数据根**：`src/data/`。不传参时 config 与所有数据落在 `src/data/` 下。
- **多站点独立工作区**：`--data-dir` 指向不同目录即可互不干扰，每个数据根有自己的 `config.json`、`work/`、`out/`：

  ```bash
  python booktr-cli.py --data-dir D:\siteA init
  python booktr-cli.py --data-dir D:\siteA translate
  python booktr-cli.py --data-dir D:\siteB init
  python booktr-cli.py --data-dir D:\siteB translate
  ```

- **源镜像在 src 之外**：`source_dir` 填完整绝对路径（如 `D:\mirrors\love.life.coocan.jp`），`_path` 对绝对路径原样返回、不做重定位；相对 `source_dir` 则相对数据根解析。
- `--data-dir` 为相对路径时相对项目根（`src/`）解析。

## 快速开始

```bash
cd src

# 1) 初始化：交互式生成 data/config.json（默认值见 config.json.template）
python booktr-cli.py init          # 或 python -m booktr init

# 2) 放入待翻译站点镜像，并确认 config.json 中 source_dir 指向它
#    默认: love.life.coocan.jp（相对数据根；可改绝对路径）

# 3) 扫描站点
python booktr-cli.py scan

# 4) 生成翻译顺序
python booktr-cli.py plan

# 5)（可选）LLM 语义分析辅助排序
python booktr-cli.py plan --survey

# 6)（可选）从语料抽取词汇表候选
python booktr-cli.py extract-terms

# 7)（可选）从 data/style_refs.json 提炼风格规则
python booktr-cli.py style-extract

# 8) 翻译（可指定页面）
python booktr-cli.py translate
python booktr-cli.py translate --pages today/today45.html
python booktr-cli.py translate --next              # 翻译下一个待译页（跳过 review 页）
python booktr-cli.py translate --next 5           # 翻译接下来 5 个待译页
python booktr-cli.py translate --next --dry-run   # 仅显示下一个待译页，不翻译

# 9) 处理审核队列（LLM 发现冲突/不确定时暂停在此）
python booktr-cli.py review

# 10) 一致性 QA
python booktr-cli.py qa

# 11) 生成译者注
python booktr-cli.py annotate

# 12) 导出镜像与静态资源
python booktr-cli.py export

# 13) 清理翻译缓存，从全新状态开始（保留 glossary）
python booktr-cli.py clean --all -y       # 全清（含 output）
python booktr-cli.py clean --reset -y     # 额外清理 plan.json + site_map.json
python booktr-cli.py clean                # 交互选择

# 14) 导出指定页面的完整 LLM 对话日志为 Markdown
python booktr-cli.py export-log today/today6.html
python booktr-cli.py export-log today/today6.html --sessions 1  # 只导出最近1次翻译任务
python booktr-cli.py export-log today/today6.html --task tsk_1755432600000  # 指定 task_id

# 15) 重新生成指定页面的 out 文件（从段索引离线重组）
python booktr-cli.py regenerate profile/profile.html
python booktr-cli.py regenerate --all  # 重新生成所有已处理页

# 查看进度
python booktr-cli.py status
```

`python booktr-cli.py <cmd>` 与 `python -m booktr <cmd>`（需在 `src/` 下）等效。所有子命令**幂等**、基于 `work/state.json`（相对数据根）断点续跑。

## 配置

配置来源优先级：`<数据根>/config.json`（用户配置）> 代码内 DEFAULTS。

- `python booktr-cli.py init`：交互式生成 `config.json`（询问站点目录、语言、LLM provider、增强工具等）
- 手动方式：复制 `config.json.template` 为 `<数据根>/config.json` 后编辑
- 关键配置项（除注明外，相对路径均相对数据根解析）：
  - `source_dir` 站点镜像目录（如 `love.life.coocan.jp`，相对数据根；**或填完整绝对路径指向 src 之外**）
  - `output_dir` 输出镜像目录（默认 `out`）
  - `work_dir` 中间数据目录（默认 `work`）
  - `lang.source/target` 源/目标语言代码（默认 `ja` → `zh-Hans`）
  - `llm.provider`：`mock`（离线测试）或 `openai-compatible`（真实 API）
  - `llm.base_url/model/api_key_env`：OpenAI 兼容服务接入参数
  - `llm.max_repair`：解析失败自愈重试次数（默认 3）
  - `llm.max_history_segments`：多轮对话保留历史段落数（默认 50）
  - `llm.summary_enabled`：摘要接力开关（默认 true）
  - `llm_logs.auto_export`：translate 完成后自动导出对话日志（默认 true）
  - `llm_logs.auto_export_sessions`：自动导出最近 N 个翻译任务（默认 1）
  - `review.auto_regenerate`：review 接受后自动重生成 out 页面（默认 true）
  - `llm.retranslate_context_chars`：重新翻译时前后文总字符数（默认 1000，每侧一半=500）
  - `llm.retranslate_use_summary`：重新翻译时使用页面摘要（默认 true）
  - `llm.auto_retranslate`：翻译需要 review 时自动用重翻译提示词再试（默认 true）
  - `llm.auto_retranslate_attempts`：自动重翻译尝试次数（默认 1）
  - `style.refs_path`：风格样例文件（用户自备，接口就绪）

## 核心机制

- **逐段拼接**：在原始解码文本上定位每个可翻译文字段的字符偏移，翻译后原位拼回。除被替换的文字外，标签、注释、`tppabs` 属性、空白等字节完全不变，保证"完全相同样式"。段索引（`work/segments/*.json`）记录 `页面/段ID/源偏移/译文/引文`，为译者注与未来的浏览器插件提供锚点。
- **编码**：逐文件探测（Shift-JIS 优先，失败回退 UTF-8）；输出统一 UTF-8 并在 `<head>` 补/改 `<meta charset>`（中文无法在 Shift-JIS 编码，这是唯一必要改动）。
- **全角字符保留**：保留原文中的全角写法——全角英文字母、全角数字（０-９）、
  全角符号（！？～・＆＊＝＋＜＞等）保持全角不转半角；几何符号（●○■）、
  省略号（…）、破折号（――）、智能引号（""''）保持原样。
- **占位符**：段内内联标签（`<img>/<font>/<a>…`）转为 `[[P0]]` 占位符交给 LLM，译文必须原样保留，拼接时还原。相邻 inline 标签（含纯空白分隔）合并为单个占位符，减少 LLM 困惑。短语记忆命中后从原始 chunk 恢复占位符。
- **解析自愈**：LLM 输出非法 JSON 时自动重试（最多 `max_repair` 次），每次携带具体错误信息让 LLM 修正；占位符丢失时触发额外 repair；兜底清理去除 `|TEXT|`/JSON 残渣。
- **多轮对话翻译**：页面内所有段落共享同一对话上下文，LLM 能保持术语与风格一致性。
  - **摘要接力**：达到 `max_history_segments`（默认 50）后自动生成摘要，重建对话继续翻译。
  - **短语记忆注入**：被短语记忆跳过的翻译结果注入到下一条 user message，保持 LLM 上下文。
- **重新翻译**：删除 review 条目后，该段落标记为 pending，下次 translate 时自动重新翻译。
  - **上下文窗口**：重新翻译时提供前文/后文已翻译内容（总 `retranslate_context_chars`，每侧一半），让 LLM 看到完整的"上-中-下"结构。
  - **页面摘要**：注入页面摘要，提供整体上下文。
  - **全新对话**：重新翻译时创建新对话（新 context_id），不受之前翻译历史影响。
  - **系统提示词强化**：注入"重新翻译任务"规则，强调完整翻译、术语一致、占位符保留。
  - **上下文差异**：段级重翻译（review 删除后触发）发生在段翻译起点，此时**后文尚未翻译**，故 `context_after` 通常为空；仅前文可用。页面级 auto-retranslate（见下）因整页译完，前后文均完整。
- **自动重翻译（auto-retranslate）**：翻译过程中需要 review 的 chunk（低置信度 / needs_human / 格式错误），
  在**整个页面主翻译结束后**统一用重翻译提示词再试（`auto_retranslate_attempts` 次）。
  此时全页段均已翻译，前后文上下文完整。成功则采用新译文并清除 review 标记（含已入队的词汇表冲突条目）；
  仍失败则保留结果并进入 review 队列。
- **LLM 异常防护**：所有 LLM 返回路径均有防护——None 内容检查、API 格式异常捕获、confidence null 防护、`parse_json_response` 空响应处理。
- **翻译顺序（统一加权模型）**：每页计算一组归一化指标分（`semantic` 层级语义序 / `has_semantic` / `is_index` / `is_orphan` / `hotness` 引用热度 / `depth` / `chrono` 日期 / `volume` 编号 / `nav` 导航位次 / `dfs` 遍历序 / `len` 原文长度），按**加权总分降序**排列。
  - **层级语义序**：递归发现各级索引页（root 的 `index.html`、`today0.html`、`photo0.html`、`rec_idx.html` 等，判定 = 链接覆盖本级成员比例 ≥ `index_threshold`），页面语义分 = 目录链上各级位置的级联，跨目录自然分层、组内按索引链接序连续。
  - **权重/方向/阈值全部可调**：`config.json` 的 `planner.weights/directions/index_threshold`，或命令行 `plan --weights '{"semantic":0.4}' --direction semantic:reverse --index-threshold 0.7`。
  - **结构绝对控制**：把 `has_semantic`/`is_index`/`is_orphan` 权重调高数量级，即可实现近似"结构硬序"；这些 0/1 指标专为绝对控制设计。
  - **手动调整**：`plan.json` 的 `order` 是最终顺序，可直接编辑；重新 plan 只更新 `auto_order`，`--reset-order` 才用自动序重置。
  - 所有指标分与排序证据（权重公式、每页各指标分、语义序来源）持久化在 `plan.json`，供 GUI 调权（v2）实时重算。
  - 上下文包把前 N 页摘要随页送入。
- **暂停与人工介入**：LLM 每段返回结构化结果（confidence/冲突/needs_human）；冲突或低置信度写入 `work/review_queue.json`，批量边界暂停请求人工。用户任何时刻可向 `work/inbox/` 写入 `.txt`/`.md`/`.json` 注入笔记或规则，下个检查点生效。
  - **审核条目操作**：`[a]`接受（保留译文）`[s]`跳过 `[d]`删除（清除该段译文，页面转 pending，`--next` 可重译）`[c]`确认加入词汇表 `[q]`退出。QA 条目（`qa_*` 原因）仅 `[a]`标记已处理，不改变页面翻译状态。
  - **页面状态流转**：页面有 open 审核项时 status=`review`，`--next` 会跳过；需处理完该页全部 open 项（或 `[d]` 使页面转 `pending`）后才会被 `--next` 重新翻译。删除段译文后，`translate --next` 只重译被删除的段，其余已译段保留。

## 增强工具

| 工具 | 说明 | 配置 |
|---|---|---|
| 词汇表 | 人工预置 + `extract-terms` LLM 自动抽取候选（需确认）；confirmed 条目默认 `read_only=true`，机械替换时跳过 LLM | `glossary.path` |
| 翻译记忆 TM | 双语片段缓存，跨页复用 | `tm.enabled` |
| 短语记忆 | 导航短语精确匹配复用；自动学习，写入前检查 glossary read_only 防覆盖 | `phrases.max_len` |
| 风格指南 | `style-extract` 从对照样例提炼规则注入 | `style.rules_enabled` |
| 风格锚定 | 字符 n-gram 相似度检索 top-k 样例 few-shot 注入 | `style.exemplar_enabled` |
| 上下文包 | 前 N 篇日记摘要 | `planner.context_window` |
| 一致性 QA | 校验术语一致与 HTML 安全 | `qa.deep_llm_check` |
| 译者注 | 跨页关联/趣味发现 → 外部 JSON | `annotate` |
| Session ID | 页面翻译任务标识（task_id）+ 多轮对话标识（context_id） | 自动生成，写入 LLM 日志 |

优先级：**词汇表 > 风格样例 > 风格规则**。

## LLM 接入

- `llm.provider: "mock"`：离线运行，返回确定性结果，用于验证管线与数据结构（无需 API key）。
- `llm.provider: "openai-compatible"`：通过 `base_url + api_key_env` 接入任意兼容服务（OpenAI / OpenRouter / vLLM / Ollama / LM Studio 等）。需设置 `base_url` 对应的环境变量（默认 `BOOKTR_API_KEY`）。

## 数据文件

下表路径均相对**数据根目录**（默认 `src/data/`）。

| 文件 | 内容 |
|---|---|
| `work/site_map.json` | 每页编码/标题/日期/链接图 |
| `work/plan.json` | 翻译顺序 + 全部指标分 + 权重公式 + 语义序来源（证据可追溯） |
| `work/glossary.json` | 词汇表（`status: confirmed / auto-candidate`） |
| `work/tm.jsonl` | 翻译记忆 |
| `work/phrase_memory.json` | 短语记忆（导航短语精确复用） |
| `work/notes.jsonl` | 翻译笔记（追加式，可溯源） |
| `work/translators_notes.json` | 译者注（锚定页面+偏移+引文） |
| `work/segments/*.json` | 每页段索引（源偏移↔译文↔引文） |
| `work/state.json` | 检查点 |
| `work/review_queue.json` | 待人工审核项 |
| `work/qa_report.json` | QA 报告 |
| `work/llm_logs/*.json` | LLM 调用日志（含完整对话历史、task_id、context_id） |
| `work/logs/*.md` | 导出的 Markdown 对话日志（自动或手动导出） |
| `out/` | 翻译后完整镜像 |

## Git 约定

`data/` 目录（用户数据与产物）已被 `.gitignore` 完全排除，不入库。代码与模板（`config.json.template`、`requirements.txt`、`README.md`、`booktr/`、`booktr-cli.py`、`tests/`）入库。

## 测试

```bash
python -m pytest tests/test_core.py -q
```

## 路线图

- **v1（当前）**：CLI 全流程 + 全部增强工具 + 双式风格锚定 + 可选 survey pass + mock/真实 LLM + 交互式 init。
- **v2**：`serve.py` Web 查看器——左右对照原文/译文，实时叠加词汇表/笔记/译者注；段索引锚点已就绪，无需返工。
