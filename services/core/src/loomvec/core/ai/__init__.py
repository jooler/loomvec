"""AI 供方网关包：gateway.py（OpenAI 兼容 HTTP 通道）+ mock.py（确定性本地供方）。"""

from loomvec.core.ai.gateway import AiGateway, AiJsonError, Usage, extract_json

__all__ = ["AiGateway", "AiJsonError", "Usage", "extract_json"]
