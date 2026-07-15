# Infinite Canvas Agent：架构改造成果与剩余工作

> 文档定位：记录 Codex App Server 架构已经取得的成果，明确当前剩余问题，并将下一阶段工作收敛为两个阶段。
> 当前原则：保持上游兼容，以我们的 Agent 能力为主，不改写原画布的鼠标交互、节点 ID、连线 ID 和现有连线数据结构。

---

## 一、当前结论

Codex App Server 主架构改造已经基本完成，不再属于“待验证方向”。

当前实际链路已经是：

```text
Infinite Canvas Agent UI
→ /api/codex-agent/*
→ Canvas Agent Application Logic
→ Codex App Server Runtime
→ Codex App Server Thread / Turn
→ infinite_canvas Dynamic Tools
→ Canvas Backend / Generation Provider
```

画布 Agent 已经具备：

- App Server 启动和运行时管理。
- Codex Thread 创建、恢复和失效后重建。
- 流式回复、工具调用事件、停止和错误处理。
- Canvas Agent 独立对话历史和后台任务恢复。
- 最小上下文 envelope 和服务端画布快照。
- 基于 App Server `dynamicTools` 的画布查询、操作和生成能力。
- 图片和视频任务提交、轮询、写回、重试和恢复。
- 审批策略、高风险操作确认、撤销快照和删除墓碑。
- 选中节点、附件和 `图1/ref_1` 有序引用。
- 与原智能画布通过 `window.SmartCanvasAgentApi` 窄桥接集成。

因此，下一阶段不再重做 App Server POC，而是进入：

```text
第一阶段：架构收口 + 上下文 + Revision + Skills
第二阶段：/批量任务 + 后台并发队列执行引擎
```

---

## 二、已完成的架构成果

### 2.1 App Server 已成为 Agent 主运行时

现在的 Canvas Agent 由本地 Codex App Server 驱动，已经支持：

- `thread/start`
- `thread/resume`
- `turn/start`
- 流式事件转发
- 原生 Dynamic Tool Call
- 后台 Turn 执行与页面重连

旧文档中“CLI 保留为回退”已经属于过时信息，不列入后续目标，也不围绕它继续设计。

### 2.2 画布查询和操作已进入 Dynamic Tools 主路径

当前 Agent 已经可以通过 `infinite_canvas` 命名空间查询：

- 选中节点。
- 视口节点。
- 全画布概要。
- 节点详情。
- 节点搜索。
- 上下游连接。
- 整理布局上下文和完整节点树。
- 生成任务列表和任务详情。

也可以通过原生工具完成：

- 创建媒体、提示词、文本和循环节点。
- 创建生图/视频节点。
- 直接提交图片/视频生成。
- 重命名、块级移动、尺寸恢复/标准化、横纵宫格排列、节点树排列、分组、取消分组。
- 删除、撤销、生成任务取消与重试。
- 写入用户偏好。

`canvas_agent_action` 仅是 Dynamic Tools 注册不可用时的协议兼容路径，不是后续主方向。

### 2.3 上下文已经从“全量 Prompt”改成“最小引用 + 按需查询”

每轮发送时，前端仍会捕获发送时的画布和视口快照，但完整快照保存在服务端，不直接塞入 Codex Prompt。

Codex 收到的主要是：

```text
project / canvas
context_snapshot_id
viewport_snapshot_id
context_level
context_intent
selected refs
node counts
approval policy
provider defaults（仅生成意图）
用户原始消息
```

需要具体节点时，Agent 再通过 Canvas Tools 查询快照或实时画布。

### 2.4 Agent 与原画布的隔离边界已建立

- Agent 前端主要位于 `static/js/codex-agent-panel.js` 和 `static/css/codex-agent-panel.css`。
- 普通画布不加载 Agent 面板。
- 智能画布仅通过窄桥接对 Agent 暴露能力。
- Agent 移动和排列只修改 `node.x` / `node.y`；明确要求整理视觉尺寸时才写入或清除节点显示 `w/h`。
- Agent 素材重命名默认只修改 `node.images[index].name`。
- 不改原节点 ID、连线 ID 和连线数据形状。

---

## 三、不再列入下一阶段的事项

### 3.1 不再规划 Legacy CLI 回退

文档不再保留“App Server 失败时回到 CLI”的目标。现有兼容代码如果尚在，不必为本轮专门处理，但新能力不能继续依赖它。

### 3.2 不实现画布内 Codex 登录流程

使用本画布 Agent 的前置条件是：

```text
用户已安装 Codex 桌面版，并已完成登录。
```

“不实现登录流程”不等于忽略未就绪状态。启动 Agent 时必须检测 Codex 桌面版/App Server 是否可用：

- 检测成功：进入正常 Agent 面板。
- 未安装、未登录或 App Server 不可启动：Agent 面板进入阻塞式提示页，禁用对话和 Agent 工具。
- 提示页说明“安装/打开 Codex 桌面版、完成登录后重试”，并提供重新检测。
- 不在 Infinite Canvas 内实现 OAuth、Token 保存或登录表单。

阻塞范围仅是 Canvas Agent，原画布的手动功能仍可使用。

### 3.3 暂不扩展多 Runtime 和多 Agent 平台

本阶段不实现 Ollama、Claude、Gemini 等 Agent Runtime，也不建立泛化的多 Agent 框架。

`/批量任务` 使用独立后台队列执行器，但这是 Canvas Agent 的专用子系统，不扩展成通用多 Agent 平台。

---

## 四、架构成果：已建立适度拆分的 `agent/` 目录

### 4.1 结论

在项目根目录新建：

```text
agent/
```

已将适合独立、且与本轮优化直接相关的 Canvas Agent 后端逻辑迁入。没有按原文档拆成过多 `application/runtime/codex/context/tools/preferences` 小层级，当前实际结构为：

```text
agent/
├── __init__.py
├── backend.py       # Agent 状态、历史、Dynamic Tools、生成编排、Turn 与全部 API 路由
├── runtime.py       # Codex App Server session/runtime，线程与 turn 事件
├── context.py       # 最小 envelope 与 refs 规范化
├── canvas_skills.py # `/` 命令注册、路由和 Skill 按需加载
├── revision.py      # Agent 私有 Revision 与结构化冲突
└── skills/          # Canvas 专用 Skills
```

Skills 是独立的扩展资源，不硬编码进 Python 业务文件。Canvas 专用 Skills 放在产品自己的目录：

```text
agent/skills/infinite-canvas-*/
```

它们由 Canvas Agent 命令注册表按需加载，不默认注册成桌面版 Codex 的通用项目 Skills，避免污染普通 Codex 任务。

### 4.2 `main.py` 的最终职责

当前上游 `main.py` 仍是大型单文件（约 1.7 万行），原作者将 FastAPI 路由、Canvas 存储、Provider、生成、工作流和配置等主体功能都保留在这里。为了日后合并，我们不应为了自身模块化去搬动这些上游代码。

`main.py` 继续保留：

- 上游已有的 FastAPI App 组装和全部原有路由。
- 原画布加载/保存、素材、Provider、生图、视频和工作流函数。
- Agent 后端所需的上游依赖装配、Agent Router 挂载和启动钩子注册。
- Agent 需要复用的上游生成入口，不复制到 `agent/`。

`agent/` 只迁移我们新增且能以明确边界识别的 Agent 实现。本轮已迁移 App Server Runtime、最小上下文构造、命令/Skill 路由、Revision，以及原先集中在 `main.py` 末尾的状态、历史、Dynamic Tools、生成编排、Turn 和 API 路由。

Agent 生成模块只负责编排，底层继续调用 `main.py` 中与上游一致的 Provider/生成实现。拆分时优先使用显式依赖或窄服务接口，避免 `main.py` 与 `agent/` 循环导入。

这样能把我们的 Agent 主体与上游冲突热区分离，同时保持 `main.py` 的上游形状，方便以后持续合并。

### 4.3 后续拆分原则

当前不再为了目录完整而把 `backend.py` 拆成许多小层。只有某一块 Agent 逻辑形成稳定边界、且能显著降低与上游的合并冲突时，才新增高内聚模块；始终保持 API 路径、请求/响应格式和画布数据结构不变。

---

## 五、继续优化上下文

本轮上下文瘦身和规范化已经完成，后续只做基于真实使用数据的增量调整，不重做上下文系统。

### 5.1 剩余问题

当前隐藏 envelope 仍包含一些每轮重复的：

- 工具列表文字。
- 生成路由提醒。
- 高风险规则说明。
- 位置语义规则。
- 与 Dynamic Tool schema 重复的能力说明。

这些信息应分别迁移到：

```text
永久不变的安全边界 → AGENTS.md / Agent 基础指令
工具能力和参数       → Dynamic Tool schema
专项任务流程           → Skills
本轮画布状态           → Snapshot + Canvas Tools
本轮最小引用           → Context Envelope
```

### 5.2 下一版 Context Envelope

目标结构：

```text
canvas_id
conversation_id
context_snapshot_id
viewport_snapshot_id
canvas_revision
context_intent
approval_policy
ordered_refs
provider_default_ref（仅生成意图）
```

不再在 envelope 内完整列出工具名和大段使用说明。

### 5.3 上下文路由改造

把前端的正则推断从“决定业务逻辑”降级为“轻量提示”：

- 前端可继续提供 `context_intent` 建议。
- 服务端负责校验 command、refs、scope 和任务类型。
- Skill 或 Dynamic Tool 决定需要查询哪一层画布数据。
- 不因出现“节点”等单个词就自动将上下文提升到全画布。

### 5.4 快照生命周期

增加明确的：

- 快照 TTL 和定期清理。
- 快照与 `canvas_revision` 绑定。
- 任务结束后仅保留审计所需的最小摘要。
- 批量任务引用的快照不被普通 TTL 过早清理。
- 临时图片引用仍不进入用户项目目录。

### 5.5 上下文优化的验收指标

- 纯聊天不附带画布节点列表、Provider 列表和工具列表文字。
- 局部节点任务不查询全画布。
- 全画布任务通过分页工具查询，不将整份 Canvas JSON 塞入对话。
- 工具 schema 已说明的信息不再二次重复。
- 能记录每轮上下文级别、快照使用次数和大小，便于后续对比优化效果。

---

## 六、Canvas Revision 的作用与实现计划

### 6.1 Revision 解决的问题

Dynamic Tools 解决的是：

```text
Agent 怎么读画布、怎么操作画布。
```

Canvas Revision 解决的是：

```text
Agent 读到的画布在执行时是不是已经过期。
```

例如：

```text
Agent 在 revision=10 查询到了 20 个节点
用户随后删除、移动或新建了节点
画布变为 revision=12
Agent 尝试按 revision=10 的计划执行
→ 应要求重新查询或对受影响对象做冲突解析
```

当前的发送时快照、实时重新解析节点、丢失节点跳过和删除墓碑已经提供了局部保护；Revision 是把这种保护升级为统一、可验证的并发协议。

### 6.2 Revision 规则

- 每个画布有单调递增的 `canvas_revision`。
- 任何会影响 Agent 判断的画布持久化变更都递增 Revision。
- 纯 UI 变化（如面板展开状态）不递增。
- 查询工具返回 `canvas_revision`。
- 写工具接受可选 `expected_revision`。
- 批量和高风险写操作必须提供 `expected_revision`。
- 写入成功后返回新 Revision。

### 6.3 冲突策略

```text
无冲突
→ 执行并返回 new_revision

Revision 变化，但目标节点未变
→ 允许进行一次安全重解析后执行

目标节点被删除或关键属性变化
→ 跳过该项，返回 structured conflict

全画布排列、删除、批量 DAG 扩展等大范围变更
→ 停止当次提交，重新查询并重算计划
```

### 6.4 兼容性

Revision 优先作为 Agent 私有元数据存储，不改动原画布节点和连线结构。

如果最终需要将 Revision 写入画布根数据，只增加一个可选根字段，旧画布缺失该字段时以 `0` 启动，不修改任何现有字段语义。具体落盘方式在实现前通过最小原型确定。

---

## 七、Skills 专项拆分和 `/` 命令规范化

这是第一阶段的重点成果之一。

### 7.1 当前成果

现有 `/` 命令已经由后端统一注册，前端按注册表渲染：

```text
/整理
/重命名
/生成提示词
/创建生图节点
/创建视频节点
/总结画布
/搜索节点
/批量任务
```

每个命令只加载对应 Canvas Skill，并通过严格 Dynamic Tool 契约查询或修改画布。`/整理` 已进一步支持布局分析、尺寸标准化、块级空位移动、横纵宫格和递归节点树布局。

### 7.2 命令与 Skill 分层

```text
/ 命令
→ 用户可见入口，负责发现、参数提示和语义路由

Canvas Skill
→ 专项任务的流程知识、默认策略、校验标准和失败处理

Dynamic Tool
→ 结构化查询与执行原语

Provider Adapter
→ 真正的图片/视频/模型调用
```

`/` 命令不应复制工具实现，Skill 也不应直接写画布 JSON。

### 7.3 统一命令注册表

已经建立由后端提供、前端渲染的命令注册表，包含：

```json
{
  "id": "batch_task",
  "command": "/批量任务",
  "aliases": [],
  "title": "批量任务",
  "description": "规划独立生成节点并按平台并发排队执行",
  "skill": "infinite-canvas-batch-task",
  "intent": "batch_task",
  "default_scope": "canvas",
  "requires_agent": true,
  "parameters": [],
  "risk": "expensive"
}
```

注册表负责：

- 命令名、别名和展示文案。
- 对应 Skill。
- 默认 context intent 和 scope。
- 可选的结构化参数。
- 是否需要 Agent 规划。
- 风险等级和默认审批方式。
- 所需 Dynamic Tools 能力。

前端不再硬编码 `slashCommands` 的完整业务信息，只保留拉取、搜索、选择和渲染。

### 7.4 首批 Canvas Skills

不要对每个小工具建一个 Skill。首批按任务族拆分：

```text
infinite-canvas-organize
→ 整理、移动、分组、布局策略

infinite-canvas-asset-naming
→ 批量重命名和命名规则

infinite-canvas-prompt-workflow
→ 提示词生成、反推、拆镜和提示词节点组织

infinite-canvas-generation
→ 生图/视频节点创建、默认 Provider、参考图和生成校验

infinite-canvas-analysis
→ 画布总结、节点语义搜索、上下游理解

infinite-canvas-batch-task
→ 独立生成节点规划、一次确认和 Provider 并发队列
```

Skill 只在任务命中时加载，不把全部 Skill 内容注入每轮对话。

Canvas Skills 默认只在 Infinite Canvas 的画布 Agent 中生效。它们不作为普通 Codex 桌面任务的全局 Skills 安装；画布 Agent 通过自己的命令注册表选择并加载。与此同时，画布 Agent 仍可以使用用户已安装的通用 Codex Skills，两者不混为一套。

为了后续扩展，Canvas Skill 的输入、输出和所需工具都使用结构化契约。未来可以将底层 Canvas Tools 封装成 MCP Server，让其他 Agent 或外部客户端调用；但现阶段仍以画布 Agent 内部使用为主。

### 7.5 扩展性

后续新增命令的标准步骤应为：

1. 增加或复用一个 Skill。
2. 在命令注册表增加一项。
3. 如果缺少执行原语，再增加 Dynamic Tool。
4. 不修改前端意图正则，不复制一套单独业务逻辑。

---

## 八、第二阶段功能：`/批量任务`

> 当前范围已简化：只做“主 Agent 规划完整节点清单 → 一次状态卡确认 → 后台按 Provider 并发排队创建并执行”。多阶段 DAG、子 Agent、内容验收和自动纠错不在当前范围。

### 8.1 产品目标

`/批量任务` 用于把多个相互独立的生图或视频节点一次规划清楚，再交给后台队列按平台安全并发执行。主 Agent 只负责规划，不轮询 Provider，也不逐个调用普通生成工具。

典型任务：

```text
写一份广告脚本
→ 拆成 20 个镜头
→ 为每个镜头生成分镜提示词
→ 分批调用 GPT 生成分镜图
→ 检查分镜图质量，对不合格项重生
→ 将合格分镜图作为上游
→ 分批调用即梦生成视频
→ 跟踪失败项并重试
→ 直到任务达到预设完成标准
```

### 8.2 任务节点

新增一种 Agent 专用画布节点：

```text
任务节点（Complex Task Node）
```

约束：

- 只能由 Agent 通过 `/批量任务` 或专用 Dynamic Tool 创建。
- 不出现在手动“新建节点”菜单。
- 用户可以在画布上查看、移动、打开、暂停、继续和取消它。
- 节点展示总进度、当前阶段、成功/失败/重试数、预计待处理数和最近错误。
- 任务的真实执行状态保存在服务端数据库，不仅依赖画布节点内存。
- 服务重启或画布关闭后可恢复。

节点类型和字段必须是增量扩展，不修改现有节点的字段语义。

### 8.3 已取消的历史方案（存档）

本节至 8.10 记录的是此前的多阶段 DAG / Agentic 设计，已被 8.1 的扁平批量队列方案取代，不再作为当前实现或验收目标。保留文字只用于解释方案演变。

任务节点由后台 Complex Task Engine 执行，不要让主 Agent 一直轮询等待。

职责分工：

```text
主 Agent
→ 理解需求、补齐关键参数、生成任务规格和初始 DAG
→ 创建任务节点
→ 立即释放，继续为用户处理其他事情

Complex Task Engine
→ 按依赖、并发配额和重试策略执行任务
→ 更新任务节点和下游节点
→ 记录可恢复状态

任务 Agent（仅 Agentic 模式）
→ 只在规划、阶段验收、结果判断和纠错时被唤醒
→ 不负责毫无意义的轮询和排队
```

任务 Agent 可使用与主 Agent 分离的 Codex Thread/任务上下文，但不应为每个生图节点创建一个 Codex Thread。实际 Provider 生成仍由画布生成任务层执行。

### 8.4 两种执行模式

#### A. Deterministic（程序化模式）

适合：

- 建立 20 或 100 个节点。
- 每个节点的提示词、参考图和参数都已明确。
- 只需要按顺序或固定并发数完成。
- 无需 Agent 判断结果内容。

流程：

```text
主 Agent 一次性编排任务
→ 任务引擎校验参数
→ 按顺序/并发规则调用现有生成任务 API
→ 等待结果并推进 DAG
→ 结束
```

运行期不需要 Agent 参与。

#### B. Agentic（任务 Agent 监督模式）

适合：

- 先拆解内容，再生图，再生视频。
- 下一阶段取决于上一阶段结果。
- 需要判断图片是否符合镜头、人物或产品要求。
- 失败后需要修改提示词或选择替代路径。

任务 Agent 仅在这些 checkpoint 被唤醒：

```text
初始规划
阶段结果齐备
结果验收
多次失败或结果不合格
需要重写提示词
需要修改后续 DAG
最终验收
```

### 8.5 任务规格与 DAG

主 Agent 不直接输出大段不可执行的自然语言计划，而是产生结构化 `ComplexTaskSpec`：

```json
{
  "title": "20 镜广告片",
  "mode": "agentic",
  "canvas_id": "...",
  "base_revision": 42,
  "link_visibility": "visible",
  "stages": [
    {
      "id": "storyboard",
      "type": "generate_image",
      "items": 20,
      "depends_on": ["prompts"],
      "provider_policy": "gpt_image_safe",
      "acceptance": {"review": "agent", "max_attempts": 2}
    },
    {
      "id": "videos",
      "type": "generate_video",
      "items": 20,
      "depends_on": ["storyboard"],
      "provider_policy": "jimeng_safe",
      "acceptance": {"review": "provider_success", "max_attempts": 2}
    }
  ]
}
```

真实规格还需要包含：

- 每个 item 的提示词、参考素材、参数和预期输出。
- 依赖关系和解锁条件。
- 生成节点与画布节点的对应关系。
- 并发、超时、重试和退避策略。
- 阶段完成标准。
- Agent 可以修改的范围和最大自动重试次数。
- 费用/数量上限和需要再次确认的阈值。

DAG 允许：

- 一个阶段全部完成后再进入下一阶段。
- 每个镜头的图一完成，就单独解锁该镜头的视频任务。
- 一个输出作为多个下游任务的参考。
- 部分失败不阻塞无依赖的其他分支。

### 8.6 Provider 并发调度

当前普通生成任务可以大量同时发起，存在 429、超时、排队拥堵和重复扣费风险。`/批量任务` 必须建立统一调度器。

调度器至少包含：

- 全局最大并发。
- 按 Provider 的最大并发。
- 按模型或任务类型的可选并发。
- 图片和视频独立配额。
- 上游排队任务不重复提交。
- 429/限流指数退避和抖动。
- 可恢复的超时轮询。
- 连续失败时的 Provider 熔断。
- 暂停、继续、取消和服务重启恢复。

安全默认值由配置表管理，不散落在 Skill 或提示词中硬编码。初始可采用：

```text
GPT 生图：并发 3，经验证后最高放宽到 4
视频 Provider：根据平台排队和限制分别设置
未知 Provider：并发 1
```

一批 3～4 个任务成功或进入可轮询状态后，再递补下一个，并非一定要等整批全部完成才启动下一批。

### 8.7 画布节点和连线

所有实际任务节点都按上下游关系建立现有格式的连线：

```text
任务节点
→ 提示词节点
→ 生图节点
→ 生视频节点
```

具体规则：

- 复用现有连线 ID 和连线数据结构，不发明 Agent 专用连线格式。
- 任务节点是整个 DAG 的可见根节点。
- 每个下游节点记录对应 task item，便于定位、重试和恢复。
- 连线显示开关只影响渲染，不删除实际连接关系。
- 任务节点可设置：全部显示、只显示阶段主干、全部隐藏。
- 隐藏连线后，Agent 查询上下游时仍可以读取完整 DAG。

### 8.8 节点创建策略

不要在规划完成时就把 100 个运行中生成节点一次性全部提交。

区分：

```text
可见占位节点
→ 可以按需预先建立，让用户看到计划

真实 Provider 任务
→ 只在调度器发放令牌后提交
```

对超大任务，默认采用渐进式展开：

- 先创建任务节点和当前阶段必要节点。
- 随执行进度创建下一批。
- 支持“展开全部计划”，但展开不等于同时提交。
- 自动布局以任务节点为根，按阶段分列/分层排布。

### 8.9 验收、纠错和重生

验收分两层：

#### 程序化验收

- Provider 任务是否成功。
- 输出文件是否存在。
- 媒体类型、尺寸、时长和数量是否正确。
- 是否通过内容审核。
- 是否已完成写回和连线。

#### Agent 内容验收

- 是否符合镜号描述。
- 人物、产品、场景和风格是否连续。
- 是否存在明显构图、文字或内容错误。
- 是否需要修改提示词后重生。
- 该输出是否可以解锁视频阶段。

每个 item 必须有 `max_attempts`、错误分类和重试原因。达到上限后不能无限重生，而是将任务标记为“需要用户决策”或按已允许的降级方案继续。

### 8.10 任务状态机

```text
draft
→ planned
→ queued
→ running
→ reviewing（Agentic 模式可选）
→ completed
```

分支状态：

```text
paused
waiting_provider
waiting_user
retrying
partially_completed
failed
cancelled
```

任务、stage 和 item 都应有独立状态，但 UI 只向用户展示必要层级。

### 8.11 Canvas Revision 在批量任务中的作用

批量任务执行时间长，Revision 在此处不是可选增强，而是基础依赖：

- 任务规划记录 `base_revision`。
- 每次展开新阶段前读取当前 Revision。
- 区分用户修改、任务引擎自身修改和无关修改。
- 用户删除或修改了任务所依赖的节点时，只暂停受影响分支。
- 可安全重解析的位置变化不阻塞生成任务。
- 不能安全合并的结构变化进入 `waiting_user`。

---

## 九、`/批量任务` 的分步实现计划

### 9.1 Phase 2A：任务节点与持久化骨架

状态：首版已完成。

- 定义 `ComplexTaskSpec`、Stage、Item 和状态机。
- 新增 Agent 专用任务节点，不加入手动创建菜单。
- 建立任务、阶段、item、attempt 和 event 持久化表。
- 实现查询、暂停、继续、取消和服务重启恢复。
- 实现任务节点进度 UI。

验收：可创建一个不执行的 20 项任务计划，关闭并重启服务后仍可恢复。

### 9.2 Phase 2B：批量队列执行器

状态：首版已完成，真实 Provider 长稳压测待继续。

- 实现依赖解锁和顺序执行。
- 复用现有生图/视频任务 API。
- 实现 Provider 并发令牌和排队。
- 实现错误分类、退避、重试和熔断。
- 实现任务节点到下游节点的连线和布局。
- 已补齐逐项目实时进度广播；单项失败不会占用 Provider 并发槽，未尝试项目优先于延迟重试项。
- 已统一任务节点与提示词节点的默认尺寸，并提供独立节点日志弹窗。
- 批量生成节点已保留提示词、参考图及 Provider/模型/尺寸等运行参数。

验收：给定 20 组不同提示词/参考图，GPT 生图并发始终不超过安全配置，中途重启后不重复扣费并能继续完成。

### 9.3 Phase 2C：多阶段链路（已取消）

状态：不在当前产品范围；不作为后续实现目标。

- 支持“提示词 → 图片 → 视频”。
- 上游输出自动成为下游参考输入。
- 支持按 item 流水解锁和按 stage 整体解锁。
- 支持连线全显示/主干/隐藏。
- 支持局部失败后从指定节点或阶段重跑。

验收：一个 5 镜头小型流程可以从提示词一直运行到视频，画布连线与真实数据依赖一致。

### 9.4 Phase 2D：Agentic 验收和纠错（已取消）

状态：不在当前产品范围；批量任务不创建子 Thread。

- 为任务节点建立独立任务 Agent 上下文。
- 实现阶段 checkpoint 唤醒，不持续占用 Agent。
- 使用结构化验收结果：`accept` / `retry` / `revise_prompt` / `block` / `ask_user`。
- 实现最大重试次数和预算边界。
- 支持修改未执行的后续 DAG，不篡改已完成记录。
- 任务需要用户决策时，在任务节点和 Agent 面板中显示明确问题。

验收：完成“20 镜广告脚本 → 分镜图生成 → 质量验收/重生 → 即梦视频”的端到端工作流。

---

## 十、历史优化完成记录

本轮收口结果：

1. 已建立根目录 `agent/` 包，并按第四节的适度粒度拆分 `main.py`。
2. 已保持 `/api/codex-agent/*` 和前端调用契约不变。
3. 已完成 Context Envelope 二次瘦身。
4. 已建立 `canvas_revision` 和结构化冲突返回。
5. 已建立统一 `/` 命令注册表，前端改为动态加载、失败时本地回退。
6. 已将现有专项流程拆分成不过细的 Canvas Skills。
7. 已将公开入口收敛为唯一的 `/批量任务`，不保留旧名称或别名。
8. 已通过快照中的上下文级别、Revision 和结构化工具结果保留基础诊断信息；更完整的统计面板不属于本轮历史问题修复。

第一阶段完成标准：

- Agent 主体逻辑已迁入 `agent/`；`main.py` 只保留显式上游依赖装配、`include_router` 和启动钩子连接，原项目其他部分不做额外拆分。
- 现有 Agent 功能、历史、生成任务和审批流程无回归。
- 纯聊天上下文进一步减少。
- Dynamic Tools 不依赖 envelope 中的重复工具列表。
- Revision 能阻止一次可复现的过期全画布操作。
- `/` 命令可通过单一注册点扩展。
- 专项 Skill 只在命中相关任务时介入。

当前实施进度：

- 已建立 `agent/context.py`、`agent/canvas_skills.py`、`agent/revision.py` 和 `agent/skills/`，先拆出与优化直接相关的高内聚部分。
- 已将 Codex App Server session/runtime 迁入 `agent/runtime.py`，通过显式依赖连接 `main.py`，没有循环导入。
- 已将约 5100 行 Agent 后端主体迁入 `agent/backend.py`；`main.py` 从约 22400 行回落到约 17300 行，并且不再定义 `_codex_agent_*`、Agent 请求模型或 Agent API 路由。
- 已上线 v2 最小 envelope，删除旧全量上下文构造代码。
- 已将命令注册表改为后端单一真值源，并按任务只加载一个 Canvas Skill。
- 已将 Provider/模型列表改为 `get_generation_settings` 按需查询。
- 已建立 Agent 私有 Canvas Revision，不修改原画布 schema。
- 已新增 `agent/complex_tasks.py`，`main.py` 仍只负责 Agent Router 和上游依赖装配。
- 已建立 `complex_tasks`、`complex_task_stages`、`complex_task_items`、`complex_task_attempts`、`complex_task_events` 五张持久化表。
- 已上线 `smart-agent-task` 批量节点、任务详情面板和进度控制；任务节点不在手动新建菜单中。
- 已接入扁平批量清单、一次确认卡、Provider 并发、有限重试、抖动退避、熔断和重启恢复；运行期不创建子 Thread。
- 已实现 `create_batch_task`、`get_batch_task`、`control_batch_task` Dynamic Tools；内部持久化接口继续复用既有任务存储路径。

结论：历史优化目标已经完成，`/批量任务` 的规划、一次确认、任务节点和后台并发队列已形成闭环。下一步重点是使用真实 Provider 做长时间、大批量、断网和服务重启压测，并根据平台实测调整并发与恢复策略。

---

## 十一、第二阶段验收标准

`/批量任务` 完成不以“能一次创建很多节点”为标准，而以下列端到端能力为标准：

1. Agent 可从自然语言产生可验证的 `ComplexTaskSpec`。
2. 画布会出现只能由 Agent 创建的任务节点。
3. 主 Agent 提交后立即释放，任务在后台独立运行。
4. Deterministic 模式能稳定执行 100 项不同参数的节点任务。
5. 同一 Provider 的并发始终不超过安全配置。
6. 服务重启、页面关闭和网络短时中断不会造成静默重复提交。
7. 运行期不创建 Codex 子 Thread，也不会退回普通生成工具逐项提交。
8. 任务节点、提示词节点、生图节点和视频节点按真实依赖连线。
9. 连线可隐藏但依赖不丢失。
10. 用户可暂停、继续、取消、重试失败分支和从指定阶段重跑。
11. Revision 冲突只暂停受影响分支，不破坏已完成的其他结果。
12. 有限重试、数量/费用上限和需要用户介入的明确终止点。

---

## 十二、总体方向

现在的架构主线已经建立：

```text
Codex App Server
+ 最小上下文
+ Dynamic Canvas Tools
+ 后台生成任务
```

下一阶段不再改变这条主线，而是在它之上完成：

```text
适度代码隔离
+ 上下文继续瘦身
+ Canvas Revision
+ Skills / 命令规范
+ 批量任务 DAG
+ Provider 安全调度
+ 可恢复后台任务 Agent
```

最终要达到的产品形态是：

> 主 Agent 负责理解与编排；任务节点负责长时间、可恢复、受并发控制的执行；任务 Agent 只在需要判断和纠错时介入。
