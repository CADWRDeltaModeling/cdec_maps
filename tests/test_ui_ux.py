from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import panel_material_ui as pmui
import param

from cdec_maps.cdecuimgr import CDECDataUIManager


def _catalog():
    return gpd.GeoDataFrame(
        {
            "ID": ["FPT", "SAC"],
            "Station": ["Freeport", "Sacramento"],
            "County": ["Sacramento", "Sacramento"],
            "River Basin": ["Sacramento", "Sacramento"],
            "Operator": ["USGS", "DWR"],
            "Start Date": [pd.Timestamp("2000-01-01")] * 2,
            "End Date": [
                pd.Timestamp("2024-12-31"),
                pd.Timestamp("2039-12-31"),
            ],
            "Sensor": ["FLOW", "STAGE"],
            "Sensor Number": ["1", "2"],
            "Duration": ["(hourly)", "(hourly)"],
            "Units": ["CFS", "FEET"],
            "Description": ["Flow", "Stage"],
        },
        geometry=gpd.points_from_xy([-121.5, -121.6], [38.0, 38.2]),
        crs="EPSG:4326",
    )


def test_sensor_filter_is_touch_friendly():
    manager = CDECDataUIManager(_catalog(), SimpleNamespace())
    sensor_widget = manager.get_widgets()["Data"][0]

    assert isinstance(sensor_widget, pmui.MultiChoice)
    assert sensor_widget.label == "Sensors"


def test_default_window_uses_recent_non_future_catalog_year():
    manager = CDECDataUIManager(_catalog(), SimpleNamespace())

    start, end = manager.get_time_range(manager.get_data_catalog())

    assert (start.year, end.year) == (2023, 2024)


def test_sensor_change_filters_dataui_query():
    manager = CDECDataUIManager(_catalog(), SimpleNamespace())
    manager._dataui = SimpleNamespace(query="")

    manager.sensor_selections = ["FLOW"]

    assert manager._dataui.query == "Sensor in ['FLOW']"
