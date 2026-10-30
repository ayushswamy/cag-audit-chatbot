"""LLM-assisted reconstruction pass.

After the geometric column split, each para's ordered raw blocks are sent to the
configured LLM to reassemble clean observation_text and reply_text — REORDERING
and JOINING only, never paraphrasing. Both the raw blocks and the reconstructed
text are stored so reconstruction is fully auditable.

Resumable: paras already carrying reconstructed text are skipped.
"""
from __future__ import annotations

from app import llm
from app.config import get_settings
from ingest.models import ParaRecord

SYSTEM = (
    "You reassemble text fragments extracted from ONE column of a two-column "
    "audit-report PDF. The fragments are in reading order but may be split "
    "mid-sentence or mid-word, contain broken drop-caps, or have stray line "
    "breaks. Your job:\n"
    "- Join the fragments into clean, correctly ordered prose.\n"
    "- Fix broken words, drop-caps, and hyphenation.\n"
    "- Reorder ONLY where fragments are obviously out of sequence.\n"
    "STRICT: Do NOT paraphrase, summarize, translate, add, or remove any "
    "information. Preserve every fact, number, date, and monetary value "
    "(Rs., crore, lakh) EXACTLY. Output ONLY the cleaned text, nothing else."
)


def _reconstruct_section(blocks: list[str]) -> str:
    if not blocks:
        return ""
    joined = "\n".join(f"[{i+1}] {b}" for i, b in enumerate(blocks))
    user = ("Fragments (in reading order):\n\n" + joined +
            "\n\nReturn the cleaned, joined text only.")
    return llm.complete(SYSTEM, user)


def reconstruct_record(rec: ParaRecord, force: bool = False) -> bool:
    """Reconstruct obs+reply for one para. Returns True if it ran (skips if
    already reconstructed unless force). Raises on LLM/transport error so the
    caller can decide to abort or continue."""
    if rec.observation_reconstructed is not None and not force:
        return False
    s = get_settings()
    rec.observation_reconstructed = _reconstruct_section(rec.raw_observation_blocks)
    rec.reply_reconstructed = _reconstruct_section(rec.raw_reply_blocks)
    rec.reconstruction_model = s.model_name
    return True
