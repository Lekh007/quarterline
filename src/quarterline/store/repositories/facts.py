"""Facts repository: observations, normalized facts, lineage, derived metrics.

Idempotency notes (Wave-0 log):
- ``fact_observations`` dedupes on the unique ``observation_hash`` (insert-or-skip).
- ``normalized_facts`` has a unique key over period identity + scope. SQLite
  treats NULL ``period_start`` (instant facts) as distinct inside a unique
  index, so upserts MUST look the row up first with an explicit ``IS NULL``
  match instead of relying on the constraint.
- ``derived_metrics`` has no unique constraint; upsert matches on
  (company, period_end, metric, formula_version).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select

from quarterline.store.models import (
    DerivedMetric,
    FactLineage,
    FactObservation,
    NormalizedFact,
    decimal_to_text,
    text_to_decimal,
)
from quarterline.store.repositories.base import BaseRepo

#: Lineage roles (SPEC 9.5).
ROLE_DIRECT_SOURCE = "direct_source"
ROLE_ANNUAL_TOTAL = "annual_total"
ROLE_PRIOR_YTD = "prior_ytd"


def _clone_fact(
    fact: NormalizedFact,
    *,
    selection_policy: str,
    value: str | None = None,
    is_derived: bool | None = None,
    derivation_method: str | None = None,
) -> NormalizedFact:
    """Detached copy of a fact row used for as-of reconstructions."""
    return NormalizedFact(
        company_id=fact.company_id,
        concept=fact.concept,
        period_start=fact.period_start,
        period_end=fact.period_end,
        fiscal_year=fact.fiscal_year,
        fiscal_quarter=fact.fiscal_quarter,
        period_kind=fact.period_kind,
        reporting_scope=fact.reporting_scope,
        value_decimal=value if value is not None else fact.value_decimal,
        unit=fact.unit,
        selection_policy=selection_policy,
        normalization_version=fact.normalization_version,
        is_derived=fact.is_derived if is_derived is None else is_derived,
        derivation_method=derivation_method
        if derivation_method is not None
        else fact.derivation_method,
        available_at=fact.available_at,
        data_quality_status=fact.data_quality_status,
    )


@dataclass(frozen=True)
class QuarterRef:
    """One quarter's period identity as stored on normalized facts."""

    company_id: int
    period_start: date | None
    period_end: date
    period_kind: str
    fiscal_year: int | None
    fiscal_quarter: str | None


class FactsRepo(BaseRepo):
    # -- observations ---------------------------------------------------------

    def get_observation_by_hash(self, observation_hash: str) -> FactObservation | None:
        return self.session.scalar(
            select(FactObservation).where(FactObservation.observation_hash == observation_hash)
        )

    def insert_observation_skip_duplicate(
        self, observation: FactObservation
    ) -> tuple[FactObservation, bool]:
        """Insert unless an observation with the same hash exists; returns (row, inserted)."""
        existing = self.get_observation_by_hash(observation.observation_hash)
        if existing is not None:
            return existing, False
        self.session.add(observation)
        self.flush()
        return observation, True

    def observations_for_company(
        self,
        company_id: int,
        concept: str | None = None,
        as_of: date | None = None,
    ) -> list[FactObservation]:
        """Observations for one company, optionally one concept.

        ``as_of`` excludes observations filed after the timestamp (SPEC 10.4) —
        an as-of query must never use information filed after it.
        """
        stmt = select(FactObservation).where(FactObservation.company_id == company_id)
        if concept is not None:
            stmt = stmt.where(FactObservation.canonical_concept == concept)
        if as_of is not None:
            stmt = stmt.where(FactObservation.filed_at.is_not(None)).where(
                FactObservation.filed_at <= as_of
            )
        return list(self.session.scalars(stmt.order_by(FactObservation.filed_at)).all())

    def count_observations(self, company_id: int) -> int:
        return len(
            list(
                self.session.scalars(
                    select(FactObservation.id).where(FactObservation.company_id == company_id)
                )
            )
        )

    # -- normalized facts -----------------------------------------------------

    def find_fact(
        self,
        company_id: int,
        concept: str,
        period_start: date | None,
        period_end: date | None,
        period_kind: str,
        reporting_scope: str | None,
    ) -> NormalizedFact | None:
        """NULL-safe lookup by the full unique period-identity key.

        SQLite treats NULLs as distinct in unique indexes, so instant facts
        (period_start IS NULL) must be matched explicitly.
        """
        stmt = select(NormalizedFact).where(
            NormalizedFact.company_id == company_id,
            NormalizedFact.concept == concept,
            NormalizedFact.period_end == period_end,
            NormalizedFact.period_kind == period_kind,
            NormalizedFact.reporting_scope == reporting_scope,
        )
        if period_start is None:
            stmt = stmt.where(NormalizedFact.period_start.is_(None))
        else:
            stmt = stmt.where(NormalizedFact.period_start == period_start)
        return self.session.scalar(stmt)

    def upsert_fact(self, fact: NormalizedFact) -> tuple[NormalizedFact, bool]:
        """Insert or update by the full period-identity key. Returns (row, created)."""
        existing = self.find_fact(
            fact.company_id,
            fact.concept,
            fact.period_start,
            fact.period_end,
            fact.period_kind,
            fact.reporting_scope,
        )
        if existing is not None:
            for field in (
                "fiscal_year",
                "fiscal_quarter",
                "value_decimal",
                "unit",
                "selection_policy",
                "normalization_version",
                "is_derived",
                "derivation_method",
                "available_at",
                "data_quality_status",
            ):
                setattr(existing, field, getattr(fact, field))
            self.flush()
            return existing, False
        self.session.add(fact)
        self.flush()
        return fact, True

    def get_fact(self, fact_id: int) -> NormalizedFact | None:
        return self.session.get(NormalizedFact, fact_id)

    def facts_for_quarter(
        self,
        company_id: int,
        period_end: date,
        kinds: tuple[str, ...] = ("quarter", "instant"),
        as_of: date | None = None,
    ) -> dict[str, NormalizedFact]:
        """Map concept -> normalized fact for one quarter (flows + instants).

        YTD and annual facts sharing the same end date are excluded by default
        so an annual value can never masquerade as the quarter (SPEC 2.1.9).
        ``as_of`` restricts the lineage observations used for *direct* facts to
        those filed at or before the timestamp; the returned fact row for a
        restated period is reconstructed from the in-force observation.
        """
        rows = list(
            self.session.scalars(
                select(NormalizedFact).where(
                    NormalizedFact.company_id == company_id,
                    NormalizedFact.period_end == period_end,
                    NormalizedFact.period_kind.in_(kinds),
                )
            )
        )
        result: dict[str, NormalizedFact] = {}
        for row in rows:
            if as_of is not None:
                in_force = (
                    self._derived_value_as_of(row, as_of)
                    if row.is_derived
                    else self._direct_value_as_of(row, as_of)
                )
                if in_force is None:
                    continue  # this fact was not yet filed/derivable at as_of
                row = in_force
            result[row.concept] = row
        return result

    def _lineage_in_force(
        self, fact: NormalizedFact, as_of: date
    ) -> list[tuple[FactObservation, FactLineage]]:
        return [
            (obs, lin)
            for obs, lin in self.lineage_for_fact(fact.id)
            if obs.filed_at is not None and obs.filed_at <= as_of
        ]

    def _derived_value_as_of(self, fact: NormalizedFact, as_of: date) -> NormalizedFact | None:
        """Reconstruct a derived fact as it was in force at ``as_of``.

        Uses the lineage roles: the latest-filed ``annual_total`` observation
        minus the latest-filed ``prior_ytd`` observation, both filed at or
        before ``as_of``. Returns None when either input was unavailable then.
        """
        in_force = self._lineage_in_force(fact, as_of)
        by_role: dict[str, FactObservation] = {}
        for obs, lin in sorted(in_force, key=lambda pair: (pair[0].filed_at, pair[0].id)):
            by_role[lin.role] = obs  # later filings overwrite earlier ones
        cumulative = by_role.get(ROLE_ANNUAL_TOTAL)
        subtracted = by_role.get(ROLE_PRIOR_YTD)
        if cumulative is None or subtracted is None:
            return None
        if cumulative.value_decimal is None or subtracted.value_decimal is None:
            return None
        value = text_to_decimal(cumulative.value_decimal) - text_to_decimal(
            subtracted.value_decimal
        )
        clone = _clone_fact(fact, selection_policy="as_of")
        clone.id = fact.id
        clone.value_decimal = decimal_to_text(value)
        return clone

    def _direct_value_as_of(self, fact: NormalizedFact, as_of: date) -> NormalizedFact | None:
        """Reconstruct a direct fact's value as it was in force at ``as_of``.

        Queries ALL observations of the fact's period identity (not just the
        lineage winner) so pre-restatement values remain point-in-time
        recoverable: among observations filed at or before ``as_of``, the
        latest filing wins. Returns None when nothing was filed yet.
        """
        stmt = select(FactObservation).where(
            FactObservation.company_id == fact.company_id,
            FactObservation.canonical_concept == fact.concept,
            FactObservation.period_end == fact.period_end,
            FactObservation.period_kind == fact.period_kind,
            FactObservation.filed_at.is_not(None),
            FactObservation.filed_at <= as_of,
        )
        if fact.period_start is None:
            stmt = stmt.where(FactObservation.period_start.is_(None))
        else:
            stmt = stmt.where(FactObservation.period_start == fact.period_start)
        observation = self.session.scalar(
            stmt.order_by(FactObservation.filed_at.desc(), FactObservation.id.desc()).limit(1)
        )
        if observation is None:
            return None
        clone = _clone_fact(fact, selection_policy="as_of", value=observation.value_decimal)
        clone.id = fact.id  # keep the id resolvable for provenance lookups
        return clone

    def quarters_available(self, company_id: int) -> list[QuarterRef]:
        """Distinct quarter period identities, oldest first (SPEC 10.5 window)."""
        rows = self.session.scalars(
            select(NormalizedFact)
            .where(NormalizedFact.company_id == company_id, NormalizedFact.period_kind == "quarter")
            .order_by(NormalizedFact.period_end)
        ).all()
        seen: dict[tuple, QuarterRef] = {}
        for row in rows:
            key = (row.period_start, row.period_end)
            if key not in seen:
                seen[key] = QuarterRef(
                    company_id=company_id,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    period_kind=row.period_kind,
                    fiscal_year=row.fiscal_year,
                    fiscal_quarter=row.fiscal_quarter,
                )
        return list(seen.values())

    def latest_annual_fact(self, company_id: int, concept: str) -> NormalizedFact | None:
        return self.session.scalar(
            select(NormalizedFact)
            .where(
                NormalizedFact.company_id == company_id,
                NormalizedFact.concept == concept,
                NormalizedFact.period_kind == "annual",
            )
            .order_by(NormalizedFact.period_end.desc())
            .limit(1)
        )

    def fact_series(
        self,
        company_id: int,
        concept: str,
        n_quarters: int,
    ) -> list[NormalizedFact]:
        """Latest ``n_quarters`` quarter-kind facts for one concept, oldest first."""
        rows = self.session.scalars(
            select(NormalizedFact)
            .where(
                NormalizedFact.company_id == company_id,
                NormalizedFact.concept == concept,
                NormalizedFact.period_kind == "quarter",
            )
            .order_by(NormalizedFact.period_end.desc())
            .limit(n_quarters)
        ).all()
        return list(reversed(rows))

    # -- lineage ----------------------------------------------------------------

    def add_lineage(
        self, normalized_fact_id: int, source_observation_id: int, role: str
    ) -> FactLineage:
        existing = self.session.scalar(
            select(FactLineage).where(
                FactLineage.normalized_fact_id == normalized_fact_id,
                FactLineage.source_observation_id == source_observation_id,
                FactLineage.role == role,
            )
        )
        if existing is not None:
            return existing
        lineage = FactLineage(
            normalized_fact_id=normalized_fact_id,
            source_observation_id=source_observation_id,
            role=role,
        )
        self.session.add(lineage)
        self.flush()
        return lineage

    def lineage_for_fact(
        self, normalized_fact_id: int
    ) -> list[tuple[FactObservation, FactLineage]]:
        """(observation, lineage) pairs feeding one normalized fact."""
        rows = self.session.execute(
            select(FactObservation, FactLineage)
            .join(FactLineage, FactLineage.source_observation_id == FactObservation.id)
            .where(FactLineage.normalized_fact_id == normalized_fact_id)
            .order_by(FactObservation.filed_at, FactObservation.id)
        ).all()
        return [(obs, lin) for obs, lin in rows]

    # -- derived metrics ----------------------------------------------------------

    def upsert_derived_metric(
        self,
        company_id: int,
        period_end: date | None,
        metric: str,
        value: object,
        unit: str | None,
        formula_version: str | None,
        status: str,
        input_fact_ids: list[int] | None = None,
        available_at: datetime | None = None,
    ) -> tuple[DerivedMetric, bool]:
        """Idempotent write of one derived metric value."""
        import json

        existing = self.session.scalar(
            select(DerivedMetric).where(
                DerivedMetric.company_id == company_id,
                DerivedMetric.period_end == period_end,
                DerivedMetric.metric == metric,
                DerivedMetric.formula_version == formula_version,
            )
        )
        value_text = decimal_to_text(value) if value is not None else None
        input_json = json.dumps(input_fact_ids or [])
        if existing is not None:
            existing.value_decimal = value_text
            existing.unit = unit
            existing.status = status
            existing.input_fact_ids_json = input_json
            existing.available_at = available_at or existing.available_at
            self.flush()
            return existing, False
        row = DerivedMetric(
            company_id=company_id,
            period_end=period_end,
            metric=metric,
            value_decimal=value_text,
            unit=unit,
            formula_version=formula_version,
            status=status,
            input_fact_ids_json=input_json,
            available_at=available_at,
        )
        self.session.add(row)
        self.flush()
        return row, True

    def metric_series(
        self,
        company_id: int,
        metric: str,
        n_quarters: int,
        formula_version: str | None = None,
    ) -> list[DerivedMetric]:
        """Latest ``n_quarters`` derived-metric values, oldest first."""
        stmt = select(DerivedMetric).where(
            DerivedMetric.company_id == company_id,
            DerivedMetric.metric == metric,
        )
        if formula_version is not None:
            stmt = stmt.where(DerivedMetric.formula_version == formula_version)
        rows = self.session.scalars(
            stmt.order_by(DerivedMetric.period_end.desc()).limit(n_quarters)
        ).all()
        return list(reversed(rows))

    def get_metric(
        self,
        company_id: int,
        metric: str,
        period_end: date | None,
        formula_version: str | None = None,
    ) -> DerivedMetric | None:
        stmt = select(DerivedMetric).where(
            DerivedMetric.company_id == company_id,
            DerivedMetric.metric == metric,
            DerivedMetric.period_end == period_end,
        )
        if formula_version is not None:
            stmt = stmt.where(DerivedMetric.formula_version == formula_version)
        return self.session.scalar(stmt)

    # -- provenance helpers -------------------------------------------------------

    def observation(self, observation_id: int) -> FactObservation | None:
        return self.session.get(FactObservation, observation_id)

    def decimal_value(self, fact: NormalizedFact) -> object:
        return text_to_decimal(fact.value_decimal)
