# BASE04 / BASE05 独立复验

审核提交：6ce35458e49d1752391ced3b69a589d9bddb7713。

仅从该提交 git archive 到 /tmp/cet-base-6ce-boundary/repo，借用已安装 .venv 依赖；所有实例库、配置与日志都指向本目录，未改产品代码、原 PID 或正式库。启动使用真实 scripts/cet/run.sh，停止使用真实 scripts/qa/stop_cet.sh，无 mock。

## BASE04：指定三个场景通过

1. `--check --fresh`、`--check --reset`：均 exit 0。已有 SQLite 哨兵库和同目录 keep.txt 的全文件 SHA256 前后完全一致。证据 results.json / check--fresh.log / check--reset.log。源码 run.sh:455 检查返回在修改阶段之前。
2. 活服务同库 fresh：A 端口 57964，PID 56575，数据库 a/vocab.db。真实启动后写 audit_keep='keep-live'；对同目录同端口执行 `--fresh` 返回 3，记录完整保留，/health 仍 ok。证据 fresh-live-a.log/results.json。源码 run.sh:493 同库服务拒绝，499 进一步检查库持有者。
3. 停 A 不伤 B：B 端口 57965，PID 57847，数据库 b/vocab.db。两者共用同一日志根目录，旧共享 server.pid 已被 B 覆盖。停 A 返回 0、A 端口释放；日志明确跳过不监听 A 端口的 B PID。B /health 仍 ok，PID 未变。证据 stop-a.log/results.json。源码 stop_cet.sh:191、199、206 逐项核验端口、进程与库。最后用真实脚本停止 B，返回 0，B 端口释放；自己的两个服务已清理。

## BASE05：正式配置与真实路由通过

A：ELT_CET_MODE=1，未设置 agent override。/api/cet/mode 返回 cet_listening_agent.enabled=false，真实 GET /workspace/1 的 HTML 不包含队友 JS/CSS 引用。
B：同样 CET 模式并设置 ELT_CET_FEATURE_CET_LISTENING_AGENT=1。/api/cet/mode 返回 enabled=true，真实页面包含两项引用。
源码 backend/webapp/cet_mode.py:136 默认关闭、217 环境变量覆盖；frontend/templates/workspace.html:1859 服务端模板控制是否加载。无浏览器拦截、无测试替身。证据 a-mode.json/b-mode.json/a-workspace.html/b-workspace.html/results.json。

本子任务未做 UI 交互、空题按钮行为或智能体链路验收；这些由主验收负责。以上结论限于要求复验的场景，不代表全系统无缺陷。
