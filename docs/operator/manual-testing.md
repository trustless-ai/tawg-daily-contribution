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
git switch feature/tawg-knowledge-bot

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
export GITHUB_REF_NAME='feature/tawg-knowledge-bot'

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

机器上的 Git 凭据必须有权向 `feature/tawg-knowledge-bot` 推送。运行 tick 时，bot 会自动提交并推送安全的 `data/` 和 `knowledge/` 更新。

Telegram 前置条件：

- bot 是目标群管理员。
- BotFather 中关闭 Privacy Mode。
- 没有配置 webhook。
- 不要同时运行第二个 getUpdates 消费者。
- 测试期间不要同时运行 GitHub Action。

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
- 重点表达大家工作的价值，不做贡献排名或打分。
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

- bot 回复原消息。
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

## 出错时

- 如果 Telegram delivery 状态是 `ambiguous`，不要直接重跑发送；先人工查看群里是否已经收到。
- 如果 getUpdates 报错，检查 webhook 和是否存在另一个 polling 进程。
- 如果模型输出被 schema、privacy 或 citation validator 拒绝，不要绕过校验。
- 如果 cursor 已前进但仓库状态没有成功推送，先修复分支推送问题，不要手动修改 cursor。

## Workflow 上传提醒

当前 workflow 含有每 5 分钟一次的 cron。先完成上述本地测试，再上传并启用定时任务。

GitHub 要求可手动触发的 workflow 存在于默认分支，而定时 workflow 也只会在默认分支运行。因此在代码尚未合并 main 之前，不要把带 cron 的原文件直接启用在 main；否则它会每 5 分钟运行 main 上的版本。可以先移除 `schedule`，仅保留 `workflow_dispatch` 做 Actions 验证，正式合并后再恢复 cron。

[GitHub 手动运行说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[事件触发规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
