"""真实模型的预览/确认验证。仅使用虚构设备，不具备平台发送能力。"""
import argparse
import json
import time

from lingxi_flight_control import OllamaDecider, RuntimeBinding
from lingxi_flight_control.preview import FlightPreviewAgent, ReferencePose, PREVIEW_SYSTEM_PROMPT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="明确允许调用已配置的模型服务")
    parser.add_argument("--text", default="往前飞10米")
    parser.add_argument("--heading", type=float, default=90)
    args = parser.parse_args()
    if not args.live:
        parser.error("只有 --live 才调用模型；离线回归请运行 unittest")
    brain = OllamaDecider(system_prompt=PREVIEW_SYSTEM_PROMPT, timeout=120, max_attempts=1)
    agent = FlightPreviewAgent(RuntimeBinding("fixture-actor", "fixture-project", "fixture-hangar",
                                             "fixture-drone", "fixture-batch", 1), brain)
    card = agent.preview(args.text, command_id="fixture-command", idempotency_key="fixture-command",
                         pose=ReferencePose(113, 23, 50, args.heading, time.time()), site_id="fixture-site")
    result = {"preview": card, "device_requests": 0, "fixture_device": True}
    if card.get("confirmation_id"):
        result["confirmation"] = agent.confirm(card["confirmation_id"], **{
            key: card[key] for key in ("command_id", "idempotency_key", "confirmation_binding", "context_version")})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
