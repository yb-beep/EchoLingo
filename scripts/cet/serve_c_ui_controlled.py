#!/usr/bin/env python3
"""C 板块验证专用：带受控模型的隔离服务启动器。

为什么需要它：
    F05「有候选」路径必须经过 `cet_ai.generate(action="transfer")`。
    没有 AI key 时后端直接 503，前端就永远只能走到「无候选」分支，
    「播放候选真实音频 → 作答 → 落库」这条主链就验证不到。

    本脚本在 `cet_ai._call` 边界注入**受控响应**（明确标注非真实模型），
    其余全部是真实路由、真实数据库、真实状态机 —— 与
    `scripts/cet/run_contract_controlled.py` 的做法一致。

边界（必须如实说明）：
    - 注入的只是「模型返回内容」，不是模型质量、速度或训练效果
    - 不能据此声称「真实模型已验证」
    - 结论只对「前端交互链路是否正确」有效

用法：
    cd /tmp/wt-c-ui
    ELT_USER_DB=/tmp/wt-c-ui/.cet-qa/data/vocab.db \
    ELT_CONFIG_DIR=/tmp/wt-c-ui/.cet-qa/data \
    ELT_PORT=5188 \
      .venv/bin/python scripts/cet/serve_c_ui_controlled.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def install_controlled_call() -> None:
    """在 cet_ai._call 边界替换为受控实现（只影响 explain/chunk/transfer）。"""
    from webapp.services import cet_ai

    def _controlled(action: str, sentence: str, feedback: str, candidate):
        if action == "explain":
            return (
                {"explanation": "受控解释：这句在说两人都很疲惫，建议先结束今天的讨论。",
                 "keyPhrases": []},
                "controlled-model",
            )
        if action == "chunk":
            words = sentence.split()
            if len(words) < 2:
                raise cet_ai.CETAIError("AI_INVALID_OUTPUT", "受控响应：句子过短无法分段。")
            mid = max(1, len(words) // 2)
            return ({"chunks": [" ".join(words[:mid]), " ".join(words[mid:])]},
                    "controlled-model")
        if action == "transfer":
            # 受控换句题：题干/选项/答案固定，用于验证前端渲染与提交链路
            return (
                {
                    "question": "受控换句题：新片段里说话人建议怎么做？",
                    "options": ["今天先结束，改天再继续", "继续再讲两个小时", "换一个话题重新开始"],
                    "correctAnswer": "A",
                    "explanation": "受控解释：新片段表达了「今天就到这里」的意思。",
                },
                "controlled-model",
            )
        raise cet_ai.CETAIError("AI_INVALID_OUTPUT", f"受控响应未覆盖动作：{action}")

    cet_ai._call = _controlled


def main() -> int:
    install_controlled_call()
    import uvicorn
    from fastapi_server import create_app  # type: ignore

    app = create_app()
    port = int(os.environ.get("ELT_PORT", "5188"))
    host = os.environ.get("ELT_HOST", "127.0.0.1")
    print(f"C 板块受控服务：http://{host}:{port}")
    print("模型响应来源：cet_ai._call 注入（非真实模型，仅用于验证前端链路）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
