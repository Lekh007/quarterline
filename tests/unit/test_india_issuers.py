"""Issuer registry tests (IND-2): typed records, exactly 2 verified issuers."""

from __future__ import annotations

import pytest

from quarterline.sources.india.issuers import (
    STATUS_PROPOSED,
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

    def test_exactly_two_verified(self):
        verified = get_verified_issuers()
        assert len(verified) == 2
        assert {i.issuer_id for i in verified} == {"IN-INFY", "IN-HINDUNILVR"}

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

    def test_storage_slugs_match_ind1_layout(self):
        by_id = {i.issuer_id: i for i in get_verified_issuers()}
        assert by_id["IN-INFY"].slug == "infosys"
        assert by_id["IN-HINDUNILVR"].slug == "hindustan_unilever"

    def test_proposed_rows_stay_proposed(self):
        tcs = get_issuer("IN-TCS")
        assert tcs.verification_status == STATUS_PROPOSED
        assert tcs.isin == ""  # unverified: empty fields by design, never invented


class TestGuards:
    def test_unknown_issuer_raises(self):
        with pytest.raises(LookupError):
            get_issuer("IN-NOPE")

    def test_require_verified_refuses_proposed(self):
        with pytest.raises(ValueError, match="not verified"):
            require_verified("IN-TCS")

    def test_require_verified_passes_verified(self):
        assert require_verified("IN-INFY").issuer_id == "IN-INFY"
