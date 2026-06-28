"""Tenancy resolution (Handoff §3a) — turn the staged string keys into the ``case_id`` / ``dataset_id`` UUIDs
that ``pipeline.documents`` requires as NOT-NULL FKs.

Policy (decided = **A**): **require pre-created** ``pipeline.cases`` / ``pipeline.datasets`` rows and **fail
loud** if absent — a bulk live load must never silently fabricate org structure. ``--ensure-tenancy`` (config
``ensure_tenancy=True``) opts into **(b) upsert-on-first-sight** for a dev/staging bootstrap.

All work runs inside the caller's ``db.tenant(tenant_id)`` transaction (RLS bound), so every insert/select is
already scoped to the chosen tenant. Results are cached per process to avoid re-querying per document. Pure
SQL against a psycopg cursor — unit-testable with a fake cursor, no live DB needed to exercise the logic.
"""

from __future__ import annotations


class TenancyError(RuntimeError):
    """Raised under policy (a) when a required case/dataset row does not exist (fail-loud preflight)."""


class TenancyResolver:
    def __init__(self, *, ensure: bool = False) -> None:
        self.ensure = ensure
        self._cases: dict[str, str] = {}                  # case_key -> case_id
        self._datasets: dict[tuple[str, str], str] = {}   # (case_id, dataset_key) -> dataset_id

    def resolve(self, cur, tenant_id: str, case_key: str, dataset_key: str) -> tuple[str, str]:
        """Return ``(case_id, dataset_id)`` for the keys, creating them only when ``ensure`` is set."""
        case_id = self._resolve_case(cur, tenant_id, case_key)
        dataset_id = self._resolve_dataset(cur, tenant_id, case_id, case_key, dataset_key)
        return case_id, dataset_id

    def _resolve_case(self, cur, tenant_id: str, case_key: str) -> str:
        if case_key in self._cases:
            return self._cases[case_key]
        cur.execute("SELECT case_id FROM pipeline.cases WHERE tenant_id=%s AND case_key=%s",
                    (tenant_id, case_key))
        row = cur.fetchone()
        if row:
            self._cases[case_key] = str(row[0])
            return self._cases[case_key]
        if not self.ensure:
            raise TenancyError(
                f"case '{case_key}' not found for tenant {tenant_id}. Pre-create pipeline.cases (policy A) "
                f"or re-run with --ensure-tenancy to upsert it.")
        cur.execute(
            "INSERT INTO pipeline.cases (tenant_id, case_key) VALUES (%s,%s) "
            "ON CONFLICT (tenant_id, case_key) DO UPDATE SET updated_at=now() RETURNING case_id",
            (tenant_id, case_key))
        self._cases[case_key] = str(cur.fetchone()[0])
        return self._cases[case_key]

    def _resolve_dataset(self, cur, tenant_id: str, case_id: str, case_key: str, dataset_key: str) -> str:
        key = (case_id, dataset_key)
        if key in self._datasets:
            return self._datasets[key]
        cur.execute(
            "SELECT dataset_id FROM pipeline.datasets WHERE tenant_id=%s AND case_id=%s AND dataset_key=%s",
            (tenant_id, case_id, dataset_key))
        row = cur.fetchone()
        if row:
            self._datasets[key] = str(row[0])
            return self._datasets[key]
        if not self.ensure:
            raise TenancyError(
                f"dataset '{dataset_key}' not found under case '{case_key}' for tenant {tenant_id}. "
                f"Pre-create pipeline.datasets (policy A) or re-run with --ensure-tenancy.")
        cur.execute(
            "INSERT INTO pipeline.datasets (tenant_id, case_id, case_key, dataset_key) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id, case_id, dataset_key) DO UPDATE SET updated_at=now() RETURNING dataset_id",
            (tenant_id, case_id, case_key, dataset_key))
        self._datasets[key] = str(cur.fetchone()[0])
        return self._datasets[key]


def preflight(cur, tenant_id: str, pairs: list, *, ensure: bool) -> dict:
    """Dry preflight for the apply manifest: which (case_key, dataset_key) pairs already exist vs would be
    created. Does NOT write (even when ensure=True) — pure read, so it can run before the load commits."""
    found, missing = [], []
    for case_key, dataset_key in pairs:
        cur.execute("SELECT case_id FROM pipeline.cases WHERE tenant_id=%s AND case_key=%s",
                    (tenant_id, case_key))
        c = cur.fetchone()
        if not c:
            missing.append([case_key, dataset_key]); continue
        cur.execute(
            "SELECT dataset_id FROM pipeline.datasets WHERE tenant_id=%s AND case_id=%s AND dataset_key=%s",
            (tenant_id, c[0], dataset_key))
        (found if cur.fetchone() else missing).append([case_key, dataset_key])
    return {"found": found, "missing": missing,
            "policy": "ensure" if ensure else "require_precreated",
            "would_fail": [] if ensure else missing}
