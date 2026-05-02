from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bot import is_commitment, is_stop, looks_like_auto_reply


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)


def respond(state: ConversationState, merchant_message: str) -> dict[str, Any]:
    state.turns.append({"from": "merchant", "body": merchant_message})
    if is_stop(merchant_message):
        return {"action": "end", "rationale": "User opted out or was hostile."}
    if looks_like_auto_reply(merchant_message, state.merchant_id):
        return {"action": "end", "rationale": "Detected a canned WhatsApp auto-reply."}
    if is_commitment(merchant_message):
        return {
            "action": "send",
            "body": "Done, moving to action. I will prepare the draft/update now and ask only for final confirmation.",
            "cta": "open_ended",
            "rationale": "Commitment detected; route to action immediately.",
        }
    return {
        "action": "send",
        "body": "Got it. I will keep it to one useful draft and one clear next step. Reply YES if I should proceed.",
        "cta": "YES/STOP",
        "rationale": "Acknowledged and advanced without over-qualifying.",
    }

