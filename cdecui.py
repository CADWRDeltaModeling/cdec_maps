"""CDEC Map Explorer — pn.serve entry-point with session persistence.

Launch with:
    python cdecui.py [--port 80] [--no-show]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from functools import lru_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manager factory — called once per unique user session
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _shared_station_catalog():
    """Read the immutable saved station catalog once per server process."""
    from cdec_maps import cdec

    reader = cdec.Reader()
    stations, sensor_list, stations_meta_info = reader.read_saved_stations_info()
    return (
        stations.copy(deep=True),
        sensor_list.copy(deep=True),
        stations_meta_info.copy(deep=True),
    )


def build_cdec_manager():
    """Build and return a fresh :class:`~cdec_maps.cdecuimgr.CDECDataUIManager`.

    All heavy I/O (reading station metadata) happens here so that it only
    runs once per user (the in-memory registry serves returning users without
    re-running this function).
    """
    import pandas as pd
    import geopandas as gpd
    from cdec_maps import cdec
    from cdec_maps.cdecuimgr import CDECDataUIManager

    t0 = time.perf_counter()
    logger.info("build_cdec_manager: reading saved station info …")
    reader = cdec.Reader()
    stations, sensor_list, stations_meta_info = _shared_station_catalog()
    stations = stations.copy(deep=True)
    sensor_list = sensor_list.copy(deep=True)
    stations_meta_info = stations_meta_info.copy(deep=True)
    logger.info(
        "build_cdec_manager: loaded %d station-sensor rows, %d sensor types  (%.1fs)",
        len(stations_meta_info), len(sensor_list), time.perf_counter() - t0,
    )

    # Restrict to California / adjacent states lat-lon box
    stations_meta_info = stations_meta_info[
        (stations_meta_info["Latitude"] >= 30)
        & (stations_meta_info["Latitude"] <= 45)
        & (stations_meta_info["Longitude"] >= -125)
        & (stations_meta_info["Longitude"] <= -110)
    ]

    displayed_sensor_list = sensor_list["Sensor"].unique()
    stations_meta_info = stations_meta_info[
        stations_meta_info["Sensor"].isin(displayed_sensor_list)
    ]
    logger.info(
        "build_cdec_manager: %d rows after geo+sensor filter  (%.1fs)",
        len(stations_meta_info), time.perf_counter() - t0,
    )

    # Parse date-availability column into typed Start Date / End Date columns
    stations_meta_info[["Start Date", "End Date"]] = stations_meta_info[
        "Data Available"
    ].str.split(" to ", expand=True)
    stations_meta_info["End Date"] = stations_meta_info["End Date"].replace(
        "present", pd.to_datetime("today").strftime("%m/%d/%Y")
    )
    stations_meta_info["Start Date"] = pd.to_datetime(
        stations_meta_info["Start Date"], errors="coerce"
    )
    stations_meta_info["End Date"] = pd.to_datetime(
        stations_meta_info["End Date"], errors="coerce"
    )

    stations_meta_info["Source"] = "CDEC"
    stations_meta_info["Sensor Number"] = stations_meta_info["Sensor Number"].astype(str)

    if all(col in stations.columns for col in ["Latitude", "Longitude"]):
        geodf = gpd.GeoDataFrame(
            stations_meta_info,
            geometry=gpd.points_from_xy(
                stations_meta_info["Longitude"],
                stations_meta_info["Latitude"],
                crs="EPSG:4326",
            ),
        )
    else:
        geodf = stations_meta_info

    logger.info(
        "build_cdec_manager: constructing CDECDataUIManager with %d rows …",
        len(geodf),
    )
    mgr = CDECDataUIManager(geodf, reader)
    logger.info(
        "build_cdec_manager: done — catalog has %d refs  (total %.1fs)",
        len(mgr._dvue_catalog._references), time.perf_counter() - t0,
    )
    return mgr


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main():
    import cartopy.crs as ccrs
    import panel as pn
    import panel_material_ui as pmui
    from dvue.dataui import DataUI
    from dvue.session_persistence import (
        install_session_handler,
        SessionManager,
    )

    parser = argparse.ArgumentParser(
        description="CDEC Map Explorer — Panel server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--port", type=int, default=80,
        help="TCP port to serve on",
    )
    parser.add_argument(
        "--address", default="0.0.0.0",
        help="Network address to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--no-show", action="store_true",
        help="Do not open a browser window automatically",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Starting CDEC Map Explorer on %s:%s", args.address, args.port)

    pn.extension(
        "gridstack", "tabulator", "codeeditor",
        notifications=True, design="material",
    )

    # Pre-import heavy UI modules before pn.serve() so their module-level
    # hv/pn extension() calls happen outside any live Bokeh session.
    from cdec_maps.cdecuimgr import CDECDataUIManager  # noqa: F401

    install_session_handler(cookie_name="cdec_user_id")

    session_mgr = SessionManager(
        cookie_name="cdec_user_id",
        persist=False,
    )

    crs_cartopy = ccrs.PlateCarree()

    def make_app():
        user_id = session_mgr.current_user_id
        reg_key = session_mgr.make_reg_key(user_id, "cdec")
        entry = session_mgr.get_entry(reg_key)

        # Serve the outer Page immediately with a loading spinner.  The heavy
        # build runs inside pn.state.onload so the browser gets the page shell
        # before any blocking I/O happens.
        loading_status = pn.pane.Markdown(
            "**Loading saved CDEC station and sensor metadata...**",
            sizing_mode="stretch_width",
            align="center",
        )
        main_panel = pn.Column(
            pn.indicators.LoadingSpinner(value=True, color="primary", size=50),
            loading_status,
            sizing_mode="stretch_both",
            align="center",
        )
        sidebar_panel = pn.Column(
            pn.indicators.LoadingSpinner(value=True, color="primary", size=50),
        )

        if entry:
            # Registry hit: reuse the already-built page and DataUI;
            # only re-register per-Document hooks (they bind to curdoc).
            logger.info("make_app: registry hit for user %s — reusing session", user_id[:8] if user_id else "anon")
            template = entry["template"]
            ui = entry["ui"]
            stored_main = entry.get("main_panel")
            if stored_main is not None:
                stored_main.loading = True

            def _reattach():
                try:
                    ui.setup_location_sync()
                    ui.setup_url_sync()
                finally:
                    if stored_main is not None:
                        stored_main.loading = False

            pn.state.onload(_reattach)
            template.servable()
            return

        logger.info("make_app: new session for user %s — building manager …", user_id[:8] if user_id else "anon")
        template = pmui.Page(
            title="CDEC Map Explorer",
            sidebar=[sidebar_panel],
            main=[main_panel],
            sidebar_width=320,
            sidebar_open=False,
            sidebar_variant="auto",
        )

        async def _load_app():
            try:
                await asyncio.sleep(0)
                loading_status.object = (
                    "**Preparing the CDEC catalog, map, and time-range controls...**"
                )
                await asyncio.sleep(0)
                mgr = build_cdec_manager()
                loading_status.object = (
                    "**Building the station table, map, and plot controls...**"
                )
                await asyncio.sleep(0)
                ui = DataUI(mgr, crs=crs_cartopy, station_id_column="ID")
                ui_page = ui.create_view(title="CDEC Map Explorer")

                # Move the built content into the served Page.  The About /
                # Disclaimer dialog lives in ui_page.main, so it moves along
                # with it and the header buttons keep working.  Empty the inner
                # page so the same models are not owned by two components.
                sidebar_panel.objects = list(ui_page.sidebar)
                main_panel.objects = list(ui_page.main)
                template.header = list(ui_page.header)
                ui_page.sidebar, ui_page.main, ui_page.header = [], [], []

                if user_id:
                    session_mgr.set_entry(reg_key, {
                        "template": template, "ui": ui,
                        "main_panel": main_panel,
                    })

            except Exception:
                import traceback
                logger.error("_load_app failed:\n%s", traceback.format_exc())
                main_panel.objects = [
                    pn.pane.Markdown("## Error loading application\n\nCheck the server logs.")
                ]

        pn.state.onload(_load_app)
        template.servable()

    pn.serve(
        {"cdec-map-explorer": make_app},
        port=args.port,
        address=args.address,
        allow_websocket_origin=["*"],
        show=not args.no_show,
        keep_alive=30000,
        # Bounded (not Bokeh's 15s default, but not unbounded either): every
        # cookie-less GET / (Azure Always-On ping, health check) spins up a
        # full heavy CDECDataUIManager session via SessionManager that never
        # reconnects. A huge value here let those orphaned sessions
        # accumulate forever, leaking memory until the container was
        # OOM-restarted -- killing every real user's connection at once.
        unused_session_lifetime_milliseconds=1_800_000,
        # Bokeh's default (300s) expires the reconnect token after a few
        # minutes idle; the browser's automatic reconnect then fails
        # permanently with "Token is expired" until the page is reloaded.
        session_token_expiration=2_592_000,
    )


if __name__ == "__main__":
    main()
