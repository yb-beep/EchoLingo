# 79e9e56 材料导入/分类独立验收

只读产品代码；真实 create_app()+TestClient，不触发 lifespan；DB/config/output/uploads 全部在 /tmp/cet-base-79e9-import。使用 wave 标准库生成 10 秒有效 WAV，真实 ffprobe 校验，没有 stub 音频探测、上传或数据库。未跑浏览器、未改代码或正式数据。

## I1 P1：改级别会丢类别，改类别会丢级别

- 文件 backend/webapp/services/cet_materials.py:475–495。先一次性移除两个 managed tags，再只恢复本次提供的字段，违反 PATCH 未传字段保留语义。
- 合法导入 cet4/练习 → PATCH {level:cet6} → cet6/category:null。
- 恢复 cet4/练习 → PATCH {category:样例} → level:unspecified/category:样例。
- 首页真实“手动归类”调用 frontend/templates/index.html:8150 就只发 level，因此用户操作会直接触发，并非只有外部调用风险。
- 修复：只替换被显式更新字段的标签；级别、类别和自定义标签互不误删。补单字段和组合 PATCH 回归。

## I2 P1：坏 JSON 条目静默丢弃，导入却报全部成功

- 文件 backend/webapp/services/cet_import.py:197–206，在校验前滤掉所有非 dict 数组元素。
- 上传句子 JSON `[合法句1,"BROKEN",null,合法句2]`，实际 HTTP200，subtitle.errors=[]，sentenceCount=2，并真实落库两句；没有告知丢掉两条。
- 原始序号也被改变，后续报错无法准确指向原条目。
- 修复：保留原数组位置，非对象条目报结构错误，一次性拒绝，不产生部分成功材料。该要求直接来自任务 P2 “不能默默跳过损坏行”。

## I3 P2：并发重复导入生成两份

- 文件 backend/webapp/services/cet_import.py:725–741 与 778–788，查重和建课分离，无原子幂等键或同键互斥。
- 同标题、同真实音频、默认 allow_duplicate=false，两并发均查到无重复后继续；两者都返回 HTTP200/duplicate:false，lessonId 分别4、5，列表 count=2。
- 探针仅在真实 find_duplicate 返回后加两线程屏障，确定性交错；不改变查重结果和数据库行为。
- 修复：把普通导入的去重决策与创建绑定为同一可重复请求保护，显式 allow_duplicate=true 仍可创建新份。

## I4 P2：坏音频返回无解释500

- 文件 backend/webapp/services/cet_import.py:778–781 的 save_media_upload 在其 try 之外；route cet_base.py:218 只捕获 ImportValidationError。
- 上传 bad.mp3，内容 b'not audio at all'，实际500 +纯文本 Internal Server Error，而不是可定位的格式/内容校验错误。
- 上传实现有清理残片，此项未发现假卡片；主要缺陷为错误映射和用户恢复提示。
- 应捕获 MediaUploadError/TooLarge 等明确映射422/413，不吞真正程序异常。

## 已通过边界

- 合法真实WAV+JSON字幕：200、ready、media hash正确、句子完整落库。
- 仅音频：200、unavailable、无伪时间轴。
- 归档后默认列表排除，恢复后重新出现，现有类别保留（归档本身未触发managed tags变更）。
- NaN 时间轴最终被422拒绝且没有假卡片，但错误落在SQL约束而非预检；属于可并入验证改进的次要项，本报告不单独扩大返修范围。

证据：probe.py、results.json、output.txt，同目录。results.json 包含每个真实响应。
