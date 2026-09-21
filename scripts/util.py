import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from astropy.io import fits
from astropy.table import Table


def get_obs_paths(config, rev_map: pd.DataFrame):
    paths = []
    already_processed = 0

    expected_marker = get_proc_marker(
        config.ENERGY_BANDS,
        config.DETTHRESH,
        config.DETTHRESH2,
        config.EXPOTHRESH,
        config.RATEMINTHRESH,
        config.INCATALOG,
        config.CLEANSNR,
        config.NOISE_CORRECTION,
        config.DECLUTTER,
        config.COMPRESS,
    )

    for _, row in rev_map.iterrows():
        path = os.path.join(
            config.ARCHIVE_ROOT,
            row["month"],
            row["obs"],
        )

        if config.CHECK_PROCESSED:
            processed_path = os.path.join(
                config.OUTPUT_PROCESSED,
                row["month"],
                row["obs"],
            )

            marker_path = os.path.join(
                processed_path,
                "proc_completion.json",
            )

            if os.path.isdir(processed_path) and check_marker(
                marker_path,
                "proc",
                expected_marker,
            ):
                already_processed += 1
                continue

        paths.append(path)

    return paths, already_processed


def get_img_mjd(img_path: str) -> float:
    with fits.open(img_path) as hdul:
        header = hdul[0].header

        if "MJDREFI" in header:
            MJD_REF = header["MJDREFI"]
            if "MJDREFF" in header:
                MJD_REF += header["MJDREFF"]
        else:
            MJD_REF = header["MJDREF"]

        start_mjd = MJD_REF + header["TSTART"] / 86400.0
        stop_mjd = MJD_REF + header["TSTOP"] / 86400.0

        mid_mjd = (start_mjd + stop_mjd) / 2.0

    return mid_mjd


def get_img_paths(config, rev: int, rev_map: pd.DataFrame):
    with fits.open("files/integralrevs.fits") as hdul:
        rev_table = Table(hdul[1].data).to_pandas()
        rev_table = rev_table.set_index("rev")

    total_imgs = 0

    rev_images = []
    for i, row in rev_map.iterrows():
        obs_path = os.path.join(config.OUTPUT_PROCESSED, row["month"], row["obs"])
        search_path = os.path.join(obs_path, "point_*", "*_2.img")
        if config.COMPRESS:
            search_path += ".gz"
        img_paths = glob.glob(search_path)

        total_imgs += len(img_paths)

        for img_path in img_paths:
            # check if batsurvey returned success for this pointing
            if not pointing_succeeded(img_path):
                continue

            img_mjd = get_img_mjd(img_path)

            min_mjd = rev_table.loc[rev, "mjd"]
            try:
                max_mjd = rev_table.loc[rev + 1, "mjd"]
            except KeyError:
                rev_durations = np.diff(rev_table["mjd"])
                typical_duration = np.median(rev_durations)
                max_mjd = min_mjd + typical_duration

            if min_mjd <= img_mjd < max_mjd:
                rev_images.append(img_path)

    return rev_images, total_imgs


def pointing_succeeded(img_path):
    point_dir = os.path.dirname(img_path)

    status_paths = glob.glob(os.path.join(point_dir, "*_status.txt"))

    if len(status_paths) != 1:
        raise RuntimeError(
            f"Expected exactly one status file in {point_dir}, found {len(status_paths)}."
        )

    with open(status_paths[0], "r") as f:
        status = f.read()

    return 'status="SUCCESS"' in status


def write_marker(path, product_type, config):
    path = Path(path)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")

    marker = {
        "product_type": product_type,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "config": config,
    }

    # write to temporary file first so that a crash doesn't leave a valid marker
    with open(tmp_path, "w") as f:
        json.dump(marker, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)


def check_marker(path, product_type, expected_config):
    path = Path(path)

    if not path.is_file():
        return False

    try:
        with open(path) as f:
            marker = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    if marker.get("product_type") != product_type:
        return False

    if marker.get("config") != expected_config:
        return False

    return True


def check_mosaic_complete(config, rev):
    expected_marker = get_mosaic_marker(
        config.ENERGY_BANDS,
        config.PCODING_FILTER,
        config.PCODING_THRESH,
        config.DO_WAVELET,
        config.COMPRESS,
    )

    if not config.LABEL:
        base = os.path.join(config.OUTPUT_MOSAICED, f"bat_{str(rev).zfill(4)}")
    else:
        base = os.path.join(
            config.OUTPUT_MOSAICED, f"bat_{str(rev).zfill(4)}_{config.LABEL}"
        )

    extension = "fits.gz" if config.COMPRESS else "fits"

    required_files = [
        f"{base}_flux.{extension}",
        f"{base}_error.{extension}",
        f"{base}_sig.{extension}",
        f"{base}_expo.{extension}",
    ]

    if not all(os.path.isfile(p) for p in required_files):
        return False

    return check_marker(
        os.path.join(
            config.OUTPUT_MOSAICED, f"bat_{str(rev).zfill(4)}_completion.json"
        ),
        "mosaic",
        expected_marker,
    )


def check_archive_complete(config, rev):
    return os.path.isfile(
        os.path.join(
            config.OFFLOAD_DIR,
            f"rev_{rev:04d}.tar",
        )
    )


def get_proc_marker(
    band,
    detthresh,
    detthresh2,
    expothresh,
    rateminthresh,
    incatalog,
    cleansnr,
    noise_correction,
    declutter,
    compress,
):
    return locals()


def get_mosaic_marker(band, pcoding_filter, pcoding_thresh, wavelet, compress):
    return locals()
