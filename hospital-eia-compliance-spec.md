# 医院建设项目环保审批合规检测系统 — 实施方案 (SPEC)

> 用途：仓库根目录的唯一事实来源。改代码前先读本文件。
> 辖区：中国大陆。场景：新建医院，环保相关审批与合规预审。使用方式：单机自用。
> 已锁定的产品决定见第 14 节。种子清单、示例数值都不是法律事实。

## 0. 一句话定义

输入「项目参数 + 申报材料」，输出「需要办哪些环保审批 + 每个审批点的材料是否满足要求 + 每条结论的法条/标准原文引用 + 证据在材料中的位置」。

结论分四种：`符合 / 不符合 / 不适用 / 无法判定`。**默认是无法判定，不是符合。**

项目参数来自已人工确认的材料抽取结果（`fact`），不另做一套无出处的登记表。材料里没有的必填项，结论为无法判定，并列出缺什么。要补，就再上传载有该句的材料。

只有两项不来自文档，由使用者在界面指定：项目名称、判定基准日。

## 1. 边界（硬约束，写进代码，不靠人自觉）

### 1.1 范围内

环评分类与审批类型判断；水污染（含医疗污水）；医疗废物与危险废物；辐射安全；噪声；大气（锅炉、备用发电机、食堂油烟、污水站臭气）；排污许可；竣工环保验收。

这些是并行轨道，不是一个枚举。至少包括：环评类型（报告书 / 报告表 / 登记表）、排污许可类别、辐射安全许可、医废、竣工验收。

### 1.2 范围外（系统必须拒绝，不得硬答）

规划许可、用地、消防、医疗机构执业许可、卫健部门审批、地方性法规与地方标准（后续扩展之前）。

未知事项同样视为范围外。遇到范围外问题，输出 `OUT_OF_SCOPE`。流水线没有聊天入口时，范围外表现为未覆盖清单，而不是临场编答案。

### 1.3 机制性约束

| 约束 | 实现方式 |
|---|---|
| 引用必须真实 | `quote` 必须是库中该条文原文规范化后的子串，否则该 finding 丢弃，写入 `rejected_finding`，不入库 |
| 证据必须真实 | `evidence_span` 必须是源文档文本的子串，且 `char_start/char_end` 对得上 |
| 数值判断不经过 LLM | 数值项、日期比较、单位换算只走 `rules/evaluator.py` |
| 版本正确 | 只取「施行日 ≤ 判定基准日 且 状态为有效 且 `verified_at` 已填写」的版本 |
| 信息不足不判 | 必填参数缺失 → 该项 `无法判定`，并列出缺什么。比较结果为 unknown 时同样无法判定 |
| 人在回路 | 参数抽取必须经人工确认才能进入评估；`check_item` 人工审核后才 `active`；自动采集的版本在人工确认前不得参与评估 |
| 不当法律意见 | 报告与界面页眉固定声明：辅助预审，不构成法律意见 |
| 可复现 | 思考模式下 temperature 不生效，不要依赖它。每次模型调用按 `hash(模型, prompt 版本, 输入)` 缓存校验后的 JSON。重跑读缓存。`result_hash` 用缓存输出计算。规则引擎路径本身是纯函数 |

检出限与「小于号」是数值规则的一部分，不交给模型：申报上界已经满足限值 → 可以判符合；上界高于限值、真值不明 → 无法判定。见第 6.3 节。

校验失败就修正解析文本或重新切条，不放宽校验器。

## 2. 总体架构

```
 ┌─────────────┐   ┌──────────────┐   ┌───────────────┐
 │ 法规采集器   │──▶│ 条文库(版本化)│──▶│ check_item 抽取 │──人工审核──▶ active 规则库
 │ 自动 + 人工  │   │ 未确认不可用  │   └───────────────┘
 └─────────────┘   └──────────────┘
                          │
 ┌─────────────┐   ┌──────▼───────┐   ┌───────────────┐   ┌──────▼──────┐
 │ 材料上传     │──▶│ 解析/OCR/ASR  │──▶│ 参数抽取(LLM)   │──▶│ 人工确认参数 │
 └─────────────┘   └──────────────┘   └───────────────┘   └──────┬──────┘
                                                                   │
                      ┌────────────────────────────────────────────▼──┐
                      │ 审批识别(规则) → 数值评估(规则引擎) → 定性评估(LLM×2) │
                      └────────────────────────────┬───────────────────┘
                                                   │
                                  引用校验器(硬) ──▶ 报告 + 双栏 UI
```

自动采集与人工上传写入同一套 `law_version`。自动写入的行 `verified_at` 为空，状态为 `待确认`。

## 3. 技术栈（固定，不要自行替换）

- 后端：Python 3.11（容器内）, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic
- 数据库：PostgreSQL 16 + pgvector（Docker 路径）。Windows 免 Docker 启动使用 SQLite，向量列先空着；采集、切条、规则引擎、引用校验不依赖向量
- 向量/重排：BGE-M3、bge-reranker-v2-m3，抽象为 `EmbeddingProvider` / `RerankProvider`。第一版不下载模型，`embedding` 允许为空
- LLM：只暴露 `generate_json(schema, prompt, temperature 不使用)`。配置见 `config/llm.yaml`
- 规则引擎：自写最小求值器（第 6 节），不用 eval，不用 LLM。单位换算用 `pint`
- 文档解析：材料侧为 MinerU 或 Docling、PaddleOCR、FunASR（录音后续再做）。法规 PDF 若是文本型，用 `pypdf` 抽取；扫出来是空文本则要求人工换 txt，不把空文本当成法规
- 前端：本机工作台由后端直接提供（上传、核验、确认）。Next.js 14 保留为后续双栏界面。对外下载页是静态页 `docs/index.html`
- 任务：先用 FastAPI BackgroundTasks，不上 Celery
- 测试：pytest

规格外、实现必须用到的库，理由如下，不要再扩大：

| 库 | 理由 |
|---|---|
| httpx | 调用 DeepSeek 与国家法律法规数据库等公开站点 |
| pypdf | 文本型法规 PDF 取正文。扫描件不靠它 |
| python-multipart | FastAPI 文件上传 |
| pyyaml | 读取 llm.yaml、local.yaml、种子清单 |

模型配置（换模型只改这里）：

```yaml
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  model: deepseek-v4-flash
  thinking: enabled
  reasoning_effort: high
  max_tokens: 8192
  response_format: json_object   # 不支持原生 json_schema，靠 Pydantic 校验
  retries: 2                     # 校验失败重试，仍失败则输出「无法判定」
  cache: true
  prompt_version: "2026-10-21"
```

密钥只来自环境变量 `DEEPSEEK_API_KEY`。思考模式开启时不发送 temperature。`reasoning_effort` 与 `thinking` 是请求体里的同级字段。缓存只保存校验通过的 JSON，不把思考过程算进 `result_hash`。

若 8192 把 JSON 截断，再调高 `max_tokens`。不要为此关掉思考模式。

定性判断目前只用这一个模型。第二遍换不同 prompt。报告标明「同源双审」，并默认建议人工复核。两遍一致只说明两遍说法相同。以后把第二遍的 provider 换成别的模型即可，不必改评估流程。

用 golden set 看出某个环节误判偏高时，再单独把那个环节换成更强的模型。

## 4. 数据模型

```sql
law_instrument(id, name, level, issuer, source_url, doc_no)
  -- level: 法律|行政法规|部门规章|国家标准|行业标准|名录|规范性文件|司法解释

law_version(id, instrument_id, publish_date, effective_date, repeal_date NULL,
            status, raw_text, raw_text_sha256, raw_file_path,
            source, raw_status_code, verified_by, verified_at, created_at)
  -- status: 有效|已修订|已废止|未生效|待确认
  -- source: manual|flk|mee|std
  -- 一经插入不可修改条文与哈希。确认核验时只允许填写 verified_* 以及人工选定的 status 与日期
  -- 自动采集插入时 status=待确认 且 verified_at 为空

law_article(id, version_id, path, text, text_sha256, embedding NULL)
  -- path 例: "第一章/第一条/第一款"；标准用 "5.1.2"
  -- embedding 第一版允许为空

check_item(id, article_id, item_code, applies_when JSON, requirement JSON,
           kind, required_params JSON, quote, status, reviewed_by, reviewed_at, pack_id NULL)
  -- kind: numeric|presence|qualitative ; status: draft|active|retired

rule_pack(id, content_hash, note, created_at)

project_case(id, name, reference_date, params_confirmed)
  -- 没有无证据的 profile。参数以 fact 为准

document(id, case_id, filename, file_sha256, parsed_text, page_map JSON)

fact(id, case_id, param_key, value JSON, unit, document_id, page,
     char_start, char_end, evidence_span, status, confirmed_by, confirmed_at)
  -- status: extracted|confirmed|conflict|rejected

finding(id, case_id, item_id, verdict, reasons JSON, quote, evidence_refs JSON,
        method, result_hash, created_at)
  -- method: rule_engine|llm_dual|human
  -- 同源双审时 reasons 里带 review_label=同源双审

rejected_finding(id, case_id, item_id NULL, payload JSON, reason, created_at)

review_log(id, finding_id NULL, target_type, target_id, action, by, at, note)

llm_cache(cache_key PK, model, prompt_version, response_json, created_at)

verification_report(id, seed_name, source, outcome, version_label,
                    publish_date, effective_date, raw_status, superseded_note,
                    fulltext, version_id NULL, detail JSON, created_at)
  -- outcome: 找到|未找到
  -- fulltext: 已获取|需人工上传|不适用
```

没有用户表。审核人姓名来自 `config/local.yaml` 的 `reviewer_name`，为空时每次确认必须当场填写。

`law_version` 的正文、哈希、来源路径不得 UPDATE。解析错了就再插入一条版本。评估只认 `verified_at` 非空、状态为有效、且施行日 ≤ 基准日的版本。

所有 `*_sha256` 在写入时计算。空正文不得冒充已获取全文。

## 5. 法规采集

两条路都要有，写入同一张版本表。

### 5.1 自动采集

- 法律、行政法规、司法解释：国家法律法规数据库公开检索接口 `https://flk.npc.gov.cn/law-search/search/list`。正文只下载接口返回的 **https 公开文件地址**，且主机名在允许列表内。忽略内网地址（如 `urlIn`）。下载失败则报告「找到元数据，正文需人工上传」，不重试绕过。
- 部门规章、名录、规范性文件：生态环境部公开网页检索。页面解析不到就「未找到」，请人工上传。
- 国家标准、行业标准：只根据公开平台检索元数据。**不下载全文，不绕过阅读限制。** 全文一律人工上传。
- 标题必须规范化后精确匹配（「环境保护法」不得命中「海洋环境保护法」）。模糊近邻写入报告的 `near_misses`，供人工看，不入库为该种子的版本。
- 接口里的状态码原样保存为 `raw_status_code`，程序不把它翻译成「有效」或「已废止」。
- 每一项输出核验报告：`找到/未找到`、公布日期、施行日期、原始状态码、全文是否已取得、近邻标题。**未人工确认的项不得进入评估。**

### 5.2 人工上传

上传 PDF / DOCX / TXT。文本型 PDF 用 pypdf；DOCX 用压缩包内的正文 XML；扫不出文字则拒绝当成法规正文。

上传时可以勾选「我已对照官网核对」。未勾选则同样是 `待确认`。勾选后记录核对人。

### 5.3 种子清单

文件：`backend/seeds/instruments.yaml`。来自人工整理，**不是事实**。其中包含「中华人民共和国生态环境法典」，以及「GB 18871-2002」这一待核验版本（含有没有修改单、基准日当天是否仍然有效）。

采集器逐项核验。法典与各单行法谁替代谁，只写在人工确认后的版本状态里，不在代码里写死。未生效或已废止的版本，评估时不用。

名录另走表格解析器，输出待校对 CSV。校对完成前不参与审批类型判断。分类管理名录建议整表人工校对。

### 5.4 解析

- 法律、法规：按章 / 条 / 款 / 项切分。
- 标准、技术规范：按章节号切分。
- 每条记录原文 sha256。

## 6. check_item 与规则 DSL

### 6.1 生成流程

1. LLM 读取单个 `law_article`，输出候选 `check_item`（`status=draft`）。
2. 人工审核：左边条文原文，右边候选规则。可改、可拒绝、可通过。
3. 通过后 `status=active`。未审核的规则不参与任何评估。所属条文版本也必须已核验。

### 6.2 规则示例

数值只说明格式。真实限值必须在人工审核时对照原文填写，不能照抄示例。

```json
{
  "item_code": "WW-DEMO-001",
  "kind": "numeric",
  "applies_when": {"and": [
    {"==": [{"var": "has_inpatient"}, true]},
    {"==": [{"var": "discharge_to"}, "municipal_sewer"]}
  ]},
  "required_params": ["COD_mg_per_L"],
  "requirement": {"<=": [{"var": "COD_mg_per_L"}, {"value": 250, "unit": "mg/L"}]},
  "quote": "（条文原文，必须是 law_article.text 的子串）"
}
```

标准表的每一格限值做成一条 numeric 规则，条件放在 `applies_when`。求值器不做表格查找。

### 6.3 求值器（`app/rules/evaluator.py`）

- 只支持白名单算子：`== != < <= > >= and or not in between`，以及 `var`。
- 三值：`true / false / unknown`。缺变量是 unknown，不是 false。
- 真值表：
  - `and`：有 false → false；没有 false 但有 unknown → unknown；否则 true
  - `or`：有 true → true；没有 true 但有 unknown → unknown；否则 false
  - `not unknown` → unknown
- 求值前做单位换算（pint）。失败 → unknown。
- 纯函数，无 IO，无随机，无时间依赖。不用 eval。
- 每次求值输出 trace：变量、值、fact_id、比较结果、缺失参数。
- `kind=qualitative` 若被送进求值器，直接拒绝。
- 检出限：`<10` 相对限值 `<= 250` 且 10 已落在限值内 → true。`<300` 相对 `<= 250` → unknown。`未检出` 且没有检出限 → unknown。

审批类型用决策表，对照已人工校对的名录行，不用 LLM。名录变更要能给出类型差异。第一版先留下解析 CSV 与求值器，决策表在名录校对完成后启用。

## 7. 材料解析与参数抽取（后续里程碑）

1. 文档 → 文本 + 页码 / 字符偏移。
2. 参数字典固定。模型只能填字典里的 key。
3. 抽取输出 `[{param_key, value, unit, evidence_span, page}]`。`evidence_span` 必须是源文本子串，数值能在 span 里找到。
4. 人工逐条确认。未确认完，评估接口返回 409。
5. 同一参数冲突 → `conflict`，必须人工选择一条，选中的那条仍带页码。

## 8. 评估（后续里程碑，约束先固定）

对每个 active check_item，且其版本已核验并在基准日有效：

1. `applies_when` 为 false → `不适用`；unknown → `无法判定`。
2. `numeric` 与 `presence` → 规则引擎，`method=rule_engine`。
3. `qualitative` → 同一模型、两个 prompt 各跑一遍，各有各的缓存键。一致才采纳；不一致 → 无法判定，并标「需人工复核」。一致时报告仍标「同源双审」，并建议人工复核。
4. 引用校验器过滤。
5. `result_hash = sha256(规则包哈希 + 已确认参数快照 + 条文 sha + 缓存中的输出)`。

规则引擎路径对同一输入连续运行，`result_hash` 必须一致。定性路径的一致来自缓存命中，不来自 temperature。

## 9. 引用校验器（`app/verify/citation.py`）

1. `quote` 做 NFKC、去掉空白后，必须是对应条文同样规范化后的子串。空串失败。
2. 每个 `evidence_span` 必须等于 `parsed_text[char_start:char_end]`。
3. 引用的版本必须满足「施行日 ≤ reference_date 且状态有效且已人工核验」。
4. 任一项失败 → 不入库，写入 `rejected_finding`。
5. 报告引用格式：`《名称》（版本日期）第X条第X款：「原文」`。

## 10. 输出与界面

- 本机工作台：上传法规、跑种子核验、对照官网确认版本。页眉带免责声明。
- 后续：按审批点分组的报告；左材料、右法条。
- 人工修改写入 `review_log`。

## 11. 验收标准

| 项 | 标准 |
|---|---|
| 引用真实性 | quote、evidence_span 通过子串校验，有单测 |
| 可复现 | 规则引擎同一输入哈希稳定；模型路径同一缓存键命中同一 JSON |
| 缺参数不乱判 | 缺必填参数或比较为 unknown 时，结论为无法判定 |
| 版本正确 | 未核验、待确认、施行日晚于基准日、状态非有效，都不得采用 |
| 规则审核门禁 | draft 规则、定性项都不能从规则引擎得到「符合」 |
| 标题精确 | 短名称不得误绑到包含该短名称的另一部法规 |
| golden set | 后续 30–50 个标注案例。数值项目标准确率 ≥ 99%。定性项统计「误判为符合」的比例，目标趋近 0 |

## 12. 里程碑

当前仓库已经包含：规格、数据模型与迁移、规则引擎、引用校验、人工上传、三类自动采集、核验报告、本机工作台、Windows 启动脚本、下载页。

尚未包含、不要假装已经完成的部分：check_item 的模型抽取与审核页、审批决策表（等名录校对）、材料 OCR 与参数确认闭环、定性双遍评估全流程、双栏高亮与 PDF 导出、录音与地方标准。

后续会话按这个顺序继续：

1. 名录 CSV 的人工校对入口，以及审批类型决策表。
2. `check_item` 候选生成与审核页。draft 不能进评估。
3. 材料解析、参数字典、抽取、子串校验、确认前 409。
4. 评估流水线、同源双审、`result_hash`、`rejected_finding` 接线。
5. 双栏界面与导出。

## 13. 已知风险

- 种子清单里的名称、以及任何一次自动采集看到的日期和状态码，在人工确认前都不是结论。
- 国标全文默认人工上传。
- 地方标准本阶段不覆盖。
- 名录结果只作预审，最终以审批机关意见为准。
- 同源双审会偏乐观。定性项默认建议人工复核。
- 网站改版后自动采集会变成「未找到」。这时用人工上传，不要改校验器。

## 14. 已锁定的产品决定

- 单机自用。无登录、无用户表。
- 有人工审核。没有 `verified_at` 的版本、没有 `active` 的规则，都不参与评估。
- 模型固定为第 3 节的 yaml。起步只用 `deepseek-v4-flash`，思考开，effort 为 high。
- 调用缓存、规则引擎、引用子串校验、参数人工确认，都不能省。
- 事实都从材料里来，并带证据位置。不设无出处的项目画像。基准日和项目名称由使用者指定。
- 自动采集和人工上传都做。
- 对外下载页只提供 Windows 包。包内有免 Docker（SQLite）和 Docker（PostgreSQL + pgvector）两种启动方式。
- 材料默认留在本机。只有填写了 DeepSeek 密钥并执行抽取或定性判断时，相关文本才会发到 DeepSeek。法规核验访问的是公开法规网站，不调用模型。
