"""Para endpoints: paginated listing, full structured para, source PDF stream."""
from __future__ import annotations

import io
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app import db
from app.api.deps import require_api_key
from app.api.schemas import (
    ParaDetail,
    ParaListItem,
    ParaListResponse,
    ParaPages,
    ParaTable,
)
from app.config import get_settings

router = APIRouter(prefix="/paras", tags=["paras"],
                   dependencies=[Depends(require_api_key)])


@router.get("", response_model=ParaListResponse, summary="List / search paras")
def list_paras(
    period: str | None = Query(None, description="e.g. 2019-20"),
    para_type: str | None = Query(None, pattern="^(IIA|IIB)$"),
    q: str | None = Query(None, description="free-text match on title/observation"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ParaListResponse:
    where, params = [], {}
    if period:
        where.append("period = :period"); params["period"] = period
    if para_type:
        where.append("para_type = :para_type"); params["para_type"] = para_type
    if q:
        where.append("(title ILIKE :q OR observation_text ILIKE :q)")
        params["q"] = f"%{q}%"
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    engine = db.get_engine()
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT count(*) FROM paras{clause}"),
                             params).scalar_one()
        params2 = dict(params, limit=page_size, offset=(page - 1) * page_size)
        rows = conn.execute(text(
            "SELECT para_key, project, period, para_type, para_no, title, "
            "page_start, page_end, source_pdf, needs_review "
            f"FROM paras{clause} ORDER BY period, para_type, "
            "para_no::text LIMIT :limit OFFSET :offset"), params2).mappings().all()
    items = [ParaListItem(**dict(r)) for r in rows]
    total_pages = (total + page_size - 1) // page_size
    return ParaListResponse(items=items, page=page, page_size=page_size,
                            total=total, total_pages=total_pages)


def _get_row(para_key: str) -> dict:
    with db.connection(db.get_engine()) as conn:
        row = db.get_para(conn, para_key)
    if not row:
        raise HTTPException(status_code=404,
                            detail=("para_not_found",
                                    f"No para with key {para_key!r}."))
    return row


@router.get("/{para_key}/pdf", summary="Stream the source PDF")
def para_pdf(para_key: str,
             pages: bool = Query(False,
                                 description="true = only this para's page range")):
    row = _get_row(para_key)
    s = get_settings()
    path = os.path.join(s.data_dir, row["source_pdf"])
    if not os.path.isfile(path):
        raise HTTPException(status_code=404,
                            detail=("pdf_not_found",
                                    f"Source PDF {row['source_pdf']!r} not on disk."))
    fname = row["source_pdf"]
    if pages and row.get("page_start") and row.get("page_end"):
        data = _slice_pdf(path, row["page_start"], row["page_end"])
        fname = f"{para_key.replace('|', '_')}_p{row['page_start']}-{row['page_end']}.pdf"
        return StreamingResponse(
            io.BytesIO(data), media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{fname}"'})

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk
    return StreamingResponse(_iter(), media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="{fname}"'})


def _slice_pdf(path: str, start: int, end: int) -> bytes:
    import pypdfium2 as pdfium
    src = pdfium.PdfDocument(path)
    dst = pdfium.PdfDocument.new()
    idxs = [p - 1 for p in range(start, end + 1) if 1 <= p <= len(src)]
    dst.import_pages(src, idxs)
    buf = io.BytesIO()
    dst.save(buf)
    return buf.getvalue()


@router.get("/{para_key}", response_model=ParaDetail,
            summary="Full structured para (observation, reply, tables, pages)")
def get_para(para_key: str) -> ParaDetail:
    row = _get_row(para_key)
    tables_md = row.get("tables_markdown") or []
    tables_json = row.get("tables_json") or []
    tables = []
    for i, md in enumerate(tables_md):
        tj = tables_json[i] if i < len(tables_json) else None
        rows = tj.get("rows_json") if isinstance(tj, dict) else (tj if isinstance(tj, list) else None)
        page = tj.get("page") if isinstance(tj, dict) else None
        section = tj.get("section") if isinstance(tj, dict) else None
        tables.append(ParaTable(markdown=md, rows=rows, page=page, section=section))
    return ParaDetail(
        para_key=row["para_key"], project=row["project"], period=row["period"],
        para_type=row["para_type"], para_no=row["para_no"], title=row["title"],
        observation=row.get("observation_reconstructed") or row.get("observation_text") or "",
        reply=row.get("reply_reconstructed") or row.get("reply_text") or "",
        tables=tables,
        pages=ParaPages(start=row.get("page_start"), end=row.get("page_end")),
        source_pdf=row["source_pdf"], column_confidence=row.get("column_confidence"),
        needs_review=row.get("needs_review"))
