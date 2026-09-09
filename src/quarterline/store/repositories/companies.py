"""Company repository: watchlist upserts and lookups (contract C5, SPEC 9.1)."""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import select

from quarterline.store.models import Company
from quarterline.store.repositories.base import BaseRepo

WATCHLIST_COLUMNS = ("ticker", "cik", "name", "sector", "country")


class CompaniesRepo(BaseRepo):
    # -- lookups ------------------------------------------------------------

    def get_by_ticker(self, ticker: str) -> Company | None:
        return self.session.scalar(select(Company).where(Company.ticker == ticker.strip().upper()))

    def get_by_cik(self, cik: str) -> Company | None:
        return self.session.scalar(select(Company).where(Company.cik == cik))

    def get(self, company_id: int) -> Company | None:
        return self.session.get(Company, company_id)

    def all(self) -> list[Company]:
        return list(self.session.scalars(select(Company).order_by(Company.ticker)).all())

    # -- upserts --------------------------------------------------------------

    def upsert_company(
        self,
        ticker: str,
        cik: str,
        name: str | None = None,
        sector: str | None = None,
        country: str | None = None,
    ) -> Company:
        """Insert or update a company, matching on ticker *or* CIK.

        CIK is the issuer identity; a ticker change must not create a second
        company for the same issuer (SPEC 9.1).
        """
        ticker = ticker.strip().upper()
        cik = cik.strip()
        company = (
            self.get_by_ticker(ticker) or self.get_by_cik(cik) or Company(ticker=ticker, cik=cik)
        )
        company.ticker = ticker
        company.cik = cik
        for field, value in (("name", name), ("sector", sector), ("country", country)):
            if value:
                setattr(company, field, value.strip() if isinstance(value, str) else value)
        self.session.add(company)
        self.flush()
        return company

    def upsert_from_watchlist_csv(self, csv_path: str | Path) -> list[Company]:
        """Upsert every row of a watchlist CSV (``ticker,cik,name,sector,country``)."""
        companies: list[Company] = []
        with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = (row.get("ticker") or "").strip()
                cik = (row.get("cik") or "").strip()
                if not ticker or not cik:
                    continue  # malformed rows are skipped, never guessed
                companies.append(
                    self.upsert_company(
                        ticker=ticker,
                        cik=cik,
                        name=row.get("name"),
                        sector=row.get("sector"),
                        country=row.get("country"),
                    )
                )
        return companies
