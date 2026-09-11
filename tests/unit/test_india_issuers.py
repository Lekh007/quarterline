"""Issuer registry tests (IND-2; all 10 issuers verified since IND-6).

The IND-1 registry held 2 verified + 8 proposed rows. Group A/B acquisition
(IND-6a/IND-6b) verified the remaining eight against NSE/BSE/instance evidence
(docs/india_source_audit_addendum_group*.md §1), and IND-6c flipped the CSV.
The not-verified guard remains contract and is exercised against a synthetic
all-proposed registry file.
"""

from __future__ import annotations

import pytest
from india_test_helpers import ALL_ISSUER_IDS, write_all_proposed_watchlist

from quarterline.sources.india import issuers as issuers_module
from quarterline.sources.india.issuers import (
    STATUS_VERIFIED,
    get_issuer,
    get_verified_issuers,
    load_issuers,
    require_verified,
)


class TestRegistry:
    def test_registry_loads_ten_rows(self):
        issuers = load_issuers()
        assert len(issuers) == 10

    def test_all_ten_verified(self):
        verified = get_verified_issuers()
        assert len(verified) == 10
        assert {i.issuer_id for i in verified} == set(ALL_ISSUER_IDS)
        assert all(i.verification_status == STATUS_VERIFIED for i in verified)

    def test_verified_identifier_records(self):
        by_id = {i.issuer_id: i for i in get_verified_issuers()}
        infy = by_id["IN-INFY"]
        assert infy.isin == "INE009A01021"
        assert infy.ticker_nse == "INFY"
        assert infy.bse_code == "500209"
        assert infy.verification_status == STATUS_VERIFIED
        assert infy.name == "Infosys Limited"
        hul = by_id["IN-HINDUNILVR"]
        assert hul.isin == "INE030A01027"
        assert hul.ticker_nse == "HINDUNILVR"
        assert hul.bse_code == "500696"

    def test_group_ab_identifiers_match_the_addenda(self):
        """The 8 IND-6 rows carry the verified ISIN/NSE/BSE triples from the
        group manifests (docs/india_source_audit_addendum_group*.md §1)."""
        expected = {
            "IN-TCS": ("INE467B01029", "TCS", "532540"),
            "IN-HCLTECH": ("INE860A01027", "HCLTECH", "532281"),
            "IN-ITC": ("INE154A01025", "ITC", "500875"),
            "IN-ASIANPAINT": ("INE021A01026", "ASIANPAINT", "500820"),
            "IN-MARUTI": ("INE585B01010", "MARUTI", "532500"),
            "IN-ULTRACEMCO": ("INE481G01011", "ULTRACEMCO", "532538"),
            "IN-SUNPHARMA": ("INE044A01036", "SUNPHARMA", "524715"),
            "IN-LT": ("INE018A01030", "LT", "500510"),
        }
        by_id = {i.issuer_id: i for i in get_verified_issuers()}
        for issuer_id, (isin, symbol, bse) in expected.items():
            issuer = by_id[issuer_id]
            assert (issuer.isin, issuer.ticker_nse, issuer.bse_code) == (isin, symbol, bse)
            assert issuer.verified_source  # verification evidence recorded

    def test_storage_slugs_match_ind1_layout(self):
        by_id = {i.issuer_id: i for i in get_verified_issuers()}
        assert by_id["IN-INFY"].slug == "infosys"
        assert by_id["IN-HINDUNILVR"].slug == "hindustan_unilever"
        # IND-6 cache layout slugs (group A/B acquisition directories)
        assert by_id["IN-MARUTI"].slug == "maruti_suzuki"
        assert by_id["IN-ULTRACEMCO"].slug == "ultratech_cement"
        assert by_id["IN-SUNPHARMA"].slug == "sun_pharmaceutical"
        assert by_id["IN-LT"].slug == "larsen_toubro"


class TestGuards:
    def test_unknown_issuer_raises(self):
        with pytest.raises(LookupError):
            get_issuer("IN-NOPE")

    def test_require_verified_refuses_proposed(self, tmp_path, monkeypatch):
        """The verified-only guard, exercised against a synthetic registry with
        proposed rows (the live registry verified all 10 rows in IND-6)."""
        proposed_csv = write_all_proposed_watchlist(tmp_path)
        monkeypatch.setattr(issuers_module, "DEFAULT_WATCHLIST_PATH", proposed_csv)
        with pytest.raises(ValueError, match="not verified"):
            require_verified("IN-TCS")

    def test_require_verified_passes_verified(self):
        assert require_verified("IN-INFY").issuer_id == "IN-INFY"
        assert require_verified("IN-TCS").issuer_id == "IN-TCS"
