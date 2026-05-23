from __future__ import annotations

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
