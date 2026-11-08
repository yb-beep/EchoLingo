# EchoLingo · 四六级听力专用底座

> 本文件是本版本（四六级专用化）的入口说明。
> 上游通用版说明见 [README.md](README.md) / [README.zh-CN.md](README.zh-CN.md)。
> 上游项目：[shine11224/EchoLingo](https://github.com/shine11224/EchoLingo)，
> 许可：[PolyForm Noncommercial 1.0.0](LICENSE)（保留原许可，未改动）。

---

## 0. 分支与接续（先看这里）

| 项 | 值 |
| --- | --- |
| 交付分支 | **`cet-base-delivery`** |
| 交付 SHA | `8bbd92025f1386a43a4ef02e164e6166444a7f87` |
| 底座验收 | ✅ 已通过独立验收（[6ce3545 复验](docs/acceptance/2026-11-06-6ce3545-底座独立复验.md)） |

```bash
git clone https://github.com/yb-beep/EchoLingo.git
cd EchoLingo
git checkout cet-base-delivery
```

**接续卡点 / AI 带练 / 换句 / 智能复习模块的队友，请先读
[队友接续指南](docs/cet-base/队友接续指南.md)。**

一句话：**你的四个文件（`cet-listening.js` / `.css` / `cet_service.py` / `cet_ai.py`）
相对 `70f8187` 一行未改，模块只是被「关掉」了不是被改坏。**
打开 `cet_listening_agent` 开关就能接续，模板不用改：

```bash
ELT_CET_FEATURE_CET_LISTENING_AGENT=1 ...   # 或把 cet_mode.py 里 kept 改 True
```

---

## 1. 本版本用途

把 EchoLingo 收束成**四六级听力学习底座**。只保留一条主线：

```
选四级/六级 → 看已有材料或导入本地音频 → 听力工作区
→ 播放 / 拖动 / 倍速 / 逐句回听 / 循环 → 看原文、查词、收藏
→ 返回材料库 → 再次进入恢复学习位置
```

**这条链路在无 AI key、未接队友模块时独立可用。**

从 CET 模式**退出**的入口（任务书 §3.3）：YouTube / Bilibili / 百度网盘 / 网页文章等来源导入、
独立文章与 PDF 阅读课程、文本生成朗读、泛英语视频学习、记忆故事、情境造句、雅思复用、
自由复述评分、泛用 AI 聊天、内容大纲生成、积分/登录/个人档案。

> 底层解析器、共享组件、历史数据与许可署名**保留**，不物理删除；
> 收束通过集中功能配置（`backend/webapp/cet_mode.py`）控制路由与初始化，
> 不用 CSS 遮挡。`ELT_CET_MODE=0` 可退回通用模式。

**明确不做**：空格卡点定位、AI 带练、换句检验、智能复习（属队友模块）；
作文批改、翻译训练、口语评分；多平台安装包；公网部署。

---

## 2. 快速开始

```bash
# 1) 首次准备（只做一次）
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # AI key 可留空

# 2) 启动
scripts/cet/run.sh            # 默认 5173
scripts/cet/run.sh --port 5184   # 端口被占时换一个

# 3) 打开
#    脚本会打印真实的材料链接（/workspace/<真实ID>，不硬编码）
```

完整说明（含换端口、空库启动、指定数据目录、手动启动、排错）见
**[docs/cet-base/使用与启动.md](docs/cet-base/使用与启动.md)**。

---

## 3. 导入格式

| 类型 | 支持 | 说明 |
| --- | --- | --- |
| 音频 | `.mp3` `.m4a` `.wav` `.aac` `.flac` | 播放器实际可用的常见格式；未测格式不承诺 |
| 字幕（带时间轴） | `.srt`（推荐）`.vtt` `.json` | 逐句定位；仓库句子 JSON 也兼容 |
| 纯文本 | `.txt` | **只作原文**，不伪造逐句时间戳 |

SRT 示例：

```srt
1
00:00:00,010 --> 00:00:03,490
It's getting late and we're both displaying signs of weariness.

2
00:00:03,550 --> 00:00:07,010
I think we should call it a day and meet again tomorrow morning.
```

校验规则（错误会指出**具体条目与行号**，不会静默跳过损坏行还报成功）：
起止非负、`end > start`、顺序递增、不超音频时长、文本非空。

**仅音频**也能导入并整段播放；此时明确显示「暂无逐句定位」，逐句操作禁用，
**不会**因为没有 key 就强制排队转写。

---

## 4. 基础功能

| 功能 | 状态 | 说明 |
| --- | --- | --- |
| 材料库 | ✅ | 四级/六级分类、标题搜索、最近学习、继续学习 |
| 待分类（旧材料） | ✅ | 级别无法可靠判断时列「待分类」，可手动归类，不静默丢弃 |
| 本地导入 | ✅ | 音频 + 可选 SRT/JSON + 可选纯文本；重复提交按 SHA-256 + 标题去重 |
| 播放器 | ✅ | 播放/暂停/拖动/倍速/逐句回听/循环，音频与原文联动 |
| 原文 | ✅ | 显示、隐藏、完整听力稿；无译文时明确说明，不留空壳按钮 |
| 查词 | ✅ | 离线词典优先，无 key 可用 |
| CET 词表高亮 | ✅ | CET4/CET6 词表高亮 |
| 词句收藏 | ✅ | 保留材料身份、原句、音频定位；可从收藏回到原材料/原音频 |
| 学习进度 | ✅ | 按材料恢复位置；完成态来自真实行为，不编造掌握度 |
| 设置 | ✅ | 默认级别、学习显示、实际可用词表、现有模型设置入口（不改用户配置） |
| AI 词句解释 | ⚠️ 可选 | 次级操作，按需触发；**无 key 不阻塞基础学习** |

---

## 5. 队友模块接入状态

| 项 | 状态 |
| --- | --- |
| `frontend/static/cet-listening.js` / `.css` | **保留原样，未改动**（队友独占） |
| `backend/webapp/services/cet_service.py` / `cet_ai.py` | **保留原样，未改动**（队友独占） |
| `backend/webapp/fastapi_routes/cet.py` | 保留；底座只加了 `GET /api/cet/mode` |
| 空格卡点 → AI 带练 → 换句检验 → 智能复习 | **未在本轮验证**，属队友模块 |
| 底座独立运行时的表现 | 不显示无法使用的「开始 AI 带练」等假入口 |

接入契约（材料列表/详情、音频地址、句子索引、级别与版本、播放器获取方式、
seek/play/pause/倍速、挂载位置、快捷键归属、关键事件）见
`docs/cet-base/队友接入说明.md`（由播放与收藏板块交付）。

> **本版本只交付底座**。队友链路是否可用，由队友模块自己的验收决定，
> 不因底座验收通过而宣布完整参赛项目完成。

---

## 6. 实际测试平台

| 项 | 值 |
| --- | --- |
| 操作系统 | macOS 27.0.1（Apple Silicon） |
| Python | 3.11.16 |
| FFmpeg / ffprobe | 9.0.2 |
| FastAPI / Starlette | 0.162.0 / 1.10.0 |
| 浏览器验证 | Chromium（Playwright），桌面 1440×900 / 1280×800 / 768×1024 / 375×780 |

**未测平台**：Windows、Linux、移动端。
本版本交付**本机可运行源码与演示入口**，不提供多平台安装包，不做公网部署。

---

## 7. 自验

```bash
# 逐项自验 T01–T15 可自动化部分（自起服务、自建空库、跑完自停）
.venv/bin/python scripts/cet/verify_base.py --port 5184

# 用户视角完整链路
.venv/bin/python scripts/cet/lead_trial.py --base http://127.0.0.1:5184

# 全量回归
ELT_CONFIG_DIR=/tmp/verify/config \
  .venv/bin/python -m pytest -q --ignore=tests/test_cet_listening_ui.py
```

> ⚠️ **跑 pytest 时不要设 `ELT_USER_DB`**。该环境变量优先级高于测试里的
> `monkeypatch.setattr(db, "DB_PATH", ...)`，会让所有测试写进同一个库互相污染，
> 得出大量假失败。原因与证据见
> [docs/cet-base/使用与启动.md §6](docs/cet-base/使用与启动.md#6-️-验收方注意elt_user_db-与-pytest-的冲突)。

逐项结果、证据与已知限制见 **[docs/cet-base/自验报告.md](docs/cet-base/自验报告.md)**。

---

## 8. 文档索引

| 文档 | 内容 |
| --- | --- |
| [使用与启动](docs/cet-base/使用与启动.md) | 启动、导入、使用、排错、`ELT_USER_DB` 陷阱 |
| [自验报告](docs/cet-base/自验报告.md) | T01–T15 逐项证据、测试分类、剩余问题 |
| [基线失败清单](docs/cet-base/evidence/baseline-failures.md) | 哪些失败是施工前就存在的，以及「41 failed」为何不可信 |
| [功能收束清单](docs/cet-base/功能收束清单.md) | 实际入口、保留/改造/退出、代码位置与理由 |
| [队友接入说明](docs/cet-base/队友接入说明.md) | 接口、播放器、挂载、快捷键与责任边界 |

---

## 9. 许可与归属

本项目基于 [shine11224/EchoLingo](https://github.com/shine11224/EchoLingo) 修改，
**保留上游 `LICENSE`（PolyForm Noncommercial 1.0.0）**、`THIRD_PARTY_LICENSES.md`
与 `licenses/` 目录，未删除任何署名信息。第三方组件仍遵循各自许可。
新增的四六级适配代码同样遵循原项目许可：个人、教育与非营利用途免费，
商用需另行取得作者授权。
