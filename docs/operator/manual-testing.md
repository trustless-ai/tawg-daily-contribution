# TAWG Knowledge Bot 手动测试指引

本指引可交给有机器操作权的测试人员执行。目标仓库：<https://github.com/trustless-ai/tawg-daily-contribution>。

## 当前功能

这个版本已经支持：

- 同步 Telegram 群里的所有普通消息，不需要 `@bot`。
- 只有 @bot 或 bot command 才会生成回复。
- 回答 ERC、Trustless AI、组织仓库和相关讨论问题，并引用可靠来源。
- 接受群友更正；bot 会判断证据是否充分，再决定更新知识库。
- 非英文问题用对应语言回答，同时附英文要点。
- 每天 23:00 UTC 生成 Daily Catch-up，总结此前 24 小时，即 `[前一天 23:00, 当天 23:00)`。
- Daily 会实时拉取 GitHub、ERC/EIP 和 Ethereum Magicians 信息，但外部正文不会保存进仓库。
- Telegram 历史和脱敏后的消息会保存；图片、视频本体不保存。
- 知识库使用 acknowledgements/ 表达成员贡献鸣谢。

## 1. 准备环境

```bash
git clone https://github.com/trustless-ai/tawg-daily-contribution.git
cd tawg-daily-contribution
git switch main
git pull --ff-only

python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .

npm install --global @anthropic-ai/claude-code@2.1.240
```

确认：

```bash
python --version
claude --version
python -m pytest -q
python -m tawg_bot.cli vault-lint
git status --short
```

开始运行前，`git status --short` 应为空。

## 2. 设置环境变量

秘密不要写进仓库或 .env 文件。

```bash
export TELEGRAM_BOT_TOKEN='...'
export TAWG_TELEGRAM_CHAT_ID='...'
export TAWG_TELEGRAM_BOT_USERNAME='bot用户名，不带@'

export GITHUB_TOKEN='...'
export GITHUB_REF_NAME="$(git branch --show-current)"

export ANTHROPIC_AUTH_TOKEN='DeepSeek API Key'
export ANTHROPIC_BASE_URL='https://api.deepseek.com/anthropic'
export ANTHROPIC_MODEL='deepseek-v4-pro[1m]'
export ANTHROPIC_DEFAULT_OPUS_MODEL='deepseek-v4-pro[1m]'
export ANTHROPIC_DEFAULT_SONNET_MODEL='deepseek-v4-pro[1m]'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='deepseek-v4-flash'
export CLAUDE_CODE_SUBAGENT_MODEL='deepseek-v4-flash'
export CLAUDE_CODE_EFFORT_LEVEL='max'
export CLAUDE_CODE_AUTO_COMPACT_WINDOW='786432'
```

机器上的 Git 凭据必须有权向当前部署分支（现在是 `main`）推送。运行 tick 时，bot 会自动提交并推送安全的 `data/` 和 `knowledge/` 更新。

Telegram 前置条件：

- bot 是目标群管理员。
- BotFather 中关闭 Privacy Mode。
- 测试 polling 时没有配置 webhook；测试 webhook 时不运行任何 getUpdates 消费者。
- 任一时刻只有一个 Telegram consumer。不要同时运行第二个 getUpdates 消费者。
- 测试期间不要同时运行 GitHub Action。

当前模式由 `TAWG_RUNTIME_MODE` 决定：`poll` 使用 `tick`，`webhook` 只使用
`maintenance-tick`，`observe` 使用对应路径但不发送。Modal 只是调用包装，核心逻辑与
Actions/CLI 共用；完整 shadow、cutover 和 rollback 步骤见
[`Modal webhook operations`](modal.md)。

## 3. 先验证 DeepSeek

```bash
python -m tawg_bot.cli refresh-knowledge --erc 8004 --dry-run
```

预期：

- Claude Code CLI 成功调用 DeepSeek。
- 输出包含 `dry_run=True`。
- 不修改知识库，不 commit，不发送 Telegram 消息。
- 不出现 API Key、Telegram token 或原始外部正文。

---

## 场景一：普通消息只同步，不回复

1. 在 Telegram 群里发送一条不带 @bot 的测试消息，例如：

```text
ROLL-INGEST-001: testing ordinary-message ingestion for the TAWG bot.
```

2. 运行：

```bash
python -m tawg_bot.cli tick --observe-only
```

注意：`--observe-only` 仍然会保存数据、commit 并推送，但绝不会向 Telegram 发消息。

3. 检查：

```bash
rg 'ROLL-INGEST-001' data/telegram
git log --oneline -5
git status --short
test ! -d data/github
test ! -d data/magicians
python -m tawg_bot.cli vault-lint
```

预期：

- 消息出现在 `data/telegram/YYYY/MM/*.jsonl`。
- 群内没有 bot 回复。
- 没有为普通消息创建回复任务。
- Telegram cursor 已更新。
- 没有 `data/github/` 或 `data/magicians/` 外部正文镜像。

---

## 场景二：生成并发布 Daily Catch-up

最好在 23:00 UTC 后测试，这样刚才发送的消息能进入最新的完整窗口。

1. 先预览，不发送：

```bash
python -m tawg_bot.cli daily-dry-run \
  --window-end YYYY-MM-DDT23:00:00Z
```

window-end 必须是已经过去的最近一个 `23:00 UTC`，不能是未来时间。

2. 检查输出：

- 使用英文。
- 时间窗口严格为前 24 小时。
- 语气友善、活跃、有适量 emoji。
- 开头使用独立的加粗标题和斜体 UTC 时间窗口。
- `Highlights` 用引用块提炼 1–4 个协作事件，而不是挑选“最佳贡献者”。
- 重点表达大家工作的价值，不做贡献排名或打分。
- `What moved` 的具体项目要写清谁做了什么、推进了什么，以及为什么对群组或 Trustless AI 有帮助。
- `What moved` 和 `Next up` 要显示为真正的 Rich Markdown 列表，不应挤成一个段落。
- `Next up` 使用 `Ideas to follow` 和 `TODOs`，不使用 `Act`。
- 可以按贡献影响和重要性安排先后，但不能公开写排名、编号优先级或赢家。
- 不应出现独立的 `Appreciation` 区块；认可直接融入 `What moved`。
- 最后有 `Trusty's take`：从 bot 视角点评已经建立的协作事件，用一句不针对个人的俏皮话为全群打气。
- 提到的进展有 Telegram、repo 或可靠外部来源支撑。
- 即使活动很少，也应保持有人情味。

3. 确认群里当前没有待处理的 @bot 消息，然后执行真实发送：

```bash
python -m tawg_bot.cli tick
```

重要：本地直接执行 tick 时，是否发送只由有没有 `--observe-only` 决定。`TAWG_DELIVERY_ENABLED` 是 workflow 的开关，不能阻止本地裸 tick 发送。

预期：

- 群里收到一条 Daily。
- Daily 使用最新实时收集的数据。
- `data/state/delivery-state.json` 记录为 `delivered`。
- `data/state/layer-success.json` 的 L4 时间被更新。
- 相关状态被 commit 并推送。

---

## 场景三：@bot 问答和更正

请在场景二完成后的 30 分钟内测试。这样下一次运行通常是 L1，只处理 Telegram 和回复，不会再次发送 Daily。

### 3A：ERC 问答

在 Telegram 输入 @ 后，从 Telegram 建议列表里真正选择 bot，确保它被标记为 mention：

```text
@bot_username How is ERC-8183 implemented, and which parts are normative versus implementation-specific? Please cite the sources.
```

然后运行：

```bash
python -m tawg_bot.cli tick
```

预期：

- bot 回复原消息，并留在该消息所在的 thread；即使回复拆成两段，两段也都应留在同一 thread。
- 回答区分规范、实现、测试、示例和讨论证据。
- 回答引用可靠链接。
- 不把 Ethereum Magicians 讨论冒充规范文本。
- 如果证据不足，应明确写出未验证点，而不是猜测。

可额外测试多语言：

```text
@bot_username 请用中文解释 ERC-8004 的核心实现和当前证据。
```

预期为中文回复，并附一段英文要点供群内其他成员阅读。

### 3B：提交更正

根据刚才的回答发送一条有依据的更正：

```text
@bot_username Correction: [说明哪里不准确]. The canonical source is [公开可靠链接].
```

再次运行：

```bash
python -m tawg_bot.cli tick
```

预期：

- bot 判断更正是否属于 TAWG/知识库范围。
- 证据充分时直接接受，并更新对应 ERC、topic 或 acknowledgement 页面。
- 证据不足时拒绝或标记为待验证，而不是强行写入。
- 新提供但尚未审核的任意 URL，只能进入 source candidate，不应在同一次对话中立刻被信任。
- 所有人名 ID 只能用于本 TAWG 的检索和鸣谢，不应扩展到其他社区身份。

---

## 最后检查

```bash
python -m tawg_bot.cli vault-lint
git diff --check
git status --short
git log --oneline -10
test ! -d data/github
test ! -d data/magicians
```

## Modal webhook 验收

不要把 token、webhook secret、真实 chat/user ID 或完整 Bot API URL 粘贴到聊天、日志或
仓库。部署本身不会配置 Telegram webhook；`setWebhook`/`deleteWebhook` 都需要单独授权。

1. 先执行 [`modal.md` 的 deterministic fixture gate](modal.md#1-deterministic-fixture-gate)。
   该测试只用 fake IDs/fakes，不调用 Modal、Telegram、GitHub 或付费模型。
2. shadow 阶段保留 GitHub polling，且 `getWebhookInfo.result.url` 必须为空。只检查部署
   metadata，不向已部署 endpoint 发送 synthetic update。当前生产 ingestion 会顺带处理
   所有 actionable reply；因此只有本地 fake 测试可以声称不调用付费模型。此时
   `TAWG_MODAL_MAINTENANCE_ENABLED` 必须精确为 `false`。
3. cutover 时使用 `drop_pending_updates=false`，验证 webhook URL 后立即把
   `TAWG_RUNTIME_MODE` 设为 `webhook`，禁用 scheduled Actions workflow，并确认没有 Action
   run 仍在执行。只有 Actions 已禁用且 idle 后，才把
   `TAWG_MODAL_MAINTENANCE_ENABLED` 改为精确小写 `true`，并从当前 `main` 的精确已审核
   commit 重新部署。然后才发送一条真实普通消息和一条真实 @bot 消息；这是已部署
   endpoint 的第一次验收，@bot 消息会正常使用生产模型和 delivery。完整顺序以
   [`modal.md`](modal.md#webhook-cutover) 为准，不得按本节省略步骤。
4. 在 GitHub `main` 中确认两条 sanitized source record；确认 @bot job 的 delivery 为
   `delivered`、`reply_to_message_id` 等于原消息，且 `message_thread_id` 与原 topic/thread
   一致。再人工确认 Telegram 中的可见回复位于同一消息/thread。
5. 验收期间及通过后都保持 scheduled Actions workflow 禁用；保留 workflow 文件。

失败时不要直接启动 polling。按 [`modal.md`](modal.md#rollback-without-dropping-updates)
先把 maintenance flag 设为精确 `false` 并从精确已审核 `main` 重新部署。pre-delete drain
有助于降低重叠风险，但不能替代强制的 post-delete drain。再执行
`deleteWebhook(drop_pending_updates=false)` 并确认 `getWebhookInfo.result.url` 为空。URL 为空后，
必须确认所有重试均已耗尽，并且 active、queued、retrying 状态的 `repository_worker` call 全部为零。
只有完成这个 post-delete drain，才恢复 `TAWG_RUNTIME_MODE=poll` 和唯一的 scheduled Actions
polling consumer，最后按仓库状态 reconcile pending/ready/ambiguous work。

## 出错时

- 如果 Telegram delivery 状态是 `ambiguous`，不要直接重跑发送；先人工查看群里是否已经收到。
- 如果 getUpdates 报错，检查 webhook、`TAWG_RUNTIME_MODE` 和是否存在另一个 polling 进程；webhook 非空时不要重试 polling。
- 如果模型输出被 schema、privacy 或 citation validator 拒绝，不要绕过校验。
- 如果 cursor 已前进但仓库状态没有成功推送，先修复分支推送问题，不要手动修改 cursor。

## Workflow 运行提醒

workflow 已位于默认分支 `main`，包含每 5 分钟一次的 cron。在 `poll` 模式它消费
Telegram；`webhook` 模式只保留为人工 fallback guard，完成 Modal cutover 后 scheduled
Actions 必须禁用，不能与 Modal maintenance 并行。人工测试期间必须确保没有另一个
`getUpdates` 消费者同时运行；需要在另一台机器测试 polling 时，应先暂停定时 workflow，
测试结束后再恢复。完成 Modal cutover 和真实 threaded delivery 验收后，应禁用 GitHub
polling schedule，让 Modal 成为唯一生产 scheduler。

手动验证 Actions 时，先使用 `workflow_dispatch` input `runtime_mode=observe`。检查提交的脱敏消息、知识页面和状态后，再开启真实 delivery。

[GitHub 手动运行说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[事件触发规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
