"""Typed repositories (contract C5). Import the concrete repos from here."""

from quarterline.store.repositories.base import BaseRepo
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import (
    ROLE_ANNUAL_TOTAL,
    ROLE_DIRECT_SOURCE,
    ROLE_PRIOR_YTD,
    FactsRepo,
    QuarterRef,
)

__all__ = [
    "ROLE_ANNUAL_TOTAL",
    "ROLE_DIRECT_SOURCE",
    "ROLE_PRIOR_YTD",
    "BaseRepo",
    "CompaniesRepo",
    "FactsRepo",
    "QuarterRef",
]
