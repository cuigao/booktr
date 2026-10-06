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
python booktr-cli.py qa-review                 # 逐条检阅 QA 意见（含原文/现译/上下文），[a]采纳/[e]自定义意见/[r]拒绝/[d]丢弃
python booktr-cli.py qa-apply                  # 对已采纳意见批量定点重译
python booktr-cli.py qa-apply --page index.html
python booktr-cli.py qa-apply --dry-run        # 仅列出将修正的段
python booktr-cli.py qa-status                 # 聚合各页最近一次 QA 状态（已/未 QA、时间、问题数、open 条数）
python booktr-cli.py qa-status --pending-only  # 只列未 QA 的页
python booktr-cli.py qa-clear                  # 移除队列中的 open 条目（先预览、同目录备份 <ts>.bak）
python booktr-cli.py qa-clear --dry-run        # 仅预览
python booktr-cli.py qa-clear --status all     # 清空整个队列（默认仅 open；--page 可限页）

# 10b-2) 监督式自动 QA（qa → 判官裁定 → 自动定点重译，全自动闭环）
python booktr-cli.py qa-auto                          # 默认：处理所有【未 QA】的已译页（可续跑）
python booktr-cli.py qa-auto --pages today/today14.html   # 指定页全自动
python booktr-cli.py qa-auto --start 14 --count 5         # 按 plan.order 分批
python booktr-cli.py qa-auto --all                        # 纳入全部已译页（含已 QA）
python booktr-cli.py qa-auto --adjudicate-only            # 跳过 QA，仅裁定队列中 open 条目
python booktr-cli.py qa-auto --no-apply                   # 只写裁决、不自动重译
python booktr-cli.py qa-auto --dry-run                    # 跑 QA+裁定但只打印，不写不改
# 只裁决指定 id 白名单（配合 tools/qa_auto_probe/analyze.py --emit-category 按类别筛选）
python booktr-cli.py qa-auto --adjudicate-only --no-apply --only-ids work/qa2_DE_ids.json

# 10c) 段落定位（按原文/译文片段定位段号）
python booktr-cli.py locate --page today/today90.html --src "私も無理せず"   # 从页面复制的片段
python booktr-cli.py locate --page today/today90.html --dst "努力起床" --all  # --all 纳入未译段

# 10d) 段落回滚（段颗粒度版本管理；默认先预览再确认）
python booktr-cli.py rollback --page today/today90.html --list             # 列出各段历史版本
python booktr-cli.py rollback --page today/today90.html --list-ops         # 列出命令调用 op_id
python booktr-cli.py rollback --page today/today90.html --segments 2       # 恢复到上一个不同版本（先预览）
python booktr-cli.py rollback --page today/today90.html --src "私も無理せず"  # 用片段定位目标段
python booktr-cli.py rollback --page today/today90.html --op op_..._reset  # 撤销一次 reset 的整页改动
python booktr-cli.py rollback --page today/today90.html --segments 2 --version ver_xxxx
python booktr-cli.py rollback --page today/today90.html --dry-run          # 仅预览（译文/TM/笔记/状态）
python booktr-cli.py rollback --page today/today90.html --interactive      # 交互浏览版本并恢复
python booktr-cli.py rollback --backfill                                   # 为已有译文补录 v1 版本
python booktr-cli.py rollback --page today/today90.html --purge --keep-last 20  # 历史管理

# 11) 生成译者注 / 渲染注本
python booktr-cli.py annotate                    # 生成（写入 work/translators_notes.json）
python booktr-cli.py annotate --pages today/today11.html   # 限定页面
python booktr-cli.py annotate --export           # 离线渲染注本到 out_annotated/（不调 LLM）
python booktr-cli.py annotate --export --dry-run # 仅打印定位报告

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
  - `llm.api_key` / `llm.api_key_env` / `llm.api_key_required`：API key 提供方式（init 时**三选一**：明文写入 config / 环境变量 / 无需 key，见下）
  - `llm.max_tokens`：单次回复的 token 上限（默认 131072）。**推理模型**（如 deepseek-v4.1 系列）会先输出大量 `reasoning` token，上限过低会导致正文为空（`finish_reason=length`），故默认放宽
  - `llm.max_tokens_ceiling`：当正文因 reasoning 被截空时，自动翻倍 `max_tokens` 重试一次的上限（默认 524288）
  - `llm.stream`：流式输出（默认 true）。流式下每个分块都会重置读取超时，**长思考不再被误判为网络超时**；不支持流式的服务设 false
  - `llm.timeout`：流式**读取间隔**超时（默认 120s）——只对"指定秒数内无任何数据"生效，专防真正网络卡死/中断（合法长响应因持续有分块而不触发）。非流式下它即等于总时长（见下 `max_call_seconds`）。
  - `llm.connect_timeout`：流式建连超时（默认 20s）
  - `llm.reasoning_effort`：推理模型思考等级（OpenAI 规范字段）。**空字符串 = 不发送该字段**（用模型默认，通常 `high`）；可设 `none`/`low`/`high`/`max`（以服务支持值为准）。**命令级覆盖**：`qa.reasoning_effort` 非空时覆盖全局，仅对 QA 生效；为空则继承 `llm.reasoning_effort`。
  - `llm.max_repair`：解析失败自愈重试次数（默认 3）
  - **输出循环防护**（`llm.loop_*`，默认开）：流式过程中若输出**末尾陷入周期性重复**（模型被上下文片段卡住、自我锚定），即提前中止并以**相同参数**重试，避免跑满预算（实测可在浪费 3–5 万字符时止损，节省 90%+ 时间）。
    - `llm.loop_guard`（默认 true）；`loop_window`（默认 16384，检测的末尾窗口字符数）；`loop_min_repeats`（默认 2，窗口内最少重复次数）；`loop_min_span`（默认 2048，重复段总长下限 `period×repeats`——短周期需更多次，如 `Hmm.` 需连续数百次，而 7k 长块 2 次即成立）；`loop_check_every`（默认 512，流式检测间隔）；`loop_retries`（默认 2，循环重试次数，**独立于** `max_retries`）；`loop_temp_bump`（默认 0.1，每次循环重试递增 temperature，上限 base+0.3，用于打破锚定）；`loop_norm`（默认 true，比较前折叠空白）。
    - **仅识别精确（空白不敏感）周期重复**（周期实测 5～7k+ 字符）；非周期性的推敲不在此防线内。检测到的异常轮次**不会**进入多轮对话历史（内部重试，调用方消息不变），并完整记录于该次调用日志的 `loop_aborts` 字段。
  - **单调用总时长上限**（`llm.max_call_seconds`，默认 300s；`0`=关闭）：覆盖"一直在输出但不结束"的意外超长响应。周期性循环与非周期滴答都可能超长：前者由 `loop_guard` 秒级截断，后者由本上限兜底。可按 tag 前缀覆盖：`llm.max_call_seconds_by_tag`（默认 `{"qa":1200,"term":600}`，因 QA/术语辨析的合法单次调用本身很长）。超时后以**相同参数**重试 `llm.call_retries` 次（默认 1，独立于 `max_retries`/`loop_retries`），仍失败则以普通 `LLMError` 冒泡（上层降级处理）。异常轮次记录于日志 `wall_aborts` 字段。
    - **三层防护互不冲突**：`llm.timeout`（默认 120s，**流式读取间隔**超时）专防真正网络卡死/无数据停顿——合法长响应因持续有分块而不触发；`loop_guard` 秒级抓周期循环；`max_call_seconds` 兜底非周期超长。三者按"谁先触发"生效，预算各自独立。
    - **注意**：非流式（`llm.stream=false`）下无中途检测机会，read timeout 即等于总时长，故 `_post_once` 以 `min(timeout, max_call_seconds)` 作为超时。
    - **未来可选**：真正的"首 token 120s、其后 60s"分段读取超时需 `urllib3>=2.0`（`HTTPResponse.settimeout`）；当前栈固定单值，暂以 120s 单值覆盖。
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

### 基于已有实例初始化（`init --clone`）

`init --clone <data_dir>` 以**另一个已存在实例的 `config.json` 作为默认配置层**（替代模板，仍深合并内置默认兜底），从而快速克隆一个配置相同的实例：

- 逐项提示的默认值来自该实例，**直接回车沿用、主动输入才覆盖**（仍是完整交互式，可改任意项）。
- **自动继承**该实例的 `glossary` 与 `style_refs`；若同时给 `--prefs`，偏好文件在其后写入并**覆盖**继承数据。
- **`source_dir` 相对路径按新数据根重算**，始终指向同一站点（同深度复制则保持原样；不同深度自动调整）；`output_dir`/`work_dir` 保持相对（新实例自有的 out/work，从零开始）。
- **API key 提供方式**（`init` 中三选一）：`[1] 明文 key`（写入 config，**默认**）——回车保留现有值、输入 `-` 清空、直接留空表示无需 key；`[2] 环境变量`——写入环境变量名（默认 `BOOKTR_API_KEY`，可自定义），`api_key` 留空、运行时从环境读取；`[3] 无需 key`（本地服务）——`api_key_required: false`，请求头省略 `Authorization`。
- **`--clone` 时**：API key 提供方式与提示默认值**镜像源实例**（源为环境变量模式则默认 env 并继承其变量名）；明文模式仍为回车保留、`-` 清空。
- 典型用法（同配置、仅风格改为上海话）：

  ```bash
  python booktr-cli.py --data-dir ../instance/data-deepseek-v4.1-flash-sh \
      init --clone ../instance/data-deepseek-v4.1-flash   # 交互中选「上海话」
  ```

- 优先级：**内置默认 < `--clone` 配置 < `--prefs` < 交互输入**。

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
     **匹配不做全半角归一**：源站同一专名存在全角/半角混用（如 `岡崎律子Ｂｏｏｋ` / `岡崎律子Book`）时，应为每种宽度各建一条词条，并在 note 中如实说明（如「此词条为半角形式，原文中与全角形式混用」），使翻译与 QA 均能按原文形式判定。
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
| 术语初筛辨析 | `terms-scan`：零词条起步，机械算法（脚本串/跨页模板/n-gram 等）抽高频候选 → LLM 语义辨析判定价值并给建议译名 → 并入词汇表（auto-candidate，待人工确认）；`extract-terms` 为旧版逐页抽取，保留兼容 | `terms_scan` |
| 翻译记忆 TM | 双语片段缓存，跨页复用 | `tm.enabled` |
| 短语记忆 | 导航短语精确匹配复用；自动学习；长句经宽松匹配注入 user prompt 作推荐译法 | `phrases.max_len` |
| 风格指南 | `style-extract` 从对照样例提炼规则注入 | `style.rules_enabled` |
| 风格锚定 | 字符 n-gram 相似度检索 top-k 样例 few-shot 注入 | `style.exemplar_enabled` |
| 上下文包 | plan 前 N1 篇 + 时间前导 N2 + 链接前导 N3 摘要（逐级去重） | `planner.context` |
| 一致性 QA | 对已译页做体检：本地规则（占位符完整性=高危、术语一致=中危）+ LLM 深度语义检查（`qa.deep_llm_check`）；问题以 `qa_*` 原因写入审核队列供人工确认，不自动触发重译 | `qa.deep_llm_check` |
| 词汇/短语审计 | `audit-terms` 用新词汇表/短语记忆重建已译段（按占位符边界精确匹配），同步 state+段缓存并重生成 out | `audit-terms` |
| 残留检测 | `check-residual` 只读扫描已译段，列出译文残留的源语言片段（当前仅日语·平假名），逐项给出 reset 命令供人工核验后手动重译；不自动重译 | `check-residual` |
| 译者注 | 跨页关联/趣味发现 → 外部 JSON（每条含可锚定 `src_quote`/`dst_quote`）；`--export` 离线渲染注本（复制 out + 插 `<sup>` 角标 + 内联 JS 侧栏，源 out 零改动） | `annotate [--export]`、`translators_notes.max_notes_per_page` |
| 段落定位 | 按原文/译文片段在页面内定位段号（精确→去占位符→跨行→模糊 Dice）；`qa` 与 `rollback` 共用 | `locate` |
| 段落回滚 | 段颗粒度版本管理：提交即版本、非线性 pick 恢复、`--op` 整命令撤销、`--dry-run` 预览、`--purge` 管理 | `rollback` |
| 短语记忆清理 | 覆盖路径（reset/review `[d]`/qa-apply/rollback）按 `源文 ∩ 旧译文 ∖ 新译文` 对称清理短语；qa-apply 清后按同条件回写新短语（与 TM 对齐）；不纳入版本快照 | 自动 |
| 监督式自动 QA | `qa-auto`：逐页 `qa → 监督判官逐条裁定（采纳/拒绝/跳过）→ 对采纳项自动定点重译` 的全自动闭环；判官可独立配置模型，以全站摘要+当前页全文+词汇表+风格规则为语境 | `qa.supervisor` |
| Session ID | 页面翻译任务标识（task_id）+ 多轮对话标识（context_id） | 自动生成，写入 LLM 日志 |

优先级：**词汇表 > 风格样例 > 风格规则**。

### 一致性 QA（`qa`）

`python booktr-cli.py qa` 对已翻译完成的页面做一致性体检，问题分两类来源：

- **本地规则检查**（无 LLM 开销，逐段执行）：
  - **HTML 安全（高危）**：译文占位符数量与原文不一致（`[[P0]]` 等），说明内联标签被 LLM 删除/改动，会破坏原站结构。
  - **术语一致（中危）**：原文含已确认词汇表术语但译文未含其标准译文。
- **LLM 深度检查**（`qa.deep_llm_check`，默认 true）：把整页原文+译文（各截断 6000 字符）交 LLM（temperature 0.2）做语义层面审查，补充误译、术语使用不当、上下文不一致等问题。提示词要求区分两类问题并同段同类合并为一条：`high` 用于确定性错误（术语与词汇表不符、漏译、误译），`low` 用于措辞/风格类问题（生硬、翻译腔、不自然的表达）；并**逐字引用**相关原文/译文片段（`src_quote`/`dst_quote`），不给出段号。QA 的词汇表与翻译**同款渲染**（复用 `prompts.term_lines`）：含 `note` 的条目以 `└ 使用场景：…` 子行呈现，供 LLM 判定。**QA 与翻译/判官共享同一 `user_rules`**（`translate.effective_user_rules`，含用户注入笔记），避免仅凭词汇表条目过度外推（如把「岡崎保留原形」误推为所有专名一律保留）。此外：
  - **审核政策**（`qa.reduce_style_reports`，默认 true）：追加"只报影响命题内容/言外之力/语域语气/关键术语者，其余（含直译-意译取向差异）属可接受损失不报；尊重用户规则中的风格倾向"，抑制"忠实-流畅"两轴来回摇摆。
  - **历史注入**（`qa.inject_history`，默认 false）：把同页已发生的 QA 决策（applied 段的旧→新译文 + rejected 理由 + `llm_suggestion` 原值）注入 QA 与判官提示词，减少对已定译法的反向重报。

每条问题标准化为 `{severity, reason, src_quote, dst_quote, suggestion}`，随后按引用的**原文/译文片段机械定位**到具体段（`locate_segments`：精确子串 → 去占位符 → 跨行拆分 → 模糊；候选为**所有已翻译段**，含 `head_title`/属性段），得到 `segments` 与 `resolved`。

运行过程**逐页打印进度**（`[i/N] 页面  问题数 (耗时)`）；深度检查单页 LLM 调用失败会打印 `⚠ ... LLM 深度检查失败（已跳过）` 而非静默。报告写入带时间戳的 `work/qa_reports/qa_<YYYYmmdd_HHMMSS>.json`（**每次运行都留存，不覆盖**），同时刷新稳定别名 `work/qa_report.json`；均为**逐页增量写入**。问题写入**专用队列** `work/qa_queue.json`（按 id 去重，**不再写入 `review_queue.json`**）。

**报告结构（version 2）**：除 `pages`（**仅有问题的页** → issue 列表，向后兼容）外，新增 `checked`——**每个被检查的页**（含 0 问题）→ `{ts,total,high,mid,low,unresolved,deep,duration_s}`；另有 `ts/scope/total_pages_checked` 本次运行元信息。因此**每页是否 QA 过、最近一次时间与结果均可聚合查询**（`qa-status`；旧版报告无 `checked`，仅能从 `pages` 推断有问题的页）。

`qa-status`（只读）扫描 `work/qa_reports/*.json` 聚合各页**最近一次** QA 状态，对照 `plan.order` 列出已/未 QA、每页时间与问题数、以及 `open` 队列条数；`--pending-only` 只列未 QA 页，`--issues` 只列有问题或未决条目的页。

**无状态、按区间推进**：QA 自身不维护"已检查到哪"的游标，`--start/--count` 按 `plan.order`（站点固定顺序，不受重译影响）取区间。因此分批检查即 `--start 1 --count 10` → `--start 11 --count 10` …；重译后想重查某页用 `--pages` 强制指定。**查询每页 QA 状态**用 `qa-status`（从报告 `checked` 聚合，见上）。

### QA 裁定与应用（`qa-review` / `qa-apply`）

QA 只发现问题，纠正走"人工裁定 + 定点重译"闭环：

1. `qa-review`：逐条检阅（严重度/原因/相关原文/现有译文/建议，并展示定位段的**完整原文+现译+前后文**）。操作 `[a]采纳` `[e]自定义意见` `[r]拒绝` `[m]手工指定段号` `[d]丢弃` `[s]跳过` `[q]退出`。**未定位**（`resolved=false`）的条目会提示，须 `[m]` 指定段号或 `[d]` 丢弃（保留在队列直至手动处理）。
   - `[e]自定义意见`：LLM 检出问题但不满意其提案时，人工输入**建议译文/说明**覆盖有效字段 `suggestion`/`reason`，原 LLM 值归档到 `llm_suggestion`/`llm_reason`（**仅留档，后续 `qa-apply` 不再引用**），标记来源 `source=human` 后采纳。字段级输入：**回车=沿用 LLM 原值**、**`-`=清空该字段**、其它文本=覆盖；两字段均回车视为无变化，取消 `[e]`、不采纳（如需直接采纳 LLM 建议用 `[a]`）。人工建议仍作为提示交 LLM 重译（非逐字硬写）。
2. `qa-apply`：对 `adopted` 条目按 `(页面, 段)` 分组、合并同段意见，逐段定点重译——在基础重译上下文之上，追加 **QA 意见 + 现有译文**，并提示"在此基础上修正、其余尽量保持不变"。重译前清理该段旧 TM/notes、成功后写入新 TM（与 `reset` 一致，避免自我锚定），同步更新 state/段缓存并重生成 out，条目标记 `applied`。`--dry-run` 仅列出将修正的段。
3. `qa-clear`：从队列移除条目（默认仅 `open`）。先**预览**将删/保留条数并二次确认，落盘前自动**同目录备份** `<path>.<ts>.bak`。`--status all` 清空整队列，`--status adopted|rejected|applied` 按状态、`--page` 限页，`--dry-run` 仅预览、`-y` 跳过确认。用于丢弃一批过时/已失效的 QA 意见（不动 `qa_reports`、翻译状态与 TM/notes）。

配置（`qa`）：`deep_llm_check`、`queue_path`（默认 `work/qa_queue.json`）、`report_dir`（默认 `work/qa_reports`）、`reasoning_effort`（默认空=继承 `llm.reasoning_effort`）。**思考等级**：所有 LLM 调用默认**流式**（`llm.stream`），长思考不再误判超时；QA 可经 `qa.reasoning_effort` 单独覆盖思考等级（如设 `none` 关闭思考以加速，`high` 提升审查深度）。`reasoning` 内容完整记录在 `work/llm_logs/*.json`（`reasoning`/`reasoning_len`）；**失败调用**（如正文被 reasoning 截空、流式中断）也会尽量记录已累加的 `reasoning`/`reasoning_len` 与 `finish_reason`，便于事后诊断模型"纠结"的内容。

> `[e]自定义意见` 已实现"覆盖意见并采纳"（`interactive_qa_plan.md` L1 的最小落地）。但 `qa-review` 仍无法**部分采纳/拆分**意见、原文划线提意见或人工直改译文，且无撤销手段。更完整的**交互式 QA 审查**设计（含上述能力与数据模型演进）见工作区文档 `instance/report/interactive_qa_plan.md`。

### 监督式自动 QA（`qa-auto`）

面向"想要接近人工 QA 的效果、却无力承担人工精校成本（甚至不懂日文）"的场景，`qa-auto` 逐页自动完成 **`qa → 判官裁定 → 对采纳项定点重译`** 闭环（默认全自动；`--no-apply`/`--dry-run` 可选退出）。判官是一个**独立 LLM**，以**重语境**逐条裁定 QA 问题：

- **system 语境**（每轮随请求重发）：当前页上下文 + **全站页面摘要**（`include_all_summaries`，总量上限 `all_summaries_max_chars` 默认 65536）+ 词汇表（含 note）+ 风格规则 + 用户规则 + **当前页完整原文/译文**。
- **多轮逐条**：第 1 条 user 列出全部 QA 问题并要求"先只裁决第 1 条"，其后每条一轮（`multi_turn`）。历次裁决 JSON 累积在对话历史中，为同页一致性提供锚点，避免一次性多判决的漂移。**提问现场拼装**：每条的请求 = 已提交历史 + 当前条目；单条失败时**不提交历史、不注入 seed**，避免"seed(第 1 条) + 第 i 条"并存导致答错条目。
- **索引回显 + 相关性守门**：判官须返回 `index`；回显不等于当前序号、或响应明显指向同页其它条目时，该条判 `skip`（留人工），不覆盖建议。
- **裁定** `adopt`（采纳，默认沿用 QA 原 `suggestion`；判官若给出更优版本则替换并归档到 `llm_suggestion`，标记 `source=supervisor`）/ `reject`（拒绝）/ `skip`（信息不足或未定位，保持 open 自动跳过）。
- **容错**：单条 LLM 调用失败（网络抖动等）记 skip 并继续，不中断整页；判官响应允许值字符串内未转义引号（自动修复）。
- **闭环**：采纳项按 `(页, 段)` 分组复用 `qa-apply` 的 `apply_qa_fix` 定点重译（同步清理旧 TM/notes/短语、重生成 out、提交段历史版本）。**判官 HTML 安全防线**：问题涉及标签/占位符时要求核对**源文本本身**，避免把 QA 幻觉（如源文即含的字面 `<`+`!` 文本）写成标签。

**运行范围与续跑**：默认只处理**尚未 QA 过的已译页**（`--all` 可纳入已 QA 页；`--pages/--start/--count` 显式指定）。**逐页独立容错与增量落盘**——某页出错只记录并跳过，不中断整批；因"已完成页才计入报告"，中断后**重跑同一命令即自动续跑**。每次运行的逐页统计与错误写入 `work/qa_auto_runs/qa_auto_<ts>.{log,json}`（`qa.auto_log_dir` 可配）。

**按 id 白名单裁**：`--only-ids <file>` 只裁决白名单里的条目（`["id",...]` 或 `[{"id":...}]`），**未选中的 open 条目保持不动**。可配合 `tools/qa_auto_probe/analyze.py --emit-category <data_dir> --category DE` 按问题类别（A 标点/全半角、B 术语专名未译、C 前后不一致、D 措辞/翻译腔、E 漏译/语义偏移、F 术语选词、G 星期日期数字）产出名单，人工增删后只对某类问题跑闭环，例如"只对 D+E 措辞语义类裁决"：
`qa-auto --adjudicate-only --no-apply --only-ids work/qa2_DE_ids.json`。

配置（`qa.supervisor`）：`enabled`（默认 true）、`provider`/`base_url`/`model`/`api_key_env`/`api_key`/`api_key_required`（**空值继承主 `llm`**）、`temperature`（默认 0.1）、`max_tokens`（默认 131072；正文因 reasoning 截空时自动翻倍，上限 `llm.max_tokens_ceiling`）、`timeout`（默认 120，短超时快速暴露抖动）、`max_retries`（默认 1）、`reasoning_effort`、`include_all_summaries`、`all_summaries_max_chars`、`multi_turn`、`log`（判官调用是否写 `llm_logs`，默认 true；判官 system 很大，长跑可设 false 省磁盘）。判官漂移可离线审计：`python tools/qa_auto_probe/analyze.py --audit <data_dir>`。设计依据与实测评估见工作区报告 `instance/report/qa_auto_probe_report.md`；阶段 0 的探针/评估脚本留存于 [`tools/qa_auto_probe/`](tools/qa_auto_probe/README.md)（`judge` 判官校准 / `e2e` 端到端 / `analyze` 问题级复现率分析）。

### 术语初筛与辨析（`terms-scan`）

**零词条起步**：从原文全站机械抽取高频候选 → LLM 语义辨析判定价值并给建议译名 → 并入词汇表（`status=auto-candidate`，**人工确认后**才用于翻译）。

```bash
python booktr-cli.py terms-scan --no-llm          # 仅机械初筛 → work/term_candidates.json
python booktr-cli.py terms-scan                   # 两遍初筛 + 语义辨析 → work/term_reviewed.json
python booktr-cli.py terms-scan --write           # 辨析终稿并入 glossary（auto-candidate）
python booktr-cli.py terms-scan --no-pass2        # 只跑 Pass1（高频）
```

- **机械初筛**（`booktr/terms.py`，无新依赖、纯 Python）：`runs`（最大脚本串频次，含**片假名·拉丁混排**如 `プライベートCD`）、`repeat_lines`（跨页重复整行/模板）；另提供 `cvalue`/`bpe`/`entropy`/`pmi` 可插拔（本规模语料噪声偏大，默认关闭）。每候选含 `src/script/count/pages/**contexts**`（≤3 条源文窗口）。
- **两遍策略**（避免低频稀释高频）：**Pass1** `count ≥ band_split`（默认 7）用一般提示词；**Pass2**（默认开）`min_count ≤ count < band_split`、仅 `kana/latin`、按 **maximality** 去掉 Pass1 已确认词的子串，用**收严**提示词并**注入 Pass1 保留清单**（跨遍一致性）。合并去重后产出终稿。
- **LLM 语义辨析**：候选（含上下文）+ **现行词汇表** + 用户规则（Pass2 另加 Pass1 清单）交主 LLM，按"专名/站点固定用语/**专业·领域词**/高频自有词 → keep；日常可直译、片段/缩写、套话 → drop"判定，产出 `{src, dst, note, category}`。
- **配置**（`terms_scan`）：`algorithms`（默认 `["runs","repeat_lines"]`）、`band_split`（默认 7）、`two_pass`（默认 true）、`min_count`（Pass2 下限，默认 3）、`pass2_scripts`（默认 `["kana","latin"]`）、`pass2_strict`（默认 true）、`reasoning_effort`（默认 low）、`min_pages`、`context_chars`、`max_len`、`top`。
- 旧版 `extract-terms`（逐页 6000 字符截断的 LLM 抽取）保留兼容，但推荐使用 `terms-scan`。

#### 人工审核 → 确认合入（`terms-review` / `terms-apply`）

`terms-scan` 的终稿默认只作"建议"，需人工审核后再以 **confirmed** 合入词汇表：

```bash
python booktr-cli.py terms-scan --emit-review   # 辨析后直接导出人工审核文件
#   或：python booktr-cli.py terms-review        # 从 work/term_reviewed.json 导出
#   → work/term_review_manual.json：一行一条，字段顺序 src/dst/category/note
#   人工编辑：删行=拒绝、改行=编辑、加行=新增（无状态字段干扰）
python booktr-cli.py terms-apply --dry-run      # 预览
python booktr-cli.py terms-apply                # 备份 glossary 后以 confirmed 合入
```

- 审核文件**一行一条**（含自身花括号与尾逗号），末条多余逗号容错；坏行/空 src **跳过并告警**。
- `terms-apply` **先备份** `work/glossary.json.<ts>.bak` 再写入，可随时回滚（覆盖回备份即可）。
- 同名 src 若译文不同 → **覆盖为人工值并列出冲突**提示确认。
- 合入为 `status=confirmed`（`read_only=true`）：此后精确整段机械替换 + 软注入作推荐译法。

## LLM 接入

- `llm.provider: "mock"`：离线运行，返回确定性结果，用于验证管线与数据结构（无需 API key）。
- `llm.provider: "openai-compatible"`：接入任意兼容服务（OpenAI / OpenRouter / vLLM / Ollama / LM Studio 等）。API key 提供方式（`init` 三选一，运行时优先级从高到低）：
  - `llm.api_key`：**直接写入 config**（init 选「明文 key」即写入此项）。
  - `llm.api_key_env`：环境变量名（默认 `BOOKTR_API_KEY`），从环境读取（init 选「环境变量」时写入此项，`api_key` 留空）。
  - 本地免 key 服务（如 Ollama）：init 选「无需 key」，即 `llm.api_key_required: false`，空 key 也可请求（请求头省略 `Authorization`）。
  - 运行时取值优先级：**`llm.api_key`（非空）> 环境变量 `api_key_env` > 空**。

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
| `work/segment_history/*.json` | 每页段历史版本（提交即版本；供 `rollback` 恢复） |
| `work/state.json` | 检查点 |
| `work/review_queue.json` | 待人工审核项 |
| `work/qa_report.json` | QA 报告（最新一次的别名） |
| `work/qa_reports/qa_<时间戳>.json` | QA 报告归档（每次运行留存；含 `checked` 每页状态，供 `qa-status` 聚合） |
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
- **v2（QA 交互化）**：交互式 QA 审查（原文划线提意见、意见编辑/拆分/部分采纳、人工直改译文、撤销）——设计见工作区 `instance/report/interactive_qa_plan.md`（与 `roadmap.md §3` 衔接）。
