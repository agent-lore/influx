"""Agent Zero RFC helper functions for Influx.

Deploy this file into the Agent Zero container as ``/a0/usr/influx_rfc.py``.
It is intentionally stored outside the main Influx package because it runs
inside Agent Zero, not inside Influx itself.
"""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

from __future__ import annotations

from agent import AgentContext, UserMessage
from helpers import extension
from helpers import message_queue as mq
from initialize import initialize_agent


async def enqueue_message(
    *,
    text: str,
    context: str,
    message_id: str = "",
) -> dict[str, str]:
    """Inject a message into a fixed Agent Zero context via RFC.

    This mirrors the core flow used by Agent Zero's ``/api/message_async``
    endpoint without requiring session-cookie auth.
    """
    agent_context = AgentContext.use(context)
    if agent_context is None:
        agent_context = AgentContext(
            config=initialize_agent(),
            id=context,
            set_current=True,
        )

    data = {
        "message": text,
        "attachment_paths": [],
    }
    await extension.call_extensions_async(
        "user_message_ui",
        agent=agent_context.get_agent(),
        data=data,
    )

    message = str(data.get("message", ""))
    attachment_paths = list(data.get("attachment_paths", []))

    mq.log_user_message(agent_context, message, attachment_paths, message_id or None)
    agent_context.communicate(
        UserMessage(
            message=message,
            attachments=attachment_paths,
            id=message_id or "",
        )
    )
    return {
        "message": "Message received.",
        "context": agent_context.id,
    }
