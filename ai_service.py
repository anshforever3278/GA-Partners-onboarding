"""LLM integration for CSA extraction, status reports, and inquiry drafts."""
import os
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv
from emergentintegrations.llm.chat import LlmChat, UserMessage, TextDelta, StreamDone

load_dotenv(Path(__file__).parent / ".env")
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY")
MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"


def _chat(system_message: str) -> LlmChat:
    return LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=str(uuid.uuid4()),
        system_message=system_message,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)


async def _stream_collect(chat: LlmChat, prompt: str) -> str:
    """Use stream_message (default) and collect all tokens into a single string."""
    out_parts = []
    async for ev in chat.stream_message(UserMessage(text=prompt)):
        if isinstance(ev, TextDelta):
            out_parts.append(ev.content)
        elif isinstance(ev, StreamDone):
            break
    return "".join(out_parts).strip()


async def generate_progress_note(client_name: str, phase_name: str, doc_summary: str) -> str:
    chat = _chat(
        "You are an onboarding project manager assistant for G&A Partners, a PEO. "
        "Write concise, professional progress notes (2-3 sentences) for internal records. "
        "Plain text only, no markdown."
    )
    prompt = (
        f"Generate a progress note for client '{client_name}'. "
        f"Phase just completed: {phase_name}. "
        f"Documents/context: {doc_summary}. "
        "Include a forward-looking next-step sentence."
    )
    try:
        return await _stream_collect(chat, prompt)
    except Exception as e:
        return f"{phase_name} phase completed for {client_name}. All required deliverables received and verified. Proceeding to next phase. [auto-fallback: {e}]"


async def generate_status_report(client_name: str, data: dict) -> str:
    chat = _chat(
        "You are a client-facing onboarding analyst at G&A Partners. "
        "Write a polished weekly status report in plain text. "
        "Sections: Overview, Completed Milestones, Pending Items, Next Steps, Estimated Go-Live. "
        "Use short paragraphs and bullet markers with '-'. No markdown headers."
    )
    prompt = (
        f"Compile a weekly status report for client '{client_name}'. "
        f"Data:\n{json.dumps(data, indent=2)}"
    )
    try:
        return await _stream_collect(chat, prompt)
    except Exception as e:
        return (
            f"Weekly Status Report — {client_name}\n\n"
            f"Overall completion: {data.get('completion_pct', 0)}%.\n"
            f"Completed: {', '.join(data.get('completed', []))}.\n"
            f"Pending: {', '.join(data.get('pending', []))}.\n"
            f"Estimated Go-Live: {data.get('go_live', 'TBD')}.\n"
            f"[auto-fallback: {e}]"
        )


async def draft_inquiry_response(client_name: str, inquiry: str, category: str, status_data: dict) -> str:
    chat = _chat(
        "You are a G&A Partners onboarding analyst replying to a client inquiry. "
        "Tone: helpful, concise, professional. Plain text, 3-5 sentences max. "
        "Sign-off as 'G&A Onboarding Team'."
    )
    prompt = (
        f"Client: {client_name}\nCategory: {category}\nInquiry: {inquiry}\n\n"
        f"Live status data:\n{json.dumps(status_data, indent=2)}\n\n"
        "Draft the response."
    )
    try:
        return await _stream_collect(chat, prompt)
    except Exception as e:
        return (
            f"Hi,\n\nThank you for reaching out regarding {category.lower()}. "
            f"Your onboarding is currently in the {status_data.get('phase', 'active')} phase "
            f"with {status_data.get('completion_pct', 0)}% completion. "
            f"We will follow up shortly with more detail.\n\nG&A Onboarding Team\n[auto-fallback: {e}]"
        )


async def csa_cross_validation_narrative(discrepancies: list) -> str:
    if not discrepancies:
        return "All CSA fields align with the ClientSpace handoff. No discrepancies detected — single validation passes."
    chat = _chat(
        "You are a contract compliance analyst at G&A Partners. "
        "Summarize CSA-vs-handoff discrepancies in 2-3 plain text sentences for an OBPM. "
        "Be precise and action-oriented."
    )
    prompt = f"Discrepancies found:\n{json.dumps(discrepancies, indent=2)}\nSummarize."
    try:
        return await _stream_collect(chat, prompt)
    except Exception as e:
        return f"Detected {len(discrepancies)} discrepancy(ies) between CSA and ClientSpace handoff. Review required before proceeding. [auto-fallback: {e}]"
