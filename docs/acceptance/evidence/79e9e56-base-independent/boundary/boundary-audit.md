# 79e9e56 启动、隔离与接入边界独立审查

审查对象：`79e9e56a63453e5318b1fa7b172304e6c8314e4b`。只读产品代码，探针与临时测试资源位于 `/tmp/cet-base-79e9-boundary`。未改产品文件、原数据库或原 PID 文件，未操作浏览器。所有自建 HTTP 测试进程已停止。

## P1：停止脚本没有绑定端口/实例，会停止另一个实例

代码：`scripts/qa/start_cet.sh:24,130–139`；`scripts/qa/stop_cet.sh:18,39–46,63–66`。

所有数据目录/端口共用仓库下 `.cet-qa/logs/server.pid`。停止函数虽然接收 port 参数，但不校验 PID 的命令、监听端口或数据目录，直接 kill。更换端口启动第二实例会覆盖第一实例 PID 文件；随后 `PORT=A stop_cet.sh` 停的是 B。

安全动态复现：复制未修改的 stop 脚本到临时项目结构；自建两个仅测试用 HTTP 进程 A/B（不同随机空闲端口）；PID 文件记录 B；用 PORT=A 执行停止脚本。结果 A 仍运行、B 被停止；脚本还显示 A 端口占用，但最终返回 0。不是推测的 PID 复用风险，是当前多端口使用方式即可触发。

期望：按实例（数据目录＋端口）保存日志/PID，停止前核验身份；端口仍未停止必须非零返回；不能默认顺便停另一个 Mock 实例。无需扩大成进程管理系统。

## P1：--check 与 --fresh 组合仍会删库；运行中的同库也没有保护

代码：`scripts/cet/run.sh:113–115,133–136,201–202`。

端口同库校验通过后称“复用，不重启”，随后 --fresh 无条件删除 DB/WAL/SHM，没有检查服务是否仍在持有该库。--check 只在删库之后退出。

安全动态复现：临时目录内新建 SQLite audit_sentinel 表，执行实际 `run.sh --check --fresh --data-dir <该临时目录> --port 52961`。返回0、“检查通过”，但 vocab.db 已被删除。使用与启动文档把 --check 定义为“只检查不启动”，此路径不应修改现有数据。

活服务删库的路径本次仅做代码证据，未对主代理服务或用户服务做删除实验。可在主代理另外自建的可丢弃库验证；不应尝试正式库。

期望：--check 先行保证纯检查；fresh 默认使用新隔离目录，或在明确重置现有目录时检查实例状态；活动数据库不删除。当前隐藏 --reset 参数在解析阶段 rm -rf DATA_DIR 也应并入同样约束，不能在检查前执行。

证据：`launch_probe.py`、`launch-results.json`。

## P1：队友模块“关闭”只有测试网络拦截，独立底座没有实际开关

代码：`frontend/templates/workspace.html:1847–1849` 无条件引入卡点脚本；`frontend/static/cet-listening.js:1743–1766` 找到材料就 mount/renderQuestions，1799 无条件注册键盘监听。

`backend/webapp/cet_mode.py` 集中功能集合没有 teammate/agent 开关。`cet-player.js:407` 的 cetListeningEnabled() 只读 DOM 状态，不控制启用。

`docs/cet-base/队友接入说明.md:368` 的关闭是 Playwright route.abort() 阻断 .js/.css，没有可交付的配置方式。用户导入普通无题音频也会被识别为 CET 材料，主代理已在真实页面观察到空题交卷、卡点、复习工具栏。

期望：本任务底座默认关闭队友训练入口，集中开关在模板/初始化层决定是否加载；接入时明确启用并做冒烟。保护队友源码，不重复开发其训练算法。即使保持自动启用，至少需要真实能力判断，不能给无题材料渲染空交卷假入口。

## P2：退出的网盘模块仍会自动探测

代码：`frontend/templates/index.html:8511` 加开关的一处调用并未涵盖 `8855` 无条件 `initBaiduPanCard()`；`8620` 函数入口即 fetch `/api/v2/lessons/baidu-pan/capability`。这段 JS 没有模板条件包围。

`tests/test_cet_base_home.py:221` 只断言受控调用那一行存在，没排除第二次调用。主代理可以通过首页实际访问日志进一步确认。影响是无关后台探测，通常非核心链路阻断；可与入口收束一起小修。

## 正向确认与边界

- CET 默认模式为集中配置，导航、功能项与词表有后端统一来源；ELT_CET_MODE=0 是兼容开关，本轮任务书本来允许集中配置，不需因它存在另行审批。
- 底座播放器没有注册 keydown，ownsSpaceKey=false，与队友快捷键边界的代码声明一致。
- 对比 `70f8187..79e9e56`，cet-listening.js/.css、cet_service.py、cet_ai.py 无差异；这才是“底座阶段零改动”的合适口径。与早先6584db2相比有改动，因为中间还有旧链路返修，不能不写基线笼统称52提交全无改动。
- `_resume_interrupted_translations` 已优先取 ELT_USER_DB 并设置 DB context，再 spawn_with_db_context，修复了此前后台固定 DB_PATH 的问题；本审查未实际跑后台翻译。
- 本审查不把原库“总行数相同”等同内容完全未变，行内值可以在行数不变时被更新；独立隔离验收应按逻辑内容/主键值对比，不照抄报告中的只看总行数判据。
