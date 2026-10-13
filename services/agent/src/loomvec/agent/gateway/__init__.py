"""acp-gateway 适配层（P2）：services/agent 通过本包与网关交互。

- `client.py`   薄 httpx 异步客户端，只覆盖用到的端点；
- `translate.py` 网关事件 → loomvec SSE 帧（纯状态机，可独立单测）；
- `history.py`  网关 messages 视图 → `AgentMessageOut` 形状；
- `service.py`  workspace / session 懒创建、MCP token 续签（落库状态机）；
- `orchestrator.py` 提问流编排；
- `openapi.snapshot.json` 网关契约快照（契约测试比对用）。
"""
