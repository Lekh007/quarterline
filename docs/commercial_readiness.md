# Quarterline — Commercial-Readiness Boundary (SPEC §28)

This document is the honest boundary of the release. Quarterline is a
**local, single-user research and education tool built as an AI-engineering
portfolio project**. It is NOT a commercial product, and this page describes
the work a commercial launch would additionally require. None of the items
below are implemented; they are listed so nobody mistakes the current state
for production readiness.

## What a commercial launch would require (all NOT done)

### 1. Data-provider licensing and redistribution review

SEC EDGAR data is public but public accessibility does not grant commercial
redistribution rights (SPEC §2.4.5). A hosted product needs: terms-of-service
review for SEC/EDGAR bulk access, attribution and disclaimer obligations,
license review for vendored front-end assets (htmx 1.9.12 0-Clause BSD,
Chart.js 4.4.3 MIT — recorded in `src/quarterline/api/static/LICENSES.md`),
model-license review (qwen3:4b, nomic-embed-text), and a decision on whether
derived metrics may be redistributed. Current status: single-user local use
only; the full local filing cache is gitignored and never redistributed.

### 2. Regulatory review for investment recommendations

Quarterline enforces research-only behavior in code (`advice-policy-v1`
refusals, rule-based quarter labels with fixed disclaimers, the experimental
fundamental score labeled as such), but no lawyer has reviewed whether any of
it constitutes investment advice in any jurisdiction. SPEC §28 is explicit:
**no personalized buy/sell engine is implemented in this release**, and any
future recommendation research must be a separately approved scope with
point-in-time evaluation and appropriate legal review. The as-of selection is
tested for future-filing exclusion, but no backtest is claimed point-in-time
safe beyond that behavior (SPEC §10.4).

### 3. Privacy and retention policies

No privacy policy, no retention schedule, no data-subject workflow exists.
Today: SEC-fair-access identity lives in `.env`; questions/queries are stored
hash+length only; run events exclude user content. A hosted release needs a
privacy policy, data-retention and deletion guarantees, and jurisdictional
review (GDPR/CCPA or equivalents).

### 4. Authentication and authorization

There is none. The API binds `127.0.0.1` and trusts the local user. The
PostgreSQL profile ships documented local-dev credentials
(`docker-compose.yml`). A hosted release needs real authn (SSO/passkeys at
minimum), authorization, session management, and transport security.

### 5. Multi-user data isolation

The data model has a single implicit user; there is no tenant concept, no
per-user quotas, no row-level isolation. The agent's approval flow is a
single-user human-in-the-loop control, not an authorization boundary.

### 6. Operational monitoring and backups

Observability exists for a local desk (structured events, metrics with sample
sizes, dashboard, eval reports) but there is: no alerting, no on-call, no SLOs,
no backup/restore procedure, no disaster recovery, no upgrade/rollback story
for the database beyond Alembic migrations. The dashboard explicitly does not
claim production monitoring (SPEC §24: "Do not claim production monitoring
unless the system is actually deployed and serving users").

### 7. Hosted-inference economics

Local inference via Ollama has no per-request API charge but is NOT free —
compute costs are not zero, and `estimated_api_cost` is deliberately `null`
with an explicit `cost_basis` rather than a fake "free" label (SPEC §24). A
hosted release needs per-request cost accounting, capacity planning, GPU
sizing, and provider rate-limit/budget management. No ingestion time or token
throughput is promised without measuring target hardware (SPEC §29).

### 8. Abuse and rate controls

The only rate control implemented is **outbound** SEC throttling (≥200 ms
spacing, backoff) — a fair-access compliance feature, not an inbound defense.
There is no inbound request limiting, no quota enforcement, no abuse
detection, no WAF. The agent's tool/transition budgets bound per-run cost,
not platform abuse.

### 9. More extensive financial-data QA

Current QA: 561 passing tests incl. real-AAPL fixture reconciliation against
trimmed companyfacts, hand-derived screener expectations, Decimal-exact
roundtrips. A commercial release would need: reconciliation of AAPL/MSFT (and
eventually the full 15-company watchlist) values against the official filings
**by a human accountant** (SPEC §31.2–31.3 — currently verified only for the
fixture subset, and live ingestion of the full watchlist is unverified while
`EDGAR_IDENTITY` remains a placeholder), restatement/correction handling
review at scale, unit/rounding audits across more industries and fiscal
calendars, and independent verification of the derivation rules.

## What this release is NOT

- Not investment advice, not a recommendation engine, not a broker-dealer
  tool. Research and education only.
- Not "hallucination-free": the validation gate bounds what generated text can
  claim, but passing those criteria does not justify the term (SPEC §31).
- Not "investment-grade" data quality: no audited reconciliation beyond
  committed fixtures.
- Not multi-user, not hosted, not internet-facing, not hardened.
- Not monitoring-backed production software.
- Not an India-market product: the India expansion (SPEC §27) is explicitly
  out of scope for this release and begins only after the US portfolio
  release or explicit reprioritization; no NSE/BSE code was written.
- Not verified against a live PostgreSQL instance in this environment (the
  pgvector profile's contract tests exist and skip cleanly; executing them
  requires a Postgres DBAPI that is not yet a project dependency).

## Honest verification status of the platform itself

See `README.md` (status table) and `docs/implementation_log.md` for the
per-wave verified/unverified split. Summary: 561 tests pass offline; live SEC
ingestion, live Ollama generation/embedding quality, and the PostgreSQL
contract execution are the remaining unverified-or-pending items at this
release.
