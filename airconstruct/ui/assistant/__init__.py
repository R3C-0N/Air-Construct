"""Claude control of the open window: the bridge server, its commands and the top-bar pill.

Owned by: workstream "assistant" (see airconstruct/assistant/__init__.py and
docs/claude-mcp.md).
"""

from .bridge import AssistantBridge
from .indicator import AssistantIndicator

__all__ = ["AssistantBridge", "AssistantIndicator"]
