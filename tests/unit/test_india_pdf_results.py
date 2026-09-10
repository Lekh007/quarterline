"""PDF header/scale detection tests (IND-2) — synthetic strings only.

Full table extraction is a later milestone; here only the declared-scale and
scope headers are detected, with honest ambiguity -> review_required.
"""

from __future__ import annotations

from quarterline.sources.india.pdf_results import detect_headers
from quarterline.sources.india.units import CRORE, LAKH


class TestScaleHeaders:
    def test_hul_style(self):
        detection = detect_headers("(Rs in Crores)")
        assert detection.scale == CRORE
        assert detection.scale_label == "Crores"

    def test_infys_style(self):
        detection = detect_headers("In ₹ crore")
        assert detection.scale == CRORE

    def test_lakhs(self):
        detection = detect_headers("Rs in Lakhs")
        assert detection.scale == LAKH

    def test_missing_scale(self):
        detection = detect_headers("Statement of Profit and Loss")
        assert detection.scale is None
        assert detection.review_required
        assert "no scale header" in (detection.reason or "")


class TestScopeHeaders:
    def test_consolidated_only(self):
        detection = detect_headers("C. CONSOLIDATED statement of profit and loss")
        assert detection.scope == "consolidated"

    def test_standalone_only(self):
        detection = detect_headers("A. STANDALONE statement of profit and loss")
        assert detection.scope == "standalone"

    def test_both_scopes_is_review_required(self):
        # Infosys' Reg-33 PDF bundles both scopes in one document (audit §6.4).
        text = "A. Standalone ... C. Consolidated"
        detection = detect_headers(text)
        assert detection.scope is None
        assert detection.review_required
        assert "both" in (detection.reason or "")

    def test_neither_scope_is_review_required(self):
        detection = detect_headers("Rs in Crores")
        assert detection.scope is None
        assert detection.review_required


class TestCombined:
    def test_clean_header(self):
        detection = detect_headers("Consolidated\n(₹ in Crores)")
        assert detection.ok
        assert detection.scale == CRORE
        assert detection.scope == "consolidated"
        assert detection.reason is None

    def test_conflicting_scale_headers(self):
        detection = detect_headers("Consolidated\nRs in Crores\nnote: figures in Lakhs")
        assert detection.scale is None
        assert detection.review_required
        assert "conflicting" in (detection.reason or "")

    def test_scale_only_header_still_flags_scope(self):
        detection = detect_headers("(Rs in Crores)")
        assert detection.scale == CRORE
        assert detection.review_required  # scope unknown -> review, not a guess
