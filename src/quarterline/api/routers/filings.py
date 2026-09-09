"""Filing viewer and evidence resolution (SPEC §19, §2.3.7).

The filing viewer renders the CLEANED extracted text stored in ``sections``
rows -- never raw third-party filing HTML (SPEC §2.3.7: third-party filing
HTML is served only through a safe viewer). Jinja2 autoescaping renders every
string inert; no ``|safe`` filter is used anywhere.

``GET /evidence/{evidence_id}`` resolves contract C8 ids: ``ev-`` + first 12
hex chars of ``sha1("{document_id}|{strategy}|{start}|{end}|{text_hash}")``
computed over ``chunks`` rows. The chunks table is built by the retrieval
wave (F3); until it is populated the route returns an explicit
"evidence not found (index not built)" 404. Finalized at integration.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from quarterline.api.routers.companies import render_error
from quarterline.store.db import session_scope
from quarterline.store.models import Chunk
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.documents import DocumentsRepo

API_DIR = Path(__file__).resolve().parents[1]

router = APIRouter()
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

#: Contract C8 evidence-id shape.
EVIDENCE_ID_PATTERN = re.compile(r"^ev-[0-9a-f]{12}$")


def evidence_id_for(chunk: Chunk) -> str:
    """C8 id of a chunk row (used for lookup and by tests)."""
    digest = hashlib.sha1(
        f"{chunk.document_id}|{chunk.strategy}|{chunk.start_offset}|{chunk.end_offset}"
        f"|{chunk.text_hash}".encode()
    ).hexdigest()
    return f"ev-{digest[:12]}"


def _resolve_chunk(session, evidence_id: str) -> Chunk | None:
    """First chunk whose C8 identity matches ``evidence_id`` (chunks table)."""
    rows = session.scalars(
        select(Chunk).where(
            Chunk.text_hash.is_not(None),
            Chunk.start_offset.is_not(None),
            Chunk.end_offset.is_not(None),
        )
    )
    for chunk in rows:
        if evidence_id_for(chunk) == evidence_id:
            return chunk
    return None


@router.get("/filings/{document_id}")
async def filing_page(request: Request, document_id: int):
    with session_scope() as session:
        repo = DocumentsRepo(session)
        document = repo.get_document(document_id)
        if document is None:
            return render_error(request, 404, f"No filing with document_id {document_id}.")
        company = None
        if document.company_id is not None:
            company = CompaniesRepo(session).get(document.company_id)
        sections = repo.list_sections(document_id)
        context = {
            "document": {
                "id": document.id,
                "form": document.form,
                "document_kind": document.document_kind,
                "accession": document.accession,
                "filed_at": document.filed_at.isoformat() if document.filed_at else None,
                "event_date": document.event_date.isoformat() if document.event_date else None,
                "period_end": document.period_end.isoformat() if document.period_end else None,
                "source_url": document.source_url,
                "extraction_status": document.extraction_status,
                "extraction_version": document.extraction_version,
            },
            "company": {"ticker": company.ticker, "name": company.name} if company else None,
            "sections": [
                {
                    "heading": section.heading,
                    "section_type": section.section_type,
                    "text": section.text or "",
                }
                for section in sections
            ],
        }
    return templates.TemplateResponse(request=request, name="filing.html", context=context)


@router.get("/evidence/{evidence_id}")
async def evidence_page(request: Request, evidence_id: str):
    if not EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
        return render_error(request, 404, f"Malformed evidence id {evidence_id!r}.")
    with session_scope() as session:
        chunk = _resolve_chunk(session, evidence_id)
        if chunk is None:
            # Explicit degraded mode (SPEC §25): the retrieval index is not built.
            return render_error(request, 404, "evidence not found (index not built)")
        repo = DocumentsRepo(session)
        document = repo.get_document(chunk.document_id)
        context = {
            "evidence_id": evidence_id,
            "text": chunk.text or "",
            "strategy": chunk.strategy,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
            "document": document,
            "filing_url": f"/filings/{chunk.document_id}" if document else None,
        }
    return templates.TemplateResponse(
        request=request, name="partials/evidence.html", context=context
    )
