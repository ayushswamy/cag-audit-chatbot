"""One-time migration: re-parse the corpus with the fixed segmenter and merge
into the cache, PRESERVING the (expensive) LLM reconstruction for paras whose
raw blocks did not change. Paras whose blocks changed have their stale
reconstruction cleared so retrieval falls back to the corrected geometric text
(re-reconstruct them later for polish; geometric is correct for retrieval).

After running this, persist + re-embed:
    python -m ingest --store --embed --recreate
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from ingest import excel_index, layout, segmenter
from ingest.pipeline import _cache_path, _load_cache, _save_cache


def main() -> int:
    s = get_settings()
    excel_paras = excel_index.load_excel(s.excel_index)
    groups = excel_index.map_periods_to_pdfs(excel_paras, s.data_dir)
    cache_dir = os.path.join("ingest_out", "cache")

    total_changed = 0
    for g in groups:
        if not g.pdf_path:
            continue
        cache = _cache_path(cache_dir, g.period)
        old = {r.para_key: r for r in (_load_cache(cache) or [])}
        blocks, tables, pages = layout.parse_pdf(g.pdf_path)
        project = g.paras[0].project if g.paras else "Corporate Office"
        new_recs = segmenter.segment(blocks, tables, g.paras, project, g.period,
                                     os.path.basename(g.pdf_path))
        changed = []
        for r in new_recs:
            o = old.get(r.para_key)
            if o and o.raw_observation_blocks == r.raw_observation_blocks \
                    and o.raw_reply_blocks == r.raw_reply_blocks:
                # unchanged -> keep the expensive reconstruction
                r.observation_reconstructed = o.observation_reconstructed
                r.reply_reconstructed = o.reply_reconstructed
                r.reconstruction_model = o.reconstruction_model
            else:
                changed.append(r.para_key)
        _save_cache(cache, new_recs)
        total_changed += len(changed)
        print(f"{g.period}: {len(new_recs)} paras, {len(changed)} changed"
              + (f" -> {changed}" if changed else ""))
    print(f"\nTOTAL changed paras (reconstruction cleared): {total_changed}")
    print("Next: python -m ingest --store --embed --recreate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
