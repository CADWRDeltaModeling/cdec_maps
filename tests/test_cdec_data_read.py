from cdec_maps import cdec
from cdec_maps.cdecuimgr import (
    CDECDataUIManager,
    CDECDataReference,
    CDECDataReferenceReader,
    _row_time_range,
)
import holoviews as hv
import pytest
import pandas as pd


def test_read_simple():
    c = cdec.Reader()
    df = c.read_station_data('FPT', '1', 'H', '2020-01-01', '2020-02-01')
    assert not df.empty
    assert len(df) == 745
    assert df['VALUE'].iloc[0] == pytest.approx(104.31)
    assert df['VALUE'].index[0] == pd.Timestamp('2020-01-01')


def test_read_simple_dask():
    c = cdec.Reader()
    df = c.read_station_data('FPT', '1', 'H', '2020-01-01', '2020-02-01')
    assert not df.empty
    assert len(df) == 745
    assert df['VALUE'].iloc[0] == pytest.approx(104.31)
    assert df['VALUE'].index[0] == pd.Timestamp('2020-01-01')


def test_start_end_order():
    c = cdec.Reader()
    df = c.read_station_data('FPT', '1', 'H', '2020-02-01', '2020-01-01')
    assert not df.empty
    assert len(df) == 745
    assert df['VALUE'].iloc[0] == pytest.approx(104.31)
    assert df['VALUE'].index[0] == pd.Timestamp('2020-01-01')


def test_read_station_data_kwargs():
    '''Test by calling positional args with keywords syntax'''
    c = cdec.Reader()
    df = c.read_station_data(station_id='FPT', duration_code='H',
                             sensor_number='1', end='2020-01-01', start='2020-02-01')
    assert not df.empty
    assert len(df) == 745
    assert df['VALUE'].iloc[0] == pytest.approx(104.31)
    assert df['VALUE'].index[0] == pd.Timestamp('2020-01-01')


def test_read_cdec_csv_repairs_extra_empty_field():
    content = """STATION_ID,DURATION,SENSOR_NUMBER,SENSOR_TYPE,DATE TIME,OBS DATE,VALUE,DATA_FLAG,UNITS
DVS,M,2,RAIN,20260701 0000,,,0.00,c,INCHES
DVS,M,2,RAIN,20260801 0000,20260801 0000,0.25,,INCHES
DVS,M,2,RAIN,20260901 0000,20260901 0000,---, ,INCHES
"""

    df = cdec.read_cdec_csv(content)

    assert len(df) == 3
    assert df["OBS DATE"].tolist() == ["", "20260801 0000", "20260901 0000"]
    assert df["VALUE"].iloc[0] == pytest.approx(0.0)
    assert df["VALUE"].iloc[1] == pytest.approx(0.25)
    assert pd.isna(df["VALUE"].iloc[2])


class RecordingCDECReader:
    def __init__(self):
        self.time_range = None

    def read_station_data(self, station_id, sensor_number, duration_code, start, end):
        self.time_range = (start, end)
        index = pd.date_range(start, end, freq="MS")
        return pd.DataFrame({"VALUE": range(len(index))}, index=index)


def test_monthly_reference_loads_a_year_of_context():
    source_reader = RecordingCDECReader()
    reader = CDECDataReferenceReader(source_reader)
    ref = CDECDataReference(
        reader=reader,
        ID="DVS",
        Sensor="RAIN",
        **{
            "Sensor Number": "2",
            "duration_code": "M",
            "Units": "INCHES",
            "Duration": "(monthly)",
        },
    )

    data = ref.getData(time_range=("2026-10-21", "2026-11-20"))

    assert source_reader.time_range == ("2025-12-01", "2026-11-20")
    assert data.index.min() == pd.Timestamp("2025-12-01")
    assert data.index.max() == pd.Timestamp("2026-11-01")


def test_monthly_plot_range_keeps_preceding_observations():
    row = pd.Series({"ID": "DVS", "Duration": "(monthly)"})

    start, end = _row_time_range(row, ("2026-10-21", "2026-11-20"))

    assert start == pd.Timestamp("2025-12-01")
    assert end == pd.Timestamp("2026-11-20")


class RecordingStaleCDECReader(RecordingCDECReader):
    def __init__(self):
        super().__init__()
        self.fallback_args = None

    def read_station_data(self, station_id, sensor_number, duration_code, start, end):
        self.time_range = (start, end)
        index = pd.date_range(start, end, freq="h")
        return pd.DataFrame({"VALUE": float("nan")}, index=index)

    def read_latest_valid_station_data(
        self, station_id, sensor_number, duration_code, before
    ):
        self.fallback_args = (
            station_id, sensor_number, duration_code, before
        )
        index = pd.date_range("2022-11-20", periods=3, freq="h")
        return pd.DataFrame({"VALUE": [1.0, 1.5, 2.0]}, index=index)


def test_stale_hourly_reference_uses_latest_valid_window():
    source_reader = RecordingStaleCDECReader()
    ref = CDECDataReference(
        reader=CDECDataReferenceReader(source_reader),
        ID="07K",
        Sensor="DP2W1",
        **{
            "Sensor Number": "186",
            "duration_code": "H",
            "Units": "FEET",
            "Duration": "(hourly)",
        },
    )

    data = ref.getData(("2026-10-22", "2026-11-21"))

    assert source_reader.fallback_args == ("07K", "186", "H", "2026-11-21")
    assert data.iloc[:, 0].tolist() == [1.0, 1.5, 2.0]
    assert data.attrs["cdec_plot_range"] == (
        pd.Timestamp("2022-11-20 00:00:00"),
        pd.Timestamp("2022-11-20 02:00:00"),
    )


class RecordingResponse:
    text = (
        "STATION_ID,DURATION,SENSOR_NUMBER,SENSOR_TYPE,DATE TIME,"
        "OBS DATE,VALUE,DATA_FLAG,UNITS\n"
        "BLU,M,18,SNOW DP,20260201 0000,20260127 0000,5, ,INCHES\n"
    )

    def raise_for_status(self):
        pass


class RecordingSession:
    def __init__(self):
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return RecordingResponse()

    def close(self):
        pass


def test_monthly_reader_uses_one_bounded_exact_range_request(tmp_path):
    reader = cdec.Reader(dbase_dir=str(tmp_path))
    reader.session = RecordingSession()

    data, _ = reader.read_station_data_using_dask(
        "BLU", "18", "M", "2025-12-01", "2026-11-20"
    )

    assert len(reader.session.requests) == 1
    url, kwargs = reader.session.requests[0]
    assert "Start=12-01-2025" in url
    assert "End=11-20-2026" in url
    assert kwargs["timeout"] == cdec.CDEC_REQUEST_TIMEOUT
    assert data.index.tolist() == [pd.Timestamp("2026-02-01")]


def test_cache_gap_uses_exact_range_instead_of_full_year(tmp_path, monkeypatch):
    reader = cdec.Reader(dbase_dir=str(tmp_path))
    cached = pd.DataFrame(
        {"VALUE": [1.0, 2.0]},
        index=pd.to_datetime(["2026-01-01 00:00", "2026-11-24 00:00"]),
    )
    reader.save_to_db(cached, "C72", "20", "E")
    calls = []

    def fake_fetch(
        station_id,
        sensor_number,
        duration_code,
        start,
        end,
        expand_to_years=True,
    ):
        calls.append(
            (
                station_id,
                sensor_number,
                duration_code,
                start,
                end,
                expand_to_years,
            )
        )
        return pd.DataFrame(
            {"VALUE": [2.0, 3.0]},
            index=pd.to_datetime([start, end]),
        ), (2026, 2027)

    monkeypatch.setattr(reader, "read_station_data_using_dask", fake_fetch)

    data = reader.read_station_data(
        "C72", "20", "E", "2026-11-01", "2026-11-25"
    )

    assert len(calls) == 1
    assert calls[0][-1] is False
    assert pd.Timestamp(calls[0][3]) == pd.Timestamp("2026-11-24 00:00")
    assert calls[0][4] == "2026-11-25"
    assert data.index.min() == pd.Timestamp("2026-11-24 00:00")
    assert data.index.max() == pd.Timestamp("2026-11-25 00:00")


def test_monthly_curve_uses_visible_scatter_points():
    data = pd.DataFrame(
        {"BLU/SNOW DP": [5.0, float("nan"), 0.0]},
        index=pd.date_range("2026-02-01", periods=3, freq="MS"),
    )
    row = pd.Series(
        {
            "ID": "BLU",
            "Sensor": "SNOW DP",
            "Duration": "(monthly)",
            "Description": "SNOW DEPTH",
        }
    )

    curve = CDECDataUIManager.create_curve(None, data, row, "inches")

    assert isinstance(curve, hv.Scatter)
