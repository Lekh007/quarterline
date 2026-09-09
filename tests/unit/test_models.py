"""Unit tests for ORM models: decimal losslessness and uniqueness contracts (SPEC §9)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from quarterline.store.models import (
    Base,
    Company,
    FactObservation,
    NormalizedFact,
    decimal_to_text,
    text_to_decimal,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session
    engine.dispose()


def _company(session: Session) -> Company:
    company = Company(ticker="AAPL", cik="0000320193", name="Apple Inc.", country="US")
    session.add(company)
    session.flush()
    return company


def test_decimal_to_text_canonical() -> None:
    assert decimal_to_text(Decimal("1234.56")) == "1234.56"
    assert decimal_to_text(Decimal("1234.560")) == "1234.560"  # digits preserved
    assert decimal_to_text(Decimal("-0.42")) == "-0.42"
    assert decimal_to_text(Decimal("1E+3")) == "1000"  # no scientific notation
    assert decimal_to_text(None) is None


def test_text_to_decimal_exact_roundtrip() -> None:
    assert text_to_decimal("1234.56") == Decimal("1234.56")
    assert text_to_decimal("1234.56").as_tuple() == Decimal("1234.56").as_tuple()
    assert text_to_decimal(None) is None
    with pytest.raises(ValueError):
        text_to_decimal("not-a-number")


def test_decimal_survives_persist_and_readback(session) -> None:
    company = _company(session)
    original = Decimal("1234.56")
    session.add(
        FactObservation(
            company_id=company.id,
            taxonomy="us-gaap",
            original_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            canonical_concept="revenue",
            value_decimal=decimal_to_text(original),
            unit="USD",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            period_kind="quarter",
            reporting_scope="consolidated",
            observation_hash="hash-1",
        )
    )
    session.commit()

    row = session.execute(select(FactObservation)).scalar_one()
    assert row.value_decimal == "1234.56"  # canonical text on disk
    assert text_to_decimal(row.value_decimal) == original  # exact Decimal readback


def test_normalized_facts_rejects_identical_key(session) -> None:
    company = _company(session)
    kwargs = {
        "company_id": company.id,
        "concept": "revenue",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 3, 31),
        "period_kind": "quarter",
        "reporting_scope": "consolidated",
    }
    session.add(NormalizedFact(**kwargs, value_decimal=decimal_to_text(Decimal(1))))
    session.commit()

    session.add(NormalizedFact(**kwargs, value_decimal=decimal_to_text(Decimal(2))))
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        session.commit()


def test_annual_and_quarter_do_not_collide(session) -> None:
    """Annual and Q4 facts may share a period_end; the key includes period_kind."""
    company = _company(session)
    session.add(
        NormalizedFact(
            company_id=company.id,
            concept="revenue",
            period_start=date(2025, 10, 1),
            period_end=date(2025, 12, 31),
            period_kind="quarter",
            reporting_scope="consolidated",
            value_decimal=decimal_to_text(Decimal(100)),
        )
    )
    session.add(
        NormalizedFact(
            company_id=company.id,
            concept="revenue",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            period_kind="annual",
            reporting_scope="consolidated",
            value_decimal=decimal_to_text(Decimal(400)),
        )
    )
    session.commit()  # no IntegrityError: distinct period identity

    kinds = set(session.execute(select(NormalizedFact.period_kind)).scalars())
    assert kinds == {"quarter", "annual"}
