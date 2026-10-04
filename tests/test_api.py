"""End-to-end API tests (FastAPI TestClient over a temporary SQLite file)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.seed import seed_demo_data
from app.service import LicenseService


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=False, echo=False)
    app = create_app(settings)
    # The lifespan creates the schema (INIT_DB=true) and owns the connection.
    with TestClient(app) as test_client:
        yield test_client


def _iso(y: int, m: int, d: int, hh: int = 0) -> str:
    return datetime(y, m, d, hh, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _register(client, version_id, parents=None, note=""):
    resp = client.post(
        "/versions",
        json={"version_id": version_id, "parent_ids": parents or [], "note": note},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _change(client, change_id, version_id, at, uses):
    resp = client.post(
        "/license-changes",
        json={
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": at,
            "allowed_uses": uses,
        },
    )
    return resp


def _check(client, version_id, use, at):
    return client.get(
        "/permissions/check",
        params={"version_id": version_id, "use": use, "at": at},
    )


def _preview(client, payload):
    return client.post("/license-changes/preview", json=payload)


def _preview_flip(client, change_id, version_id, effective_at, uses, use, at):
    return _preview(
        client,
        {
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": effective_at,
            "allowed_uses": uses,
            "use": use,
            "at": at,
        },
    )


# -- registration ----------------------------------------------------------


def test_register_version_with_unknown_parent_is_404(client):
    resp = client.post(
        "/versions", json={"version_id": "v1", "parent_ids": ["ghost"]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_parent_ids_deduplicated_and_immutable(client):
    _register(client, "p1")
    body = _register(client, "c1", ["p1", "p1"])
    assert body["version"]["parent_ids"] == ["p1"]

    # Re-registering the same id conflicts; there is no edit endpoint.
    resp = client.post("/versions", json={"version_id": "c1", "parent_ids": []})
    assert resp.status_code == 409


def test_unknown_version_query_is_404(client):
    assert client.get("/versions/nope").status_code == 404
    assert _check(client, "nope", "research", _iso(2026, 5, 1)).status_code == 404


# -- license changes -------------------------------------------------------


def test_change_requires_existing_version(client):
    resp = _change(client, "c1", "nope", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 404


def test_duplicate_submission_same_id_same_content_is_idempotent(client):
    _register(client, "v1")
    payload = {
        "change_id": "c1",
        "version_id": "v1",
        "effective_at": _iso(2026, 1, 1),
        "allowed_uses": ["research", "review"],
    }
    first = client.post("/license-changes", json=payload)
    assert first.status_code == 201
    assert first.json()["replayed"] is False

    # Same content, uses reordered and duplicated -> identical result.
    second = client.post(
        "/license-changes",
        json={**payload, "allowed_uses": ["review", "research", "research"]},
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["change"] == first.json()["change"]


def test_same_id_different_content_conflicts(client):
    _register(client, "v1")
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"]).status_code == 201

    # Different uses.
    resp = _change(client, "c1", "v1", _iso(2026, 1, 1), ["commercial"])
    assert resp.status_code == 409
    # Different effective time with the same id.
    resp = _change(client, "c1", "v1", _iso(2026, 2, 1), ["research"])
    assert resp.status_code == 409
    # Different target version with the same id.
    _register(client, "v2")
    resp = _change(client, "c1", "v2", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 409


def test_same_version_same_time_different_change_conflicts(client):
    _register(client, "v1")
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"]).status_code == 201
    resp = _change(client, "c2", "v1", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]


def test_latest_change_wins_and_naive_time_rejected(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research"])

    before = _check(client, "v1", "review", _iso(2026, 5, 1)).json()
    assert before["allowed"] is True
    after = _check(client, "v1", "review", _iso(2026, 6, 1)).json()
    assert after["allowed"] is False
    assert after["blockers"][0]["reason"] == "use_not_allowed"
    assert after["blockers"][0]["change_id"] == "c2"

    # Timezone-aware values in other offsets are accepted; naive is 422.
    resp = client.post(
        "/license-changes",
        json={
            "change_id": "c3",
            "version_id": "v1",
            "effective_at": "2026-09-01T08:00:00+08:00",
            "allowed_uses": ["research"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["change"]["effective_at"].startswith("2026-09-01T00:00:00")

    bad = client.post(
        "/license-changes",
        json={
            "change_id": "c4",
            "version_id": "v1",
            "effective_at": "2026-09-01T00:00:00",
            "allowed_uses": ["research"],
        },
    )
    assert bad.status_code == 422


# -- permission evaluation -------------------------------------------------


def test_ancestor_intersection_denies_and_reports_blockers(client):
    _register(client, "a")
    _register(client, "b")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research", "commercial"])
    _register(client, "d1", ["a", "b"])
    _change(client, "cd1", "d1", _iso(2026, 2, 1), ["research", "review", "commercial"])

    at = _iso(2026, 3, 1)
    ok = _check(client, "d1", "research", at).json()
    assert ok["allowed"] is True
    assert ok["closure"] == ["d1", "a", "b"]
    assert {entry["version_id"] for entry in ok["basis"]} == {"d1", "a", "b"}
    assert ok["allowed_uses_intersection"] == ["research"]

    denied = _check(client, "d1", "review", at).json()
    assert denied["allowed"] is False
    reasons = {(bl["version_id"], bl["reason"]) for bl in denied["blockers"]}
    assert ("b", "use_not_allowed") in reasons
    assert all(bl["version_id"] != "a" for bl in denied["blockers"])


def test_diamond_ancestors_deduplicated(client):
    # a <- b <- d1, a <- d1 : a must only appear once in the closure.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["b", "a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research"])

    body = _check(client, "d1", "research", _iso(2026, 2, 1)).json()
    assert body["closure"] == ["d1", "b", "a"]
    assert body["allowed"] is True


def test_no_valid_license_blocks(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 3, 1), ["research"])
    # Before the change takes effect there is no valid license.
    body = _check(client, "a", "research", _iso(2026, 2, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]
    assert body["basis"] == []
    assert body["allowed_uses_intersection"] == []

    # A child inherits the block from its parent.
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "commercial"])
    body = _check(client, "d1", "research", _iso(2026, 2, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]


def test_naive_query_at_is_422(client):
    _register(client, "a")
    resp = client.get(
        "/permissions/check",
        params={"version_id": "a", "use": "research", "at": "2026-01-01T00:00:00"},
    )
    assert resp.status_code == 422


# -- demo seed -------------------------------------------------------------


def test_demo_seed_is_idempotent(tmp_path):
    db_path = str(tmp_path / "seed.db")
    db = Database(db_path)
    db.init_schema()
    assert seed_demo_data(db) is True
    # Second run on the non-empty DB is skipped; resubmitting directly still
    # returns the same stored change (replay).
    assert seed_demo_data(db) is False
    change, replayed = LicenseService(db).submit_change(
        change_id="demo-chg-a-1",
        version_id="demo-root-a",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        allowed_uses=["research", "review"],
    )
    assert replayed is True
    assert change["change_id"] == "demo-chg-a-1"
    db.close()


def test_demo_fixture_permissions(tmp_path):
    db_path = str(tmp_path / "seed2.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        at = _iso(2026, 3, 1)
        research = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "research", "at": at},
        ).json()
        assert research["allowed"] is True

        commercial = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "commercial", "at": at},
        ).json()
        assert commercial["allowed"] is False
        assert commercial["blockers"][0]["version_id"] == "demo-root-a"

        nolicense = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-no-license", "use": "research", "at": at},
        ).json()
        assert nolicense["allowed"] is False
        assert nolicense["blockers"][0]["reason"] == "no_valid_license"

        # After root-a's June change drops review, intersection still allows
        # only research; research stays allowed.
        later = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "research", "at": _iso(2026, 7, 1)},
        ).json()
        assert later["allowed"] is True


# -- change preview --------------------------------------------------------


def _preview_graph(client):
    # a <- b <- d1, a <- d2 <- d1  (diamond: a is a shared ancestor of d1).
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d2", ["a"])
    _change(client, "cd2", "d2", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["b", "d2"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])


def test_preview_flips_target_and_descendants_deny_to_allow(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    at = _iso(2026, 5, 1)
    # Current state: review is blocked by a for both a and d1.
    assert _check(client, "a", "review", at).json()["allowed"] is False
    assert _check(client, "d1", "review", at).json()["allowed"] is False

    resp = _preview_flip(client, "ca2", "a", _iso(2026, 4, 1), ["research", "review"], "review", at)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["scope"] == ["a", "d1"]
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a", "d1"}
    for entry in flips.values():
        assert entry["before_allowed"] is False
        assert entry["after_allowed"] is True
        assert entry["before_blockers"]
        assert all(bl["version_id"] == "a" and bl["reason"] == "use_not_allowed"
                   for bl in entry["before_blockers"])
        assert entry["after_blockers"] == []
    assert flips["d1"]["closure"] == ["d1", "a"]
    assert flips["a"]["after_allowed_uses_intersection"] == ["research", "review"]


def test_preview_flip_allow_to_deny_reports_after_blocker(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    at = _iso(2026, 5, 1)
    body = _preview_flip(client, "ca2", "a", _iso(2026, 4, 1), ["research"], "review", at).json()
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a", "d1"}
    for entry in flips.values():
        assert entry["before_allowed"] is True
        assert entry["after_allowed"] is False
        assert entry["before_blockers"] == []
        assert len(entry["after_blockers"]) == 1
        blocker = entry["after_blockers"][0]
        assert blocker["version_id"] == "a"
        assert blocker["reason"] == "use_not_allowed"
        assert blocker["change_id"] == "ca2"


def test_preview_query_before_effective_time_has_no_impact(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    at = _iso(2026, 3, 1)

    # Effective after the query instant: the proposal is not yet in force.
    body = _preview_flip(client, "cf", "a", _iso(2026, 4, 1), ["research", "review"], "review", at).json()
    assert body["scope"] == ["a"]
    assert body["impacted_versions"] == []
    # And nothing was written.
    assert _check(client, "a", "review", at).json()["allowed"] is False
    assert client.get("/license-changes/cf").status_code == 404


def test_preview_covered_by_later_stored_change_has_no_impact(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research"])
    # A stored change already effective later than the proposal wins.
    _change(client, "ca3", "a", _iso(2026, 5, 1), ["research", "review"])
    at = _iso(2026, 8, 1)

    body = _preview_flip(client, "ca2", "a", _iso(2026, 3, 1), ["review", "research"], "review", at).json()
    assert body["impacted_versions"] == []
    assert client.get("/license-changes/ca2").status_code == 404

    # A proposal effective later than every stored change does flip the state.
    body = _preview_flip(client, "ca4", "a", _iso(2026, 6, 1), ["research"], "review", at).json()
    entry = body["impacted_versions"][0]
    assert entry["version_id"] == "a"
    assert entry["before_allowed"] is True
    assert entry["after_allowed"] is False
    assert entry["after_blockers"][0]["change_id"] == "ca4"
    assert client.get("/license-changes/ca4").status_code == 404


def test_preview_unknown_version_is_404(client):
    resp = _preview_flip(client, "c1", "ghost", _iso(2026, 1, 1), ["research"], "research", _iso(2026, 2, 1))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_preview_change_id_conflict_is_409(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research"])
    resp = _preview_flip(client, "c1", "a", _iso(2026, 2, 1), ["commercial"], "commercial", _iso(2026, 3, 1))
    assert resp.status_code == 409
    assert "变更标识" in resp.json()["message"]
    # Preview still writes nothing.
    assert _change(client, "c2", "a", _iso(2026, 2, 1), ["commercial"]).status_code == 201


def test_preview_same_version_same_time_conflict_is_409(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research"])
    resp = _preview_flip(client, "c2", "a", _iso(2026, 1, 1), ["review"], "review", _iso(2026, 2, 1))
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]


def test_preview_same_id_same_content_replay_has_no_impact(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research", "review"])
    at = _iso(2026, 5, 1)
    body = _preview_flip(client, "c1", "a", _iso(2026, 1, 1), ["review", "research", "review"], "review", at).json()
    assert body["replayed"] is True
    assert body["impacted_versions"] == []

    # Even a proposal that *would* conflict on time is neutralised when the
    # stored change with that id is itself the one at that time.
    assert _check(client, "a", "review", at).json()["allowed"] is True


def test_preview_naive_times_are_422(client):
    _register(client, "a")
    base = {
        "change_id": "c1",
        "version_id": "a",
        "effective_at": _iso(2026, 1, 1),
        "allowed_uses": ["research"],
        "use": "research",
        "at": _iso(2026, 2, 1),
    }
    bad_effective = {**base, "effective_at": "2026-01-01T00:00:00"}
    assert _preview(client, bad_effective).status_code == 422
    bad_query = {**base, "at": "2026-02-01T00:00:00"}
    assert _preview(client, bad_query).status_code == 422


def test_preview_diamond_descendant_counted_once(client):
    _preview_graph(client)
    at = _iso(2026, 5, 1)
    body = _preview_flip(
        client, "ca2", "a", _iso(2026, 4, 1), ["research"], "review", at
    ).json()
    # Each descendant of a appears exactly once despite the diamond edges.
    assert body["scope"] == ["a", "b", "d2", "d1"]
    assert sorted(entry["version_id"] for entry in body["impacted_versions"]) == ["a", "b", "d1", "d2"]
    d1 = next(entry for entry in body["impacted_versions"] if entry["version_id"] == "d1")
    assert d1["closure"] == ["d1", "b", "d2", "a"]
    # a blocks d1 exactly once.
    assert [bl["version_id"] for bl in d1["after_blockers"]] == ["a"]
    # No writes happened.
    assert client.get("/license-changes/ca2").status_code == 404


def test_preview_fills_no_valid_license_gap(client):
    # A change on a version with no earlier license can remove its block.
    _register(client, "a")
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research"])
    at = _iso(2026, 5, 1)
    assert _check(client, "d1", "research", at).json()["allowed"] is False

    body = _preview_flip(client, "ca1", "a", _iso(2026, 3, 1), ["research"], "research", at).json()
    flips = {entry["version_id"]: entry for entry in body["impacted_versions"]}
    assert set(flips) == {"a", "d1"}
    assert flips["a"]["before_blockers"][0]["reason"] == "no_valid_license"
    assert flips["d1"]["before_blockers"][0]["reason"] == "no_valid_license"
    assert all(entry["after_allowed"] is True for entry in flips.values())


def test_preview_does_not_write_under_concurrent_commits(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    for i in range(6):
        _register(client, f"d{i}", ["a"])
        _change(client, f"cd{i}", f"d{i}", _iso(2026, 1, 1), ["research", "review"])

    # Exercise the snapshot guarantee directly: the one process-wide SQLite
    # connection is shared by all threads (check_same_thread=False), so a
    # preview's reads must be serialised against writes via the db lock and
    # must never persist their hypothetical change.
    service = client.app.state.app_state.service  # type: ignore[attr-defined]
    errors: list[str] = []

    def preview_loop(idx: int) -> None:
        for i in range(25):
            try:
                result = service.preview_change(
                    change_id=f"cp-{idx}-{i}",
                    version_id="a",
                    effective_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                    allowed_uses=["research", "review"],
                    use="review",
                    at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"preview raised: {type(exc).__name__}: {exc}")
                return
            scope = set(result["scope"])
            for entry in result["impacted_versions"]:
                if not set(entry["closure"]) <= scope:
                    errors.append("closure mixed with versions outside snapshot scope")
                    return
                if len(entry["closure"]) != len(set(entry["closure"])):
                    errors.append("diamond descendant counted twice")
                    return

    def submit_loop(idx: int) -> None:
        from datetime import timedelta

        base_day = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for i in range(25):
            # Disjoint effective instants per thread -> no same-time 409.
            effective = base_day + timedelta(days=idx * 31 + i)
            try:
                _, replayed = service.submit_change(
                    change_id=f"w-{idx}-{i}",
                    version_id="a",
                    effective_at=effective,
                    allowed_uses=["research"],
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"submit raised: {type(exc).__name__}: {exc}")
                return
            if replayed:
                errors.append("fresh change unexpectedly replayed")
                return

    import threading

    threads = [threading.Thread(target=preview_loop, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=submit_loop, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # None of the preview-only change ids leaked into the store.
    for idx in range(3):
        for i in range(25):
            assert client.get(f"/license-changes/cp-{idx}-{i}").status_code == 404


# -- permission timeline (interval segmentation) ----------------------------


def _timeline(client, version_id, use, start, end):
    return client.get(
        "/permissions/timeline",
        params={"version_id": version_id, "use": use, "start": start, "end": end},
    )


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_timeline_segments_follow_change_boundaries(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research"])

    resp = _timeline(client, "v1", "review", _iso(2026, 3, 1), _iso(2026, 9, 1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version_id"] == "v1"
    assert body["use"] == "review"
    assert body["closure"] == ["v1"]

    segments = body["segments"]
    assert len(segments) == 2
    first, second = segments

    assert first["start"].startswith("2026-03-01T00:00:00")
    assert first["end"].startswith("2026-06-01T00:00:00")
    assert first["allowed"] is True
    assert [entry["change_id"] for entry in first["basis"]] == ["c1"]
    assert first["blockers"] == []
    assert first["allowed_uses_intersection"] == ["research", "review"]

    assert second["start"].startswith("2026-06-01T00:00:00")
    assert second["end"].startswith("2026-09-01T00:00:00")
    assert second["allowed"] is False
    assert [entry["change_id"] for entry in second["basis"]] == ["c2"]
    assert second["blockers"][0]["version_id"] == "v1"
    assert second["blockers"][0]["reason"] == "use_not_allowed"
    assert second["blockers"][0]["change_id"] == "c2"

    # Continuous, non-overlapping coverage of the whole interval.
    assert segments[0]["end"] == segments[1]["start"]


def test_timeline_half_open_boundaries(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])
    _change(client, "c2", "v1", _iso(2026, 3, 1), ["review"])  # exactly at start
    _change(client, "c3", "v1", _iso(2026, 5, 1), ["research"])  # exactly at end

    body = _timeline(client, "v1", "review", _iso(2026, 3, 1), _iso(2026, 5, 1)).json()
    # The change at the start applies immediately; the one at the end is not
    # counted, so the whole interval is a single allowed segment.
    assert len(body["segments"]) == 1
    segment = body["segments"][0]
    assert segment["allowed"] is True
    assert [entry["change_id"] for entry in segment["basis"]] == ["c2"]
    assert segment["start"].startswith("2026-03-01T00:00:00")
    assert segment["end"].startswith("2026-05-01T00:00:00")

    # The point check at the end instant already sees c3 (half-open difference).
    assert _check(client, "v1", "review", _iso(2026, 5, 1)).json()["allowed"] is False


def test_timeline_no_valid_license_prefix(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 3, 1), ["research"])

    body = _timeline(client, "v1", "research", _iso(2026, 1, 1), _iso(2026, 5, 1)).json()
    assert len(body["segments"]) == 2
    first, second = body["segments"]
    assert first["allowed"] is False
    assert first["basis"] == []
    assert first["blockers"] == [
        {"version_id": "v1", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]
    assert first["allowed_uses_intersection"] == []
    assert second["allowed"] is True
    assert second["allowed_uses_intersection"] == ["research"]


def test_timeline_diamond_closure_counted_once(client):
    # Diamond: a <- b <- d1, a <- d1.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 4, 1), ["research"])
    _register(client, "d1", ["b", "a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    body = _timeline(client, "d1", "research", _iso(2026, 2, 1), _iso(2026, 6, 1)).json()
    assert body["closure"] == ["d1", "b", "a"]  # shared ancestor exactly once
    assert len(body["segments"]) == 2
    first, second = body["segments"]
    # b has no license before 2026-04-01: the whole interval start is denied.
    assert first["allowed"] is False
    assert [(bl["version_id"], bl["reason"]) for bl in first["blockers"]] == [
        ("b", "no_valid_license")
    ]
    # From 04-01 on every version in the closure allows research.
    assert second["allowed"] is True
    assert second["allowed_uses_intersection"] == ["research"]


def test_timeline_reports_all_blockers_per_segment(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "b")
    _change(client, "cb", "b", _iso(2026, 4, 1), ["research", "review"])
    _register(client, "d1", ["a", "b"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    body = _timeline(client, "d1", "review", _iso(2026, 2, 1), _iso(2026, 6, 1)).json()
    assert len(body["segments"]) == 2
    first, second = body["segments"]
    # Before b's first change: a blocks (use_not_allowed) *and* b blocks
    # (no_valid_license) — every blocking version is reported with its reason.
    assert first["allowed"] is False
    reasons = {(bl["version_id"], bl["reason"]) for bl in first["blockers"]}
    assert reasons == {("a", "use_not_allowed"), ("b", "no_valid_license")}
    # After 04-01 only a still blocks.
    assert second["allowed"] is False
    assert [(bl["version_id"], bl["reason"]) for bl in second["blockers"]] == [
        ("a", "use_not_allowed")
    ]


def test_timeline_segments_agree_with_point_checks(client):
    _register(client, "a")
    _change(client, "ca1", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "ca2", "a", _iso(2026, 6, 1), ["research"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 2, 1), ["research", "review"])

    body = _timeline(client, "d1", "review", _iso(2026, 1, 15), _iso(2026, 8, 15)).json()
    segments = body["segments"]
    # Segments tile the whole interval without gaps or overlaps.
    assert segments[0]["start"] == body["start"]
    assert segments[-1]["end"] == body["end"]
    for left, right in zip(segments, segments[1:]):
        assert left["end"] == right["start"]

    # Every sampled instant matches the single-instant check contract.
    samples = [_iso(2026, 1, 20)] + [_iso(2026, month, 10) for month in range(2, 8)]
    for at in samples:
        instant = _parse_ts(at)
        segment = next(
            s
            for s in segments
            if _parse_ts(s["start"]) <= instant < _parse_ts(s["end"])
        )
        point = _check(client, "d1", "review", at).json()
        assert segment["allowed"] == point["allowed"]
        assert segment["blockers"] == point["blockers"]
        assert segment["basis"] == point["basis"]
        assert segment["allowed_uses_intersection"] == point["allowed_uses_intersection"]


def test_timeline_unknown_version_is_404(client):
    resp = _timeline(client, "ghost", "research", _iso(2026, 1, 1), _iso(2026, 2, 1))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_timeline_naive_times_are_422(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    naive_start = client.get(
        "/permissions/timeline",
        params={
            "version_id": "v1",
            "use": "research",
            "start": "2026-01-01T00:00:00",
            "end": _iso(2026, 2, 1),
        },
    )
    assert naive_start.status_code == 422
    assert naive_start.json()["error"] == "invalid_time"

    naive_end = client.get(
        "/permissions/timeline",
        params={
            "version_id": "v1",
            "use": "research",
            "start": _iso(2026, 1, 1),
            "end": "2026-02-01T00:00:00",
        },
    )
    assert naive_end.status_code == 422
    assert naive_end.json()["error"] == "invalid_time"


def test_timeline_inverted_or_empty_interval_is_422(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    inverted = _timeline(client, "v1", "research", _iso(2026, 2, 1), _iso(2026, 1, 1))
    assert inverted.status_code == 422
    assert inverted.json()["error"] == "invalid_interval"

    empty = _timeline(client, "v1", "research", _iso(2026, 1, 1), _iso(2026, 1, 1))
    assert empty.status_code == 422
    assert empty.json()["error"] == "invalid_interval"

    # The same instant written in two offsets is still an empty interval.
    same_instant = client.get(
        "/permissions/timeline",
        params={
            "version_id": "v1",
            "use": "research",
            "start": "2026-01-01T08:00:00+08:00",
            "end": "2026-01-01T00:00:00Z",
        },
    )
    assert same_instant.status_code == 422
    assert same_instant.json()["error"] == "invalid_interval"


def test_timeline_empty_use_is_400(client):
    _register(client, "v1")
    resp = _timeline(client, "v1", "   ", _iso(2026, 1, 1), _iso(2026, 2, 1))
    assert resp.status_code == 400


def test_timeline_on_demo_seed(tmp_path):
    db_path = str(tmp_path / "seed3.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        body = test_client.get(
            "/permissions/timeline",
            params={
                "version_id": "demo-derived-1",
                "use": "review",
                "start": "2026-01-15T00:00:00Z",
                "end": "2026-07-15T00:00:00Z",
            },
        ).json()
        assert body["closure"] == ["demo-derived-1", "demo-root-a", "demo-root-b"]
        segments = body["segments"]
        assert len(segments) == 3
        # Before the derived version's first change: it has no_valid_license,
        # and demo-root-b's license already excludes review.
        assert segments[0]["allowed"] is False
        first_blockers = {(bl["version_id"], bl["reason"]) for bl in segments[0]["blockers"]}
        assert first_blockers == {
            ("demo-derived-1", "no_valid_license"),
            ("demo-root-b", "use_not_allowed"),
        }
        # Then only demo-root-b blocks review ...
        assert [bl["version_id"] for bl in segments[1]["blockers"]] == ["demo-root-b"]
        # ... and after root-a's June change both roots block it.
        assert {bl["version_id"] for bl in segments[2]["blockers"]} == {
            "demo-root-a",
            "demo-root-b",
        }
        assert all(bl["reason"] == "use_not_allowed" for bl in segments[2]["blockers"])

        # research stays allowed from the derived version's first change on;
        # root-a's June change still splits the timeline (basis changed).
        research = test_client.get(
            "/permissions/timeline",
            params={
                "version_id": "demo-derived-1",
                "use": "research",
                "start": "2026-01-15T00:00:00Z",
                "end": "2026-07-15T00:00:00Z",
            },
        ).json()
        assert [s["allowed"] for s in research["segments"]] == [False, True, True]
        assert research["segments"][1]["allowed_uses_intersection"] == ["research"]
        assert research["segments"][2]["allowed_uses_intersection"] == ["research"]
        basis_june = {
            entry["version_id"]: entry["change_id"]
            for entry in research["segments"][2]["basis"]
        }
        assert basis_june["demo-root-a"] == "demo-chg-a-2"
