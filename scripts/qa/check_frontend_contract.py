#!/usr/bin/env python3
"""四六级听力黑匣子 · 前端可测性约定检查（C 负责，A 配合）。

为什么有这份东西：
    T03「输入框内按空格不产生标记」、T02「长按不连发」、T14「切页面停旧播放」
    这类是**纯前端行为**，C 走 HTTP 验不了，必须靠浏览器自动化。
    而浏览器自动化需要一个稳定的选择器约定 —— 如果 A 用随机 class 名，
    C 每轮都要跟着改测试，成本高且容易漏。

    所以先把「可测性约定」定下来，A 按约定标 `data-cet-*` 属性，
    C 的测试就能稳定跑，双方都不用反复返工。

约定（最小集，只覆盖真正要自动化的交互点）：
    data-cet-app           四六级页面根容器（存在即说明进入了改造版界面）
    data-cet-material-id   当前材料 ID
    data-cet-player        音频播放器元素
    data-cet-mark-layer    标记时间线图层
    data-cet-mark          单个标记（带 data-cet-mark-sec）
    data-cet-feedback      反馈按钮（带 data-cet-feedback=sound|meaning|heard）
    data-cet-practice-step 当前带练步骤（带 data-cet-step-action）
    data-cet-review-item   复习条目（带 data-cet-review-status）
    data-cet-state         页面主状态（loading|empty|error|ready）

这份检查做什么：
    静态扫模板与静态资源，看约定属性有没有出现、有没有明显用错。
    **它现在会报告「未实现」，因为 A 还没交付 —— 这是准确的，不是误报。**
    A 交付后应当转绿；若有缺失，就是 A 需要补的可测性缺口。

用法：
    .venv/bin/python scripts/qa/check_frontend_contract.py
    .venv/bin/python scripts/qa/check_frontend_contract.py --strict   # 缺失即失败（A 交付后用）

退出码：0 无问题｜1 --strict 下有缺失
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "frontend" / "templates"
STATIC = ROOT / "frontend" / "static"

# 属性 → 用途说明（也是给 A 的清单）
REQUIRED = {
    "data-cet-app": "四六级界面根容器，自动化据此判断是否进入改造版界面",
    "data-cet-material-id": "当前材料 ID，测试据此核对材料隔离",
    "data-cet-player": "音频播放器，测试据此控制播放与读取进度",
    "data-cet-mark-layer": "标记时间线图层，测试据此检查标记是否渲染",
    "data-cet-mark": "单个标记元素",
    "data-cet-mark-sec": "标记对应的原始按键秒数（用于核对未被改写）",
    "data-cet-feedback": "反馈按钮，取值 sound | meaning | heard",
    "data-cet-practice-step": "当前带练步骤容器",
    "data-cet-step-action": "当前步骤动作，取值 replay|slow|chunk|explain|transfer|finish",
    "data-cet-review-item": "复习条目容器",
    "data-cet-review-status": "复习条目状态，取值 pending|current|done|skipped",
    "data-cet-state": "页面主状态，取值 loading|empty|error|ready",
}

# 这些属性必须带枚举值，用错会导致测试断言失效
ENUM_VALUES = {
    "data-cet-feedback": {"sound", "meaning", "heard"},
    "data-cet-step-action": {"replay", "slow", "chunk", "explain", "transfer", "finish"},
    "data-cet-review-status": {"pending", "current", "done", "skipped"},
    "data-cet-state": {"loading", "empty", "error", "ready"},
}


def sources() -> list[Path]:
    files: list[Path] = []
    if TEMPLATES.is_dir():
        files.extend(sorted(TEMPLATES.rglob("*.html")))
    if STATIC.is_dir():
        files.extend(sorted(STATIC.rglob("*.js")))
        files.extend(sorted(STATIC.rglob("*.html")))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="前端可测性约定检查")
    parser.add_argument("--strict", action="store_true", help="有缺失即返回非 0")
    args = parser.parse_args()

    files = sources()
    print("四六级听力黑匣子 · 前端可测性约定检查")
    print(f"扫描 {len(files)} 个文件（frontend/templates + frontend/static）")
    print("-" * 66)

    if not files:
        print("X  找不到前端源码目录")
        return 1

    blobs = {}
    for f in files:
        try:
            blobs[f] = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    joined = "\n".join(blobs.values())

    missing: list[str] = []
    print("\n== 约定属性 ==")
    for attr, purpose in REQUIRED.items():
        if attr in joined:
            where = [str(p.relative_to(ROOT)) for p, b in blobs.items() if attr in b]
            print(f"  OK  {attr}")
            print(f"        {purpose}")
            print(f"        出现在：{where[0]}" + (f" 等 {len(where)} 个文件" if len(where) > 1 else ""))
        else:
            print(f"  --  {attr}（尚未出现）")
            print(f"        用途：{purpose}")
            missing.append(attr)

    # 枚举值检查：只查已经出现的属性
    print("\n== 枚举值合法性 ==")
    enum_problems: list[str] = []
    for attr, allowed in ENUM_VALUES.items():
        if attr not in joined:
            continue
        found = set(re.findall(rf'{re.escape(attr)}\s*=\s*["\']([^"\']+)["\']', joined))
        bad = found - allowed
        if bad:
            enum_problems.append(f"{attr} 出现非法值 {sorted(bad)}（允许 {sorted(allowed)}）")
            print(f"  X   {attr}: 非法值 {sorted(bad)}")
        else:
            print(f"  OK  {attr}: 已出现的值都在允许集合内 {sorted(found) or '（动态赋值）'}")

    # 反向检查：A 别用 data-testid 另起一套（统一用 data-cet-*）
    testid = re.findall(r'data-testid\s*=', joined)
    if testid:
        print(f"\n!   发现 {len(testid)} 处 data-testid。建议统一用 data-cet-* 约定，"
              f"避免两套选择器并存。")

    print("-" * 66)
    if not missing and not enum_problems:
        print("结论：可测性约定已全部满足")
        return 0

    print(f"结论：{len(missing)} 个约定属性尚未实现"
          + (f"，{len(enum_problems)} 个枚举值问题" if enum_problems else ""))
    if missing:
        print("\n给 A 的清单（按此标注即可让自动化稳定跑）：")
        for attr in missing:
            print(f"  {attr:<24} {REQUIRED[attr]}")
    if len(missing) == len(REQUIRED):
        print("\n说明：全部约定属性都还没出现 —— 前端尚未开始改造，属预期，不是缺陷。")
        print("      A 交付后本检查应转绿；届时 --strict 可作门禁。")
    else:
        print("\n说明：部分约定属性已出现，说明前端改造已开始。")
        print("      上面列出的缺失项是需要 A 补的可测性缺口。")
        print("      A 补齐后即可用 --strict 作门禁。")
    return 1 if (args.strict and (missing or enum_problems)) else 0


if __name__ == "__main__":
    sys.exit(main())
