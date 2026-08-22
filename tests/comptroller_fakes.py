"""Shared in-memory PostgREST double for Comptroller feature tests.

Not a test file itself (no test_ prefix). Mimics just enough of Supabase's
PostgREST surface for the exact request shapes app.comptroller.* issues, so
tests never touch a real network or a real database, matching this repo's
existing convention of monkeypatching `_request_json` directly
(see tests/test_district_service.py).
"""

from __future__ import annotations

from typing import Any


class FakeSupabase:
    def __init__(self) -> None:
        self.permit_locations: dict[str, dict[str, Any]] = {}
        self.status_events: dict[str, dict[str, Any]] = {}
        self.sync_runs: dict[str, dict[str, Any]] = {}
        self.districts: list[dict[str, Any]] = []
        self.closure_reviews: dict[str, dict[str, Any]] = {}
        self.jurisdictions: dict[str, dict[str, Any]] = {}
        self.intelligence_items: dict[str, dict[str, Any]] = {}
        self.real_property_records: dict[str, dict[str, Any]] = {}
        self.property_source_imports: dict[str, dict[str, Any]] = {}
        self.property_enrichment_results: dict[str, dict[str, Any]] = {}
        self._next_id = 0
        self.calls: list[dict[str, Any]] = []

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    # -- filter helpers ----------------------------------------------------

    @staticmethod
    def _pg_str(value: Any) -> str:
        # PostgREST/Postgres booleans serialize lowercase ("true"/"false"),
        # not Python's str(True) == "True" -- a real filter (is_baseline=eq.false)
        # would silently never match without this.
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _matches_eq(row: dict[str, Any], field: str, expected: str) -> bool:
        return FakeSupabase._pg_str(row.get(field)) == expected

    @classmethod
    def _apply_simple_filters(cls, rows: list[dict[str, Any]], params: dict[str, Any], skip: set[str]) -> list[dict[str, Any]]:
        result = rows
        for key, value in params.items():
            if key in skip or key in ("select", "order", "limit", "offset", "on_conflict"):
                continue
            conditions = value if isinstance(value, list) else [value]
            for condition in conditions:
                condition = str(condition)
                if condition == "is.null":
                    result = [r for r in result if r.get(key) is None]
                elif condition.startswith("eq."):
                    expected = condition[len("eq."):]
                    result = [r for r in result if cls._pg_str(r.get(key)) == expected]
                elif condition.startswith("gte."):
                    expected = condition[len("gte."):]
                    result = [r for r in result if r.get(key) is not None and str(r.get(key)) >= expected]
                elif condition.startswith("lt."):
                    expected = condition[len("lt."):]
                    result = [r for r in result if r.get(key) is not None and str(r.get(key)) < expected]
                elif condition.startswith("not.in.("):
                    excluded = condition[len("not.in.("):-1].split(",")
                    result = [r for r in result if str(r.get(key)) not in excluded]
                elif condition.startswith("ilike."):
                    result = [r for r in result if cls._matches_ilike(r.get(key), condition[len("ilike."):])]
        return result

    @staticmethod
    def _matches_ilike(value: Any, pattern: str) -> bool:
        # PostgREST ilike uses "*" where SQL ilike uses "%".
        if value is None:
            return False
        value = str(value).upper()
        prefix, suffix = pattern.startswith("*"), pattern.endswith("*")
        core = pattern.strip("*").upper()
        if prefix and suffix:
            return core in value
        if suffix:
            return value.startswith(core)
        if prefix:
            return value.endswith(core)
        return value == core

    # -- request dispatch ----------------------------------------------------

    def request_json(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        params: dict[str, Any] | None = None,
        json_payload: Any = None,
    ) -> Any:
        params = params or {}
        self.calls.append({"method": method, "url": url, "params": params, "json_payload": json_payload})
        table = url.rsplit("/", 1)[-1]
        method = method.upper()

        # Real PostgREST rejects a bulk insert/upsert whose objects don't all
        # share the exact same key set ("All object keys must match") -- a
        # real bug (conditionally-included columns in a mixed batch) was only
        # caught via live production testing because this fake didn't
        # enforce that constraint. Enforcing it here means any future
        # regression of the same shape fails in the test suite instead.
        if method == "POST" and isinstance(json_payload, list) and len(json_payload) > 1:
            key_sets = {frozenset(row.keys()) for row in json_payload}
            if len(key_sets) > 1:
                raise AssertionError(
                    f"All object keys must match (PostgREST bulk insert into '{table}' has "
                    f"inconsistent object shapes across the batch): {key_sets}"
                )

        handler = getattr(self, f"_handle_{table}", None)
        if handler is None:
            raise AssertionError(f"FakeSupabase has no handler for table '{table}'")
        return handler(method, params, json_payload)

    # -- comptroller_permit_locations ----------------------------------------

    def _handle_comptroller_permit_locations(self, method, params, json_payload):
        if method == "GET":
            rows = self._apply_simple_filters(list(self.permit_locations.values()), params, skip=set())
            return self._paginate(rows, params)
        if method == "POST":
            payload_rows = json_payload if isinstance(json_payload, list) else [json_payload]
            results = []
            for incoming in payload_rows:
                key = f"{incoming['taxpayer_id']}::{incoming['location_number']}"
                existing = self.permit_locations.get(key)
                if existing:
                    merged = {**existing, **incoming, "id": existing["id"]}
                    self.permit_locations[key] = merged
                    results.append(merged)
                else:
                    new_row = dict(incoming)
                    new_row["id"] = self._new_id("loc")
                    new_row.setdefault("is_baseline", False)
                    self.permit_locations[key] = new_row
                    results.append(new_row)
            return results
        if method == "PATCH":
            target_id = self._extract_eq(params, "id")
            row = next((r for r in self.permit_locations.values() if r.get("id") == target_id), None)
            if row is not None:
                row.update(json_payload)
            return [row] if row else []
        raise AssertionError(f"Unhandled method {method} for comptroller_permit_locations")

    # -- comptroller_permit_status_events ------------------------------------

    def _handle_comptroller_permit_status_events(self, method, params, json_payload):
        if method == "POST":
            payload_rows = json_payload if isinstance(json_payload, list) else [json_payload]
            results = []
            for incoming in payload_rows:
                row = dict(incoming)
                row["id"] = self._new_id("evt")
                row.setdefault("month_end_processed_at", None)
                row.setdefault("review_item_id", None)
                self.status_events[row["id"]] = row
                results.append(row)
            return results
        if method == "GET":
            select = str(params.get("select") or "")
            embed_locations = "comptroller_permit_locations(" in select
            rows = self._apply_simple_filters(list(self.status_events.values()), params, skip=set())
            if embed_locations:
                enriched = []
                for row in rows:
                    row = dict(row)
                    location = None
                    for loc in self.permit_locations.values():
                        if loc.get("id") == row.get("permit_location_id"):
                            location = loc
                            break
                    row["comptroller_permit_locations"] = location
                    enriched.append(row)
                rows = enriched
            return self._paginate(rows, params)
        if method == "PATCH":
            target_id = self._extract_eq(params, "id")
            row = self.status_events.get(target_id)
            if row is not None:
                row.update(json_payload)
            return [row] if row else []
        raise AssertionError(f"Unhandled method {method} for comptroller_permit_status_events")

    # -- comptroller_sync_runs ------------------------------------------------

    def _handle_comptroller_sync_runs(self, method, params, json_payload):
        if method == "POST":
            row = dict(json_payload)
            row["id"] = self._new_id("run")
            row["started_at"] = f"2000-01-01T00:00:{len(self.sync_runs):02d}Z"
            self.sync_runs[row["id"]] = row
            return [row]
        if method == "GET":
            rows = self._apply_simple_filters(list(self.sync_runs.values()), params, skip=set())
            order = str(params.get("order") or "")
            if "started_at.desc" in order:
                rows = sorted(rows, key=lambda r: r.get("started_at", ""), reverse=True)
            return self._paginate(rows, params)
        if method == "PATCH":
            target_id = self._extract_eq(params, "id")
            row = self.sync_runs.get(target_id)
            if row is not None:
                row.update(json_payload)
            return [row] if row else []
        raise AssertionError(f"Unhandled method {method} for comptroller_sync_runs")

    # -- districts -------------------------------------------------------------

    def _handle_districts(self, method, params, json_payload):
        if method == "GET":
            rows = self._apply_simple_filters(list(self.districts), params, skip=set())
            return self._paginate(rows, params)
        raise AssertionError(f"Unhandled method {method} for districts")

    # -- jurisdictions -----------------------------------------------------------

    def _handle_jurisdictions(self, method, params, json_payload):
        if method == "GET":
            rows = self._apply_simple_filters(list(self.jurisdictions.values()), params, skip=set())
            return self._paginate(rows, params)
        raise AssertionError(f"Unhandled method {method} for jurisdictions")

    # -- bpp_intelligence_items --------------------------------------------------

    def _handle_bpp_intelligence_items(self, method, params, json_payload):
        if method == "POST":
            row = dict(json_payload)
            row["id"] = self._new_id("intel")
            row.setdefault("created_at", f"2000-01-01T00:00:{len(self.intelligence_items):02d}Z")
            row.setdefault("updated_at", row["created_at"])
            self.intelligence_items[row["id"]] = row
            return [row]
        if method == "GET":
            rows = self._apply_simple_filters(list(self.intelligence_items.values()), params, skip=set())
            return self._paginate(rows, params)
        if method == "PATCH":
            target_id = self._extract_eq(params, "id")
            row = self.intelligence_items.get(target_id)
            if row is not None:
                row.update(json_payload)
            return [row] if row else []
        raise AssertionError(f"Unhandled method {method} for bpp_intelligence_items")

    # -- comptroller_closure_reviews --------------------------------------------

    def _handle_comptroller_closure_reviews(self, method, params, json_payload):
        if method == "POST":
            status_event_id = json_payload.get("status_event_id")
            if any(r.get("status_event_id") == status_event_id for r in self.closure_reviews.values()):
                return []  # resolution=ignore-duplicates: conflicting row, nothing returned
            row = dict(json_payload)
            row["id"] = self._new_id("review")
            self.closure_reviews[row["id"]] = row
            return [row]
        if method == "GET":
            rows = self._apply_simple_filters(list(self.closure_reviews.values()), params, skip=set())
            return self._paginate(rows, params)
        if method == "PATCH":
            target_id = self._extract_eq(params, "id")
            row = self.closure_reviews.get(target_id)
            if row is not None:
                row.update(json_payload)
            return [row] if row else []
        raise AssertionError(f"Unhandled method {method} for comptroller_closure_reviews")

    # -- real_property_records -----------------------------------------------

    def _handle_real_property_records(self, method, params, json_payload):
        if method == "GET":
            rows = self._apply_simple_filters(list(self.real_property_records.values()), params, skip=set())
            return self._paginate(rows, params)
        if method == "POST":
            payload_rows = json_payload if isinstance(json_payload, list) else [json_payload]
            results = []
            for incoming in payload_rows:
                key = f"{incoming['jurisdiction_id']}::{incoming['source_property_id']}"
                existing = self.real_property_records.get(key)
                if existing:
                    merged = {**existing, **incoming, "id": existing["id"]}
                    self.real_property_records[key] = merged
                    results.append(merged)
                else:
                    new_row = dict(incoming)
                    new_row["id"] = self._new_id("prop")
                    self.real_property_records[key] = new_row
                    results.append(new_row)
            return results
        raise AssertionError(f"Unhandled method {method} for real_property_records")

    # -- property_source_imports ----------------------------------------------

    def _handle_property_source_imports(self, method, params, json_payload):
        if method == "POST":
            payload_row = json_payload[0] if isinstance(json_payload, list) else json_payload
            row = dict(payload_row)
            row["id"] = self._new_id("import")
            row.setdefault("imported_at", f"2000-01-01T00:00:{len(self.property_source_imports):02d}Z")
            self.property_source_imports[row["id"]] = row
            return [row]
        if method == "GET":
            rows = self._apply_simple_filters(list(self.property_source_imports.values()), params, skip=set())
            order = str(params.get("order") or "")
            if "imported_at.desc" in order:
                rows = sorted(rows, key=lambda r: r.get("imported_at", ""), reverse=True)
            return self._paginate(rows, params)
        if method == "PATCH":
            target_id = self._extract_eq(params, "id")
            row = self.property_source_imports.get(target_id)
            if row is not None:
                row.update(json_payload)
            return [row] if row else []
        raise AssertionError(f"Unhandled method {method} for property_source_imports")

    # -- property_enrichment_results -------------------------------------------

    def _handle_property_enrichment_results(self, method, params, json_payload):
        if method == "GET":
            rows = self._apply_simple_filters(list(self.property_enrichment_results.values()), params, skip=set())
            return self._paginate(rows, params)
        if method == "POST":
            payload_rows = json_payload if isinstance(json_payload, list) else [json_payload]
            results = []
            for incoming in payload_rows:
                key = f"{incoming['jurisdiction_id']}::{incoming['subject_type']}::{incoming['subject_id']}"
                existing = self.property_enrichment_results.get(key)
                if existing:
                    # merge-duplicates upsert: only columns present in the
                    # incoming payload are replaced -- review_status (never
                    # sent by property_enrichment.py on refresh) survives.
                    merged = {**existing, **incoming, "id": existing["id"]}
                    self.property_enrichment_results[key] = merged
                    results.append(merged)
                else:
                    new_row = dict(incoming)
                    new_row["id"] = self._new_id("penrich")
                    new_row.setdefault("review_status", "NOT_REVIEWED")
                    new_row.setdefault("created_at", f"2000-01-01T00:00:{len(self.property_enrichment_results):02d}Z")
                    new_row["updated_at"] = new_row["created_at"]
                    self.property_enrichment_results[key] = new_row
                    results.append(new_row)
            return results
        raise AssertionError(f"Unhandled method {method} for property_enrichment_results")

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _extract_eq(params: dict[str, Any], field: str) -> str | None:
        value = params.get(field)
        if isinstance(value, str) and value.startswith("eq."):
            return value[len("eq."):]
        return None

    @staticmethod
    def _paginate(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
        limit = params.get("limit")
        offset = int(params.get("offset") or 0)
        if limit is None:
            return rows[offset:]
        return rows[offset : offset + int(limit)]
