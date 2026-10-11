# 900万 PDF 项目：QA 与 Developer 闭环

这个目录是闭环的配置和判断程序。Developer 自动修复保持关闭。仓库变量 `QA_AUTOFIX_MODE` 不在本变更里设置，因此 GitHub Action 默认不会读审查，也不会调用 webhook。

## 已经成立的部分

QA 自动化「900万PDF项目 - 自动QA代码审核」是启用的。

- ID：`fdc3199e-c4b6-11f1-ac31-5e2d0494121f`
- 页面：https://cursor.com/automations/fdc3199e-c4b6-11f1-ac31-5e2d0494121f
- 它已经审查过 PR #1。`dd3011a2364038f5bf965b892fbf7e31f5831e63` 的结论是 FAIL，随后的 `b1768ab49472eeb6a2a61c9215c1fca6fa3bc609` 是 PASS。
- 提示词写明由 PR 创建或更新触发。Developer 推送到原 PR 后，原生的 Pull request pushed 会再次启动这次独立 QA。这一段不依赖机器人评论。

API 只返回自动化的名称、启用状态和所有者，没有返回触发器列表。保存前仍要在页面上确认 QA 只有 Pull request opened 和 Pull request pushed。

已创建但未启用的 Developer 自动化没有运行记录。当前接口不能按名称列出未运行的自动化，所以这里没有它的 ID，也没有改它。

## 不能当作已经接通的部分

Cursor 员工已经说明：机器人和 GitHub App 的 PR 评论会在触发器判断之前被过滤，选 Anyone 也一样。QA 的审查作者是 `cursor[bot]`，因此下面几条都不能用来在 FAIL 时启动 Developer：

- Comment added
- Issue comment
- PR review comment
- 让 QA 在评论里写 `@cursor`

PR review submitted 的作者同样是 `cursor[bot]`。本方案不使用它。没有证据表明它能绕过这层过滤。

CI completed 也不能表达审查结论。PR #1 的 PASS 提交 `b1768ab` 上，检查 `Cursor Automation: 900万PDF项目 - 自动QA代码审核` 的 conclusion 是 `success`，它表示这次自动化跑完了。FAIL 提交 `dd3011a` 上没有对应检查。两条审查的 GitHub review state 都是 `COMMENTED`。检查结论区分不了 PASS、FAIL 和 NOT VERIFIED。

标签触发也没有采用。同一标签重复添加不会再次产生 label changed；机器人添加的标签是否进入 Cursor 触发器也没有被验证。

因此，FAIL 到 Developer 的衔接不是一条已经生效的原生触发器。可靠的官方替代是 webhook：GitHub Action 读到已经落在 GitHub 上的审查，再 POST 到 Developer 自动化的私有 webhook。这个 Action 已经写好，但默认不武装。

## 目标行为

| 事件 | 结果 |
| --- | --- |
| Developer 或人工推送到 PR | 已启用的 QA 按 PR opened / pushed 独立审查 |
| QA 结论 PASS | 不启动修复，等待人工验收。同一个 SHA 上后续 FAIL 也不再自动修 |
| QA 结论 NOT VERIFIED | 暂停。不叫 Developer，也不算通过，不占用修复轮次 |
| QA 结论 FAIL | 只有武装后才可能启动一轮修复 |
| 同一时刻 | 一个 PR 最多一个未完成修复。新的 FAIL 先挂起，等完成标记后再决定 |
| 同一个 SHA | 只修复一次 |
| 整个 PR | 最多 3 轮。第 4 个新的失败 SHA 停止 |
| 任何路径 | 不合并 `main` 或 `feature/cloud-development`，不批准 PR |

判断代码在 `bridge.py`。测试在 `tests/test_automations/test_qa_autofix_bridge.py`。

## 人工配置顺序

1. 保持 Developer 自动化关闭。
2. 打开它，只留 Webhook 触发器，删除评论、推送、CI 和定时触发器。工具不要批准，不要合并。把 `prompts/developer-autofix.md` 的提示词贴进去。保存后它仍应是 Disabled。复制 webhook URL 和 API key。
3. 把 QA 提示词换成 `prompts/qa-reviewer.md`。触发器仍只保留 PR opened 和 PR pushed。不要让 QA 持有 webhook key。
4. 在仓库 Settings → Secrets and variables → Actions 里新增两个 secret，值不要带换行：

```bash
printf '%s' 'https://api2.cursor.sh/automations/webhook/<developer-automation-id>' | gh secret set CURSOR_DEVELOPER_WEBHOOK_URL --repo The-sky1234/pdf-classification-9m
printf '%s' '<webhook-api-key>' | gh secret set CURSOR_DEVELOPER_WEBHOOK_TOKEN --repo The-sky1234/pdf-classification-9m
```

5. 先做下面的观察验证。确认观察结果符合表格后，再把 repository variable `QA_AUTOFIX_MODE` 设为 `armed`，并只在那时启用 Developer 自动化。本变更没有做第 5 步。

`observe` 只打印判断，不写状态评论，不调用 webhook。`armed` 在调用 webhook 之前先把轮次写入 `pdf-autofix-state`，这条声明的 `dispatch_confirmed` 为 false；webhook 返回 `{"success": true}` 之后才把它改成 true。webhook 被拒绝时会把状态改回去。回滚写入也失败时，未确认的轮次留在评论里，同一 SHA 的下一次 FAIL 只重试这一轮，不再加一轮。状态写不进去时不会调用 webhook。手动 `workflow_dispatch` 即使变量已经是 `armed` 也强制观察，避免手点一次就启动修复。

状态评论只认 `github-actions[bot]`。其他作者的评论即使带有 `pdf-autofix-state` 也不会被采用；只有这几个字、没有合法 JSON 的评论会被跳过，不会挡住更早的 Actions 状态。评论接口按 ID 升序分页，`sort` 和 `direction` 会被忽略，所以桥跟随 `Link` 读完每一页。某一页读取失败时整次运行停止，不会把没读到的状态当成第 0 轮。

完成标记必须来自 `cursor[bot]`，并且正文里的 `CURSOR_AUTOMATION_ID` 只能是 Developer 自动化 `7442224a-c4bd-11f1-ac31-5e2d0494121f`。QA 自动化和其他自动化 ID 不能打开锁。

`PASS` 和 `NOT VERIFIED` 在已有修复进行中时不清除 `in_progress`。当前 Developer 结束前，后一个 FAIL 不能再派发。

## 可信版本如何落地

带 webhook 凭证的工作流只检出仓库默认分支 `feature/cloud-development`，再执行那棵树上的 `automations/qa_loop/bridge.py`。它不检出 PR 头提交，也不检出合并提交。评论分页跟随 GitHub 实际返回的 `Link`（`https://api.github.com/repositories/<id>/issues/<pr>/comments`）。其他主机、其他仓库或未知查询参数会让这次运行失败，不会当成没有状态。

默认分支目前还没有这个脚本，也没有这个工作流文件。可信版本只能通过把本 PR 合并进默认分支落地，不能把 checkout 指到 PR。部署前置条件：

1. 把本 PR 合并进 `feature/cloud-development`。合并之前，工作流如果被手动触发，会因为默认分支上没有脚本而失败退出，不会改去执行 PR 里的副本。
2. 合并后的工作流仍只检出默认分支。后续修桥也要先合并，才能被带凭证的 job 执行。`pull_request_review` 会运行 PR 里的工作流文件，所以只改脚本、不改工作流的 PR 也不会被带凭证的 job 执行。
3. 仓库变量 `QA_AUTOFIX_MODE` 保持 `observe` 或不设置。本变更不把它写成 `armed`，也不启用 Developer。
4. 有写权限的协作者仍只有管理员。在第 1 步完成前，不要把写权限授给其他人。能改工作流文件的人可以换掉 checkout，写权限不能扩大。
5. `observe` 不写状态，也不调用 webhook。只有人工把变量改成 `armed` 之后，才会派发；那一步不在本变更里。

## 验证

本地：

```bash
python -m pytest tests/test_automations/test_qa_autofix_bridge.py -q
```

配置合并到默认分支 `feature/cloud-development` 之后，并且还没有设置变量时，每次审查最多出现一个被跳过的 Action。这不是修复任务。

观察一次历史 PASS，不会启动 Developer：

```bash
gh workflow run qa-autofix-bridge.yml --ref feature/cloud-development \
  -f pr_number=1 -f review_id=5479609804
```

`5479609804` 是 PR #1 上 PASS 审查的 id。日志里的 `action` 应为 `stop_pass`，`dispatch` 为 false。

再观察 FAIL 审查 `5479473585`。在没有历史状态时，日志会显示假如武装将会 `dispatch` 第 1 轮，同时有一句 `observe only`。这只表示判断结果，不表示 Developer 已运行。

武装前的检查：

- Developer 页面仍是 Disabled。
- 仓库变量 `QA_AUTOFIX_MODE` 不存在，或值不是 `armed`。
- Action 日志没有 webhook 的 HTTP 请求。
- PR 上没有新的 `pdf-autofix-state` 评论。
- 没有任何自动化执行 `gh pr merge`。

只有这些都成立，才把变量设为 `armed` 并启用 Developer。启用后用一个专门的试验 PR 验证：第一轮 FAIL 只产生一个 Developer 运行；同一 SHA 的第二次 FAIL 不再产生；PASS 和 NOT VERIFIED 都不产生；第 4 个新失败 SHA 停止。
