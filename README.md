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
# 可选：同时导入个人偏好文件（user_rules/glossary/style_refs，见「偏好文件」）
python booktr-cli.py init --prefs pref/booktr-prefs.json

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
python booktr-cli.py qa --pages today/today0.html      # 限定页面
python booktr-cli.py qa --start 1 --count 10           # 按 plan.order 从第 1 篇起检查 10 篇
python booktr-cli.py qa --start 11 --count 10          # 下一批（无状态，按序推进）
python booktr-cli.py qa --no-deep                      # 仅本地规则，跳过 LLM 深度检查

# 10b) QA 裁定与应用（采纳后定点重译）
python booktr-cli.py qa-review                 # 逐条检阅 QA 意见（含原文/现译/上下文），[a]采纳/[r]拒绝/[d]丢弃
python booktr-cli.py qa-apply                  # 对已采纳意见批量定点重译
python booktr-cli.py qa-apply --page index.html
python booktr-cli.py qa-apply --dry-run        # 仅列出将修正的段

# 11) 生成译者注
python booktr-cli.py annotate

# 12) 导出镜像与静态资源
python booktr-cli.py export

# 13) 清理翻译缓存，从全新状态开始（保留 glossary）
python booktr-cli.py clean --all -y       # 全清（含 output）
python booktr-cli.py clean --reset -y     # 额外清理 plan.json + site_map.json
python booktr-cli.py clean                # 交互选择

# 14) 导出个人偏好文件（user_rules/glossary/style_refs；供 init --prefs 复用）
python booktr-cli.py export-prefs pref/booktr-prefs.json

# 15) 导出指定页面的完整 LLM 对话日志为 Markdown
python booktr-cli.py export-log today/today6.html
python booktr-cli.py export-log today/today6.html --sessions 1  # 只导出最近1次翻译任务
python booktr-cli.py export-log today/today6.html --task tsk_1755432600000  # 指定 task_id

# 16) 重新生成指定页面的 out 文件（从段索引离线重组）
python booktr-cli.py regenerate profile/profile.html
python booktr-cli.py regenerate --all  # 重新生成所有已处理页

# 17) 重置指定页面或段，使下次 translate 重新翻译
#     同时自动清理该页/该段对应的翻译记忆(TM)与翻译笔记(notes)，避免重译时旧译文/旧说明
#     经检索注入形成自我锚定；--keep-tm / --keep-notes 可分别保留
python booktr-cli.py reset today/today4.html              # 整页重置（全新翻译）
python booktr-cli.py reset today/today4.html --segments 16  # 只重置段16（保留其他段）
python booktr-cli.py reset today/today4.html --segments 16 --keep-tm  # 保留 TM
python booktr-cli.py reset --all -y                        # 重置所有页面

# 18) 列出译文残留的源语言片段（当前仅日语·平假名；只读，不改状态）
python booktr-cli.py check-residual              # 扫描所有已译页，逐项给出 reset 命令
python booktr-cli.py check-residual --pages today/today3.html
python booktr-cli.py check-residual --no-report  # 只打印，不写 work/residual_report.json
# 人工核验清单后，执行该项给出的 reset 命令，再 translate 即可重译该段
# （reset 命令会自动带上本次命令所用的 --data-dir，避免跑错数据根）：
#   python booktr-cli.py --data-dir ../instance/x reset today/today3.html --segments 4
#   python booktr-cli.py --data-dir ../instance/x translate --next

# 查看进度
python booktr-cli.py status

# 添加词汇表条目（--purge-keyword 可清理 TM/notes 中含该错误译法关键词的条目）
python booktr-cli.py add-term HOME 首页 --note "导航入口"

# 审计已翻译段落，用新词汇表/短语记忆替换
python booktr-cli.py audit-terms                    # 审计所有已翻译页面
python booktr-cli.py audit-terms today/today4.html  # 审计指定页面
python booktr-cli.py audit-terms --dry-run          # 只显示不修改

# 修复无法解码的输入文件（输出到 fix 目录，不改原始文件）
python booktr-cli.py fix                    # 修复无法解码的 html → data/fix/
python booktr-cli.py fix --dry-run          # 仅列出需修复文件
python booktr-cli.py fix --all              # 复制全部文件，fix 目录可直接作新源
```

`python booktr-cli.py <cmd>` 与 `python -m booktr <cmd>`（需在 `src/` 下）等效。所有子命令**幂等**、基于 `work/state.json`（相对数据根）断点续跑。

## 配置

配置来源优先级：`<数据根>/config.json`（用户配置）> 代码内 DEFAULTS。

- `python booktr-cli.py init`：交互式生成 `config.json`（询问站点目录、语言、翻译风格、LLM provider、增强工具等）；`--prefs <file>` 可同时导入个人偏好文件
- 手动方式：复制 `config.json.template` 为 `<数据根>/config.json` 后编辑
- 关键配置项（除注明外，相对路径均相对数据根解析）：
  - `source_dir` 站点镜像目录（如 `love.life.coocan.jp`，相对数据根；**或填完整绝对路径指向 src 之外**）
  - `output_dir` 输出镜像目录（默认 `out`）
  - `work_dir` 中间数据目录（默认 `work`）
  - `lang.source/target` 源/目标语言代码（默认 `ja` → `zh-Hans`）；所有 prompt 通过 `lang_name()` 映射为人类可读名称（`zh-Hans` → "简体中文"），配置代码与提示词一致
  - `llm.provider`：`mock`（离线测试）或 `openai-compatible`（真实 API）
  - `llm.base_url/model`：OpenAI 兼容服务接入参数
  - `llm.api_key` / `llm.api_key_env` / `llm.api_key_required`：API key 提供方式（见下）
  - `llm.max_tokens`：单次回复的 token 上限（默认 131072）。**推理模型**（如 deepseek-v4.1 系列）会先输出大量 `reasoning` token，上限过低会导致正文为空（`finish_reason=length`），故默认放宽
  - `llm.max_tokens_ceiling`：当正文因 reasoning 被截空时，自动翻倍 `max_tokens` 重试一次的上限（默认 524288）
  - `llm.stream`：流式输出（默认 true）。流式下每个分块都会重置读取超时，**长思考不再被误判为网络超时**；不支持流式的服务设 false
  - `llm.connect_timeout`：流式建连超时（默认 20s）
  - `llm.reasoning_effort`：推理模型思考等级（OpenAI 规范字段）。**空字符串 = 不发送该字段**（用模型默认，通常 `high`）；可设 `none`/`low`/`high`/`max`（以服务支持值为准）。**命令级覆盖**：`qa.reasoning_effort` 非空时覆盖全局，仅对 QA 生效；为空则继承 `llm.reasoning_effort`。
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

### 偏好文件（跨实例复用个人偏好）

个人偏好（翻译规则、词汇表、风格样例）默认**分散在各实例的 data 目录**，重建实例后容易遗漏。可用**偏好文件**保存并快速恢复：

- **导出**：`python booktr-cli.py --data-dir <数据根> export-prefs pref/booktr-prefs.json`
  - 写入指定文件路径（必须显式给出文件名），内容仅含 `user_rules`、`glossary`（全部条目）、`style_refs`。
  - **不含**数据路径、模型、语言、API key 等实例专属配置。
- **导入**：`python booktr-cli.py --data-dir <数据根> init --prefs pref/booktr-prefs.json`
  - 仅在 `init` 时导入；生成 config 后写入 `user_rules`，并把 `glossary`/`style_refs` 落入实例。
  - 不指定 `--prefs` 时不导入任何偏好。
- 偏好文件建议放在仓库外（如工作区 `pref/`），**不入库**——它是个人偏好，不应成为他人默认。
- 重建翻译实例时，先 `init --prefs <file>`，再 `scan → plan → translate`，即可恢复全部个人偏好。
- **与翻译风格的关系**：`init` 会**先采用偏好中的 `user_rules`（若有），再追加所选翻译风格块**；风格块带幂等去重（同一风格不会重复追加）。选「标准」表示不改动（保留偏好原样的 `user_rules`）。`export-prefs` 导出的是完整 `user_rules`（含已追加的风格块）。

## 核心机制

- **逐段拼接**：在原始解码文本上定位每个可翻译文字段的字符偏移，翻译后原位拼回。除被替换的文字外，标签、注释、`tppabs` 属性、空白等字节完全不变，保证"完全相同样式"。段索引（`work/segments/*.json`）记录 `页面/段ID/源偏移/译文/引文`，为译者注与未来的浏览器插件提供锚点。
- **编码**：逐文件探测，候选优先级为 `<meta charset>` 声明 → **源语言常见编码列表**（`lang.source`，如 `ja`→`cp932/euc_jp/iso2022_jp`、`zh-Hans`→`gbk`、`zh-Hant`→`big5` 等；未预设语言回退 `utf-8`）→ `utf-8` 兜底。全部候选均无法严格解码时抛 `EncodingError`（拒绝，不静默替换），scan 跳过该页并汇总 `encoding_failed`，提示运行 `fix`。scan 探测到的编码缓存进 site_map（`pages[rel].encoding`），后续流程优先复用缓存编码解码。输出统一 UTF-8 并在 `<head>` 补/改 `<meta charset>`（这是唯一必要改动）。
- **编码修复（`fix` 命令）**：`booktr fix` 遍历源目录，对无法严格解码的 html 用 `errors='replace'` 修复为 UTF-8 输出到 `fix` 目录（默认 `<data_dir>/fix`，保持目录结构），**不修改原始文件**；`--dry-run` 仅列出需修复文件；`--all` 额外复制全部文件（资源与正常 html），使 fix 目录可直接作为新源。用户审核后手动合并回源目录。
- **全角字符保留**：全角写法保留（全角英文字母、全角数字、全角符号保持全角；几何符号、省略号、破折号、智能引号保持原样）是**站点特定规则**，经 `user_rules` 注入 system prompt（本模板默认含该规则；可改、可删）。人名保留原形、英文/拉丁字母不翻译同为 `user_rules` 可配置项（默认已含）。`user_rules` 小节带有优先级声明，冲突时以用户规则为准。
- **翻译风格**：`init` 可选预设风格（`[1] 标准`、`[2] 上海话`），选中后把对应规则块追加到 `user_rules` 末尾（`standard` 不改动），从而风格化译文而无需改动代码结构或新增语言码。预设表（`pipeline.TRANSLATION_STYLES`）可扩展，未来可增其他方言或风格。风格仅作用于翻译/重译的 system prompt，摘要/QA 等仍用标准中文。
- **占位符**：段内内联标签（`<img>/<font>/<a>…`）转为 `[[P0]]` 占位符交给 LLM，译文必须原样保留，拼接时还原。相邻 inline 标签（含纯空白分隔）合并为单个占位符，减少 LLM 困惑。短语记忆命中后从原始 chunk 恢复占位符。
- **解析自愈**：LLM 输出非法 JSON 时自动重试（最多 `max_repair` 次），每次携带具体错误信息让 LLM 修正；占位符丢失时触发额外 repair；兜底清理去除 `|TEXT|`/JSON 残渣。
- **JSON 机械修复**：解析失败时按序用机械修复做后处理（`ESCAPE_VALUE_STRINGS` 值字符串转义覆盖未转义引号/裸换行，`CLOSE_ARRAY` 按已知 key 先验补全缺 `]`），成功且含 `translation` key 则附加 `repaired: true` + `repair_methods` 规范字段；仍失败才触发 LLM repair。该信息持久化到段状态（`state.json`）与段缓存（`work/segments/*.json`），并在 export-log 中标注 `⚠ 修复` 及头部汇总，便于追溯与改进修复逻辑。
- **多轮对话翻译**：页面内所有段落共享同一对话上下文，LLM 能保持术语与风格一致性。
  - **摘要接力**：达到 `max_history_segments`（默认 50）后自动生成摘要，重建对话继续翻译。
   - **推荐译法注入**：system prompt 不含词条，仅含全局规则（占位符、风格、`user_rules`、输出格式）。
    词汇表/短语记忆词条在翻译时经宽松匹配（忽略大小写/空白）检索当前 chunk 中出现者，
    注入到**该 chunk 的 user message** 作为"推荐翻译译文"，由 LLM 自行裁定在长句中的用法。
    有 note 的条目标注使用场景（如「导航入口」），避免将 HOME 等词在其他语境误翻译；
    短语型内容（短 chunk 精确匹配）已通过机械替换实现，无需 LLM。
- **重新翻译**：删除 review 条目后，该段落标记为 pending，下次 translate 时自动重新翻译。删除时会同时清理该段对应的翻译记忆（TM）与翻译笔记（notes），防止旧译文/旧说明经检索注入形成自我锚定（`reset` 命令同样如此，可用 `--keep-tm`/`--keep-notes` 保留）。
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
  - **审核条目操作**：`[a]`接受（保留译文）`[s]`跳过 `[d]`删除（清除该段译文，页面转 pending，`--next` 可重译；删除前预览并二次确认，同时清理该段对应的翻译记忆 TM 与翻译笔记 notes，避免重译自我锚定）`[c]`确认加入词汇表 `[q]`退出。QA 条目（`qa_*` 原因）仅 `[a]`标记已处理，不改变页面翻译状态。
  - **页面状态流转**：页面有 open 审核项时 status=`review`，`--next` 会跳过；需处理完该页全部 open 项（或 `[d]` 使页面转 `pending`）后才会被 `--next` 重新翻译。删除段译文后，`translate --next` 只重译被删除的段，其余已译段保留。

## 增强工具

| 工具 | 说明 | 配置 |
|---|---|---|
| 词汇表 | 人工预置 + `extract-terms` LLM 自动抽取候选（需确认）；confirmed 默认 `read_only=true`（短 chunk 精确机械替换，跳过 LLM）；长句经宽松匹配注入 user prompt 作推荐译法 | `glossary.path` |
| 翻译记忆 TM | 双语片段缓存，跨页复用 | `tm.enabled` |
| 短语记忆 | 导航短语精确匹配复用；自动学习；长句经宽松匹配注入 user prompt 作推荐译法 | `phrases.max_len` |
| 风格指南 | `style-extract` 从对照样例提炼规则注入 | `style.rules_enabled` |
| 风格锚定 | 字符 n-gram 相似度检索 top-k 样例 few-shot 注入 | `style.exemplar_enabled` |
| 上下文包 | plan 前 N1 篇 + 时间前导 N2 + 链接前导 N3 摘要（逐级去重） | `planner.context` |
| 一致性 QA | 对已译页做体检：本地规则（占位符完整性=高危、术语一致=中危）+ LLM 深度语义检查（`qa.deep_llm_check`）；问题以 `qa_*` 原因写入审核队列供人工确认，不自动触发重译 | `qa.deep_llm_check` |
| 词汇/短语审计 | `audit-terms` 用新词汇表/短语记忆重建已译段（按占位符边界精确匹配），同步 state+段缓存并重生成 out | `audit-terms` |
| 残留检测 | `check-residual` 只读扫描已译段，列出译文残留的源语言片段（当前仅日语·平假名），逐项给出 reset 命令供人工核验后手动重译；不自动重译 | `check-residual` |
| 译者注 | 跨页关联/趣味发现 → 外部 JSON | `annotate` |
| Session ID | 页面翻译任务标识（task_id）+ 多轮对话标识（context_id） | 自动生成，写入 LLM 日志 |

优先级：**词汇表 > 风格样例 > 风格规则**。

### 一致性 QA（`qa`）

`python booktr-cli.py qa` 对已翻译完成的页面做一致性体检，问题分两类来源：

- **本地规则检查**（无 LLM 开销，逐段执行）：
  - **HTML 安全（高危）**：译文占位符数量与原文不一致（`[[P0]]` 等），说明内联标签被 LLM 删除/改动，会破坏原站结构。
  - **术语一致（中危）**：原文含已确认词汇表术语但译文未含其标准译文。
- **LLM 深度检查**（`qa.deep_llm_check`，默认 true）：把整页原文+译文（各截断 6000 字符）交 LLM（temperature 0.2）做语义层面审查，补充误译、术语使用不当、上下文不一致等问题。提示词要求**只列出确实需要修改的问题**（正确/可接受/无需修改的不列出）、同段同类问题合并为一条、`high` 用于确定性错误；并**逐字引用**相关原文/译文片段（`src_quote`/`dst_quote`），不给出段号。

每条问题标准化为 `{severity, reason, src_quote, dst_quote, suggestion}`，随后按引用的**原文/译文片段机械定位**到具体段（`locate_segments`：精确子串 → 去占位符 → 跨行拆分 → 模糊；候选为**所有已翻译段**，含 `head_title`/属性段），得到 `segments` 与 `resolved`。

运行过程**逐页打印进度**（`[i/N] 页面  问题数 (耗时)`）；深度检查单页 LLM 调用失败会打印 `⚠ ... LLM 深度检查失败（已跳过）` 而非静默。报告写入带时间戳的 `work/qa_reports/qa_<YYYYmmdd_HHMMSS>.json`（**每次运行都留存，不覆盖**），同时刷新稳定别名 `work/qa_report.json`；均为**逐页增量写入**。问题写入**专用队列** `work/qa_queue.json`（按 id 去重，**不再写入 `review_queue.json`**）。

**无状态、按区间推进**：QA 不记录"已检查到哪"，`--start/--count` 按 `plan.order`（站点固定顺序，不受重译影响）取区间。因此分批检查即 `--start 1 --count 10` → `--start 11 --count 10` …；重译后想重查某页用 `--pages` 强制指定。

### QA 裁定与应用（`qa-review` / `qa-apply`）

QA 只发现问题，纠正走"人工裁定 + 定点重译"闭环：

1. `qa-review`：逐条检阅（严重度/原因/相关原文/现有译文/建议，并展示定位段的**完整原文+现译+前后文**）。操作 `[a]采纳` `[r]拒绝` `[m]手工指定段号` `[d]丢弃` `[s]跳过` `[q]退出`。**未定位**（`resolved=false`）的条目会提示，须 `[m]` 指定段号或 `[d]` 丢弃（保留在队列直至手动处理）。
2. `qa-apply`：对 `adopted` 条目按 `(页面, 段)` 分组、合并同段意见，逐段定点重译——在基础重译上下文之上，追加 **QA 意见 + 现有译文**，并提示"在此基础上修正、其余尽量保持不变"。重译前清理该段旧 TM/notes、成功后写入新 TM（与 `reset` 一致，避免自我锚定），同步更新 state/段缓存并重生成 out，条目标记 `applied`。`--dry-run` 仅列出将修正的段。

配置（`qa`）：`deep_llm_check`、`queue_path`（默认 `work/qa_queue.json`）、`report_dir`（默认 `work/qa_reports`）、`reasoning_effort`（默认空=继承 `llm.reasoning_effort`）。**思考等级**：所有 LLM 调用默认**流式**（`llm.stream`），长思考不再误判超时；QA 可经 `qa.reasoning_effort` 单独覆盖思考等级（如设 `none` 关闭思考以加速，`high` 提升审查深度）。`reasoning` 内容完整记录在 `work/llm_logs/*.json`（`reasoning`/`reasoning_len`）。

## LLM 接入

- `llm.provider: "mock"`：离线运行，返回确定性结果，用于验证管线与数据结构（无需 API key）。
- `llm.provider: "openai-compatible"`：接入任意兼容服务（OpenAI / OpenRouter / vLLM / Ollama / LM Studio 等）。API key 提供方式（优先级从高到低）：
  - `llm.api_key`：**直接写入 config**（init 时输入即写入此项）。
  - `llm.api_key_env`：环境变量名（默认 `BOOKTR_API_KEY`），从环境读取。
  - 本地免 key 服务（如 Ollama）：设 `llm.api_key_required: false`，空 key 也可请求（请求头省略 `Authorization`）。

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
| `work/qa_report.json` | QA 报告（最新一次的别名） |
| `work/qa_reports/qa_<时间戳>.json` | QA 报告归档（每次运行留存） |
| `work/qa_queue.json` | QA 问题队列（裁定/应用状态） |
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
