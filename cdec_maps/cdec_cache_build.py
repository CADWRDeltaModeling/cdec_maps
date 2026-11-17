import logging

import click
from cdec_maps import build_job, cdec
import pandas as pd
import tqdm

logger = logging.getLogger(__name__)


def download_row(row, reader):
    unit = row["Units"]
    station_id = row["ID"]
    sensor_desc = row["Sensor"]
    sensor_number = row["Sensor Number"]
    duration_code = cdec.get_duration_code(row["Duration"])
    start = row["Start Date"]
    end = row["End Date"]
    df = reader.read_station_data(station_id, sensor_number, duration_code, start, end)
    return df, unit, sensor_desc


def download_all_stations_info(reader=None):
    reader = reader or cdec.Reader()
    reader.save_all_stations_info()


def download_all_stations_data(reader=None, heartbeat=None):
    """Download every station/sensor series into the Parquet cache (gap-filling).

    Returns ``{"ok", "failed", "failed_items"}``; *heartbeat* (if given) is called
    after each series so a build lock stays fresh.
    """
    reader = reader or cdec.Reader()
    stations, sensor_list, stations_meta_info = reader.read_saved_stations_info()
    # Assuming stations_meta_info is your dataframe
    stations_meta_info[["Start Date", "End Date"]] = stations_meta_info[
        "Data Available"
    ].str.split(" to ", expand=True)
    # Replace 'present' with current date and convert 'End Date' to datetime
    stations_meta_info["End Date"] = stations_meta_info["End Date"].replace(
        "present", pd.to_datetime("today").strftime("%m/%d/%Y")
    )
    # Convert 'Start Date' to datetime
    stations_meta_info["Start Date"] = pd.to_datetime(
        stations_meta_info["Start Date"], errors="coerce"
    )
    stations_meta_info["End Date"] = pd.to_datetime(
        stations_meta_info["End Date"], errors="coerce"
    )

    n_ok = n_fail = 0
    failed_items = []
    for index in tqdm.tqdm(range(len(stations_meta_info)), mininterval=30):
        row = stations_meta_info.iloc[index]
        try:
            download_row(row, reader)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            failed_items.append(
                f"{row.get('ID')}/{row.get('Sensor Number')}/{row.get('Duration')}"
            )
            logger.warning("Failed to download data for row at %s: %s", index, e)
        if heartbeat:
            heartbeat()
    logger.info("download_all_stations_data: %d ok, %d failed", n_ok, n_fail)
    return {"ok": n_ok, "failed": n_fail, "failed_items": failed_items}


def run_build(dbase_dir="cdec_db"):
    """Refresh the station tables, then gap-fill every series, under the build lock."""
    cdec.clear()  # drop in-process LRU caches so the station tables are re-read
    reader = cdec.Reader(dbase_dir=dbase_dir)

    def build(heartbeat):
        download_all_stations_info(reader)
        heartbeat()
        return download_all_stations_data(reader, heartbeat)

    return build_job.run_locked_build(dbase_dir, build, name="cdec")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--dbase-dir", default="cdec_db", show_default=True, help="Cache directory.")
def main(dbase_dir):
    """Build the CDEC cache (station tables + series download)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-28s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    run_build(dbase_dir)


if __name__ == "__main__":
    main()
