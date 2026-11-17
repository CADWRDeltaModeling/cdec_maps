"""Tests for out-of-band cache build support (atomic writes, lock, status)."""

import os
import time

import pandas as pd
import pytest

from cdec_maps import build_job, cdec, cdec_cache_build


@pytest.fixture()
def dbase(tmp_path):
    return str(tmp_path / "cdec_db")


class TestAtomicWrites:
    def test_parquet_roundtrip_leaves_no_temp_files(self, tmp_path):
        path = str(tmp_path / "FPT__1__H.prq")
        build_job.atomic_to_parquet(pd.DataFrame({"VALUE": [1.0, 2.0]}), path)

        assert pd.read_parquet(path)["VALUE"].tolist() == [1.0, 2.0]
        assert os.listdir(tmp_path) == ["FPT__1__H.prq"]

    def test_failed_write_keeps_previous_file(self, tmp_path):
        path = str(tmp_path / "stations.csv")
        build_job.atomic_to_csv(pd.DataFrame({"a": [1]}), path)

        def broken_write(tmp):
            with open(tmp, "w") as f:
                f.write("partial")
            raise OSError("disk full")

        with pytest.raises(OSError):
            build_job._atomic_write(path, broken_write)

        assert pd.read_csv(path)["a"].tolist() == [1]
        assert os.listdir(tmp_path) == ["stations.csv"]

    def test_reader_save_to_db_is_atomic(self, dbase):
        reader = cdec.Reader(dbase_dir=dbase)
        reader.save_to_db(pd.DataFrame({"VALUE": [3.0]}), "FPT", "1", "H")

        assert reader.load_from_db("FPT", "1", "H")["VALUE"].tolist() == [3.0]
        assert [f for f in os.listdir(dbase) if f.endswith(".tmp")] == []


class TestBuildLock:
    def test_second_build_is_refused_until_released(self, dbase):
        os.makedirs(dbase)
        with build_job.BuildLock(dbase):
            with pytest.raises(build_job.BuildLockedError):
                build_job.BuildLock(dbase).acquire()
        build_job.BuildLock(dbase).acquire().release()

    def test_stale_lock_is_taken_over(self, dbase):
        os.makedirs(dbase)
        lock_path = os.path.join(dbase, build_job.LOCK_NAME)
        open(lock_path, "w").close()
        old = time.time() - build_job.STALE_AFTER_SECONDS - 60
        os.utime(lock_path, (old, old))

        with build_job.BuildLock(dbase):
            assert os.path.exists(lock_path)

    def test_heartbeat_keeps_lock_fresh(self, dbase):
        os.makedirs(dbase)
        with build_job.BuildLock(dbase) as lock:
            old = time.time() - build_job.STALE_AFTER_SECONDS - 60
            os.utime(lock.path, (old, old))
            lock.heartbeat()
            with pytest.raises(build_job.BuildLockedError):
                build_job.BuildLock(dbase).acquire()


class TestRunLockedBuild:
    def test_success_records_status_and_releases_lock(self, dbase):
        build_job.run_locked_build(
            dbase, lambda hb: {"ok": 9, "failed": 1, "failed_items": ["x"]}, name="cdec"
        )

        status = build_job.read_status(dbase)
        assert status["state"] == "success"
        assert (status["ok"], status["failed"], status["failed_items"]) == (9, 1, ["x"])
        assert not os.path.exists(os.path.join(dbase, build_job.LOCK_NAME))

    def test_exception_is_recorded_and_reraised(self, dbase):
        def boom(heartbeat):
            raise ValueError("CDEC down")

        with pytest.raises(ValueError):
            build_job.run_locked_build(dbase, boom)

        status = build_job.read_status(dbase)
        assert status["state"] == "failed" and "CDEC down" in status["error"]
        assert not os.path.exists(os.path.join(dbase, build_job.LOCK_NAME))

    def test_mostly_failing_build_is_flagged(self, dbase):
        with pytest.raises(RuntimeError):
            build_job.run_locked_build(dbase, lambda hb: {"ok": 1, "failed": 9})

        assert build_job.read_status(dbase)["state"] == "failed"

    def test_overlapping_build_is_refused(self, dbase):
        def inner(heartbeat):
            with pytest.raises(build_job.BuildLockedError):
                build_job.run_locked_build(dbase, lambda hb: {})
            return {"ok": 1}

        build_job.run_locked_build(dbase, inner)
        # the refused build must not have overwritten the running build's status
        assert build_job.read_status(dbase)["state"] == "success"


class TestDownloadAllStationsData:
    class FakeReader:
        def __init__(self):
            self.calls = []

        def read_saved_stations_info(self):
            meta = pd.DataFrame(
                {
                    "ID": ["FPT", "BAD", "SAC"],
                    "Sensor": ["flow"] * 3,
                    "Sensor Number": [20, 20, 20],
                    "Duration": ["(hourly)"] * 3,
                    "Units": ["CFS"] * 3,
                    "Data Available": ["01/01/2020 to present"] * 3,
                }
            )
            return None, None, meta

        def read_station_data(self, station_id, sensor_number, duration_code, start, end):
            self.calls.append(station_id)
            if station_id == "BAD":
                raise RuntimeError("Failed to retrieve any data")
            return pd.DataFrame()

    def test_counts_failures_and_heartbeats(self):
        beats = []

        result = cdec_cache_build.download_all_stations_data(
            self.FakeReader(), heartbeat=lambda: beats.append(1)
        )

        assert (result["ok"], result["failed"]) == (2, 1)
        assert result["failed_items"] == ["BAD/20/(hourly)"]
        assert len(beats) == 3
