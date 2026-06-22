from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ccatv.storage import PersistenceStore, initialize_database


def test_recording_lifecycle_roundtrip(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        created = store.create_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            state="scheduled",
            started_at_utc="2026-05-23T10:00:00Z",
        )

        updated = store.update_recording_state(
            created.id,
            state="completed",
            ended_at_utc="2026-05-23T11:00:00Z",
        )
        listed = store.list_recordings()

        assert created.state == "scheduled"
        assert updated.state == "completed"
        assert updated.ended_at_utc == "2026-05-23T11:00:00Z"
        assert listed == [updated]
    finally:
        connection.close()


def test_scheduler_job_lifecycle_roundtrip(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        created = store.create_scheduler_job(
            channel_name="BBC ONE HD",
            start_at_utc="2026-05-23T12:00:00Z",
            duration_seconds=3600,
            state="pending",
        )

        updated = store.update_scheduler_job_state(created.id, state="running")
        listed = store.list_scheduler_jobs()

        assert created.state == "pending"
        assert updated.state == "running"
        assert listed == [updated]
    finally:
        connection.close()


def test_update_missing_rows_raises_value_error(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        with pytest.raises(ValueError, match="recording id not found"):
            store.update_recording_state(999, state="failed")
        with pytest.raises(ValueError, match="scheduler job id not found"):
            store.update_scheduler_job_state(999, state="failed")
    finally:
        connection.close()


def test_set_and_get_dvbstreamer_service_name(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        connection.execute(
            """
            INSERT INTO epg_channels(
                source,
                source_channel_id,
                display_name,
                callsign,
                logical_channel_number
            ) VALUES(?, ?, ?, ?, ?)
            """,
            ("schedules_direct", "100", "Quest", "QUEST", "12"),
        )
        connection.commit()

        updated = store.set_dvbstreamer_service_name("Quest", "QUEST")

        assert updated == 1
        assert store.get_dvbstreamer_service_name("Quest") == "QUEST"
    finally:
        connection.close()


def test_set_dvbstreamer_service_name_can_clear_mapping(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        connection.execute(
            """
            INSERT INTO epg_channels(
                source,
                source_channel_id,
                display_name,
                callsign,
                logical_channel_number,
                dvbstreamer_service_name
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            ("schedules_direct", "101", "BBC One East", "BBC1E", "1", "BBC ONE East"),
        )
        connection.commit()

        updated = store.set_dvbstreamer_service_name("BBC One East", None)

        assert updated == 1
        assert store.get_dvbstreamer_service_name("BBC One East") is None
    finally:
        connection.close()


def test_set_and_get_favorite_channel(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        connection.execute(
            """
            INSERT INTO epg_channels(
                source,
                source_channel_id,
                display_name,
                callsign,
                logical_channel_number
            ) VALUES(?, ?, ?, ?, ?)
            """,
            ("schedules_direct", "120", "BBC News", "BBCNEWS", "231"),
        )
        connection.commit()

        assert store.get_favorite_channel("BBC News") is False

        updated = store.set_favorite_channel("BBC News", True)

        assert updated == 1
        assert store.get_favorite_channel("BBC News") is True
    finally:
        connection.close()


def test_set_favorite_channel_returns_zero_when_channel_unknown(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        updated = store.set_favorite_channel("Unknown", True)

        assert updated == 0
        assert store.get_favorite_channel("Unknown") is False
    finally:
        connection.close()


def test_set_favorite_channel_updates_case_variants(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        connection.executemany(
            """
            INSERT INTO epg_channels(
                source,
                source_channel_id,
                display_name,
                callsign,
                logical_channel_number,
                favorite_channel
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            [
                ("dvbstreamer_ota", "200", "BBC FOUR HD", "BBC4HD", "106", 0),
                ("schedules_direct", "100", "BBC Four HD", "BBC4HD", "106", 1),
            ],
        )
        connection.commit()

        assert store.get_favorite_channel("BBC FOUR HD") is True

        updated = store.set_favorite_channel("BBC FOUR HD", False)

        assert updated == 2
        assert store.get_favorite_channel("BBC FOUR HD") is False
    finally:
        connection.close()


def test_channel_lineup_override_roundtrip(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        connection.execute(
            """
            INSERT INTO epg_channels(
                source,
                source_channel_id,
                display_name,
                callsign,
                logical_channel_number
            ) VALUES(?, ?, ?, ?, ?)
            """,
            ("dvbstreamer_ota", "200", "ITV1 HD", "ITV1", "3"),
        )
        connection.commit()

        result = store.set_channel_lineup_override(
            epg_channel_name="ITV1 HD",
            broadcaster_name="ITV1",
            schedules_direct_name="ITV1 HD (Meridian, Anglia)",
            guide_name="ITV1",
            guide_logical_channel_number="3",
        )

        assert result == {"action": "saved", "updatedRows": 1}
        overrides = store.list_channel_lineup_overrides()
        assert "itv1 hd" in overrides
        assert overrides["itv1 hd"] == {
            "epgChannelName": "ITV1 HD",
            "broadcasterName": "ITV1",
            "schedulesDirectName": "ITV1 HD (Meridian, Anglia)",
            "guideName": "ITV1",
            "guideLogicalChannelNumber": "3",
        }
    finally:
        connection.close()


def test_channel_lineup_override_can_clear(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        store.set_channel_lineup_override(
            epg_channel_name="BBC FOUR HD",
            broadcaster_name="BBC FOUR HD",
            schedules_direct_name="BBC Four HD",
            guide_name="BBC4",
            guide_logical_channel_number="9",
        )

        result = store.set_channel_lineup_override(
            epg_channel_name="BBC FOUR HD",
            broadcaster_name=None,
            schedules_direct_name=None,
            guide_name=None,
            guide_logical_channel_number=None,
        )

        assert result == {"action": "cleared", "updatedRows": 1}
        overrides = store.list_channel_lineup_overrides()
        assert "bbc four hd" not in overrides
    finally:
        connection.close()


def test_series_recording_subscription_roundtrip(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        assert store.list_series_recording_subscriptions() == []

        store.set_series_recording_subscription("example.org/series-1", True)
        assert store.list_series_recording_subscriptions() == ["example.org/series-1"]

        store.set_series_recording_subscription("example.org/series-1", False)
        assert store.list_series_recording_subscriptions() == []
    finally:
        connection.close()


def test_recorded_content_ref_history_roundtrip(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        assert store.has_recorded_content_ref("example.org/content-1") is False

        store.mark_recorded_content_ref(
            content_ref="example.org/content-1",
            series_ref="example.org/series-1",
            title="Episode 1",
            recording_id=123,
        )

        assert store.has_recorded_content_ref("example.org/content-1") is True
    finally:
        connection.close()


def test_delete_recording_removes_row_and_returns_deleted_record(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "ccatv.sqlite3")
    store = PersistenceStore(connection=connection)
    try:
        created = store.create_recording(
            channel_name="BBC TWO HD",
            output_path="/tmp/bbc2.ts",
            state="ready",
        )

        deleted = store.delete_recording(created.id)

        assert deleted.id == created.id
        assert deleted.output_path == "/tmp/bbc2.ts"
        assert store.get_recording(created.id) is None
    finally:
        connection.close()


def test_store_serializes_shared_connection_access() -> None:
    class _FakeCursor:
        rowcount = 1
        lastrowid = 1

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _FakeConnection:
        def __init__(self) -> None:
            self._gate = threading.Event()
            self._entered = threading.Event()
            self._lock = threading.Lock()
            self.active_calls = 0
            self.overlap_detected = False

        def execute(self, _query: str, _params=()):
            with self._lock:
                if self.active_calls > 0:
                    self.overlap_detected = True
                self.active_calls += 1
                self._entered.set()
            try:
                self._gate.wait(timeout=1.0)
                return _FakeCursor()
            finally:
                with self._lock:
                    self.active_calls -= 1

        def commit(self) -> None:
            return None

    connection = _FakeConnection()
    store = PersistenceStore(connection=connection)
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            store.list_scheduler_jobs()
        except Exception as exc:  # pragma: no cover - defensive capture for thread join
            errors.append(exc)

    first = threading.Thread(target=_worker)
    second = threading.Thread(target=_worker)

    first.start()
    assert connection._entered.wait(timeout=1.0)
    second.start()
    connection._gate.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert errors == []
    assert connection.overlap_detected is False
