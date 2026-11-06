"""C04：受控模型验证脚本（无需真实 API key）

为什么需要它：
    验收报告指出「补上模型成功响应后，后续断言仍暴露协议差异」，
    而执行方此前把 9 项 503 归为「需 key 才能过」——这个分类没被验证。
    本脚本在 cet_ai._call 边界注入受控响应（明确标注「受控」），
    保留真实路由、真实数据库、真实状态机，用来判断契约失败到底是
    协议差异还是缺 key。

用法：
    # 1. 起真实服务（不需要 AI key）
    scripts/qa/start_cet.sh

    # 2. 跑受控模型下的完整契约
    .venv/bin/python scripts/cet/run_contract_controlled.py

    # 3. 也可以只跑某几项
    .venv/bin/python scripts/cet/run_contract_controlled.py -k review

边界（必须如实说明）：
    - 注入的只是**模型返回内容**，不是模型质量、速度或训练效果
    - 不能据此声称「真实模型已验证」
    - 结论只对「程序链路是否正确」有效
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# 受控响应：内容固定、明确标记，避免被误认为真实模型输出
CONTROLLED_EXPLANATION = "受控解释：这句话在说两人都很疲惫，建议今天先结束。"
CONTROLLED_PHRASES = ["displaying signs of weariness", "call it a day"]


def install_controlled_call() -> None:
    """在 cet_ai._call 边界替换为受控实现。"""
    from webapp.services import cet_ai

    def _controlled(action: str, sentence: str, feedback: str, candidate):
        if action == "explain":
            return (
                {"explanation": CONTROLLED_EXPLANATION, "keyPhrases": CONTROLLED_PHRASES},
                "controlled-model",
            )
        if action == "chunk":
            # 把句子按空格对半切，保证是原句的连续子串
            words = sentence.split()
            if len(words) < 2:
                raise cet_ai.CETAIError("AI_INVALID_OUTPUT", "受控响应：句子过短无法分段。")
            mid = max(1, len(words) // 2)
            return (
                {"chunks": [" ".join(words[:mid]), " ".join(words[mid:])]},
                "controlled-model",
            )
        if action == "transfer":
            return (
                {
                    "question": "受控换句题：这句表达了什么？",
                    "options": ["受控选项甲", "受控选项乙"],
                    "correctAnswer": "A",
                    "explanation": "受控解释",
                },
                "controlled-model",
            )
        raise cet_ai.CETAIError("AI_INVALID_OUTPUT", f"受控响应未覆盖动作：{action}")

    cet_ai._call = _controlled


def main() -> int:
    install_controlled_call()

    # 用 uvicorn 起一个只注入受控模型的进程，再跑契约
    import uvicorn
    from fastapi_server import create_app  # type: ignore

    app = create_app()
    port = int(os.environ.get("CET_CONTROLLED_PORT", "5197"))

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # 等服务就绪
    import time
    import urllib.request

    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    else:
        print("受控服务启动失败")
        return 1

    print(f"受控服务已启动：http://127.0.0.1:{port}")
    print("模型响应来源：cet_ai._call 注入（非真实模型）")
    print()

    env = {**os.environ, "CET_API_BASE": f"http://127.0.0.1:{port}"}
    extra = sys.argv[1:]
    cmd = [sys.executable, "-m", "pytest", "tests/test_cet_contract.py", "-q", *extra]
    result = subprocess.run(cmd, cwd=str(ROOT), env=env)
    server.should_exit = True
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
