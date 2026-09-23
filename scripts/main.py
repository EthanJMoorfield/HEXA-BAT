import os
import shutil
import threading
import time
from datetime import datetime

import pandas as pd
from astropy.io import fits
from astropy.table import Table

from .bat_processing_mp import process as bat_process
from .mosaicing import do_mosaic
from .rich_tracking import PipelineProgress
from .setup import setup, setup_proc
from .transfer import cleanup_processed, offload
from .util import (
    check_archive_complete,
    check_mosaic_complete,
    get_img_paths,
    get_obs_paths,
)


def process(config, rev: int, rev_map: pd.DataFrame, progress=None):
    paths, n_processed = get_obs_paths(
        config,
        rev_map,
    )

    if config.CHECK_PROCESSED:
        progress.update_status(
            "to_process",
            f"{len(paths) + n_processed} observations to process, {n_processed} already processed",
        )
    else:
        progress.update_status(
            "to_process", f"{len(paths)} observations to process (check ignored)"
        )

    if not paths:
        progress.update_status("to_process", "All observations already processed")
        time.sleep(1)
        return 0

    bat_process(
        paths=paths,
        config=config,
        progress=progress,
    )

    return len(paths)


def mosaic(config, rev: int, rev_map: pd.DataFrame, progress=None):
    img_paths, total_n_imgs = get_img_paths(
        config,
        rev,
        rev_map,
    )

    progress.update_status(
        "to_process",
        f"Mosaicking {len(img_paths)} sky images ({len(img_paths)}/{total_n_imgs})",
    )

    if not img_paths:
        raise RuntimeError(f"No usable sky images found for revolution {rev}.")

    do_mosaic(
        rev=rev,
        img_paths=img_paths,
        config=config,
        progress=progress,
    )

    return len(img_paths)


def pipeline(config):
    if config.CHECK_ARCHIVES and not config.OFFLOAD:
        raise ValueError("CHECK_ARCHIVES=True requires OFFLOAD=True")

    if config.CHECK_MOSAICED and not config.DO_MOSAICING:
        raise ValueError("CHECK_MOSAICED=True requires DO_MOSAICING=True")

    # setup proc directory
    setup_proc()

    if config.AUTOMATE_ENVIRONMENT:
        setup(os.path.abspath(config.HEADAS))

    # read master list
    with fits.open("files/independent_bat_revs_master.fits") as f:
        obs_map = Table(f[1].data).to_pandas()

    revs = sorted(
        rev
        for rev in obs_map["rev"].unique()
        if config.START_REV <= rev <= config.END_REV
    )

    to_process = []
    already_processed = 0

    with open("./proc/benchmark.txt", "w+") as f:
        f.write(f"Generating revolutions in {config.ENERGY_BANDS} keV\n")
        f.write(f"Processed observations written to: {config.OUTPUT_PROCESSED}\n")
        f.write(f"Mosaicked revolutions written to: {config.OUTPUT_MOSAICED}\n\n")

    # don't re-process revs that were completed in previous runs
    checks_enabled = config.CHECK_ARCHIVES or config.CHECK_MOSAICED

    for rev in revs:
        archive_complete = not config.CHECK_ARCHIVES or check_archive_complete(
            config, rev
        )

        mosaic_complete = not config.CHECK_MOSAICED or check_mosaic_complete(
            config, rev
        )

        if checks_enabled and archive_complete and mosaic_complete:
            already_processed += 1
            continue

        to_process.append(rev)

    # check if any revs to process
    if not len(to_process):
        print("No revs to process. Exiting...")
        return

    progress = PipelineProgress(
        len(to_process),
        do_processing=config.DO_PROCESSING,
        do_mosaicing=config.DO_MOSAICING,
    )
    progress.start()
    time.sleep(1)
    progress.clear_status("initialisation")
    progress.update_status("energy", f"Running pipeline ({config.ENERGY_BANDS} keV)")

    if config.CHECK_MOSAICED or config.CHECK_ARCHIVES:
        progress.update_status(
            "to_process",
            f"{already_processed} revolutions already processed",
        )
    else:
        progress.update_status(
            "to_process",
            f"{already_processed} revolutions already processed (checks ignored)",
        )

    time.sleep(1)

    start_time = time.time()

    for i, rev in enumerate(to_process):
        rev_start_time = time.time()
        rev_start_date = datetime.now()

        progress.clear_status("to_process")
        progress.clear_status("workers")

        rev_map = obs_map[obs_map.rev == int(rev)]

        mosaic_complete = config.CHECK_MOSAICED and check_mosaic_complete(config, rev)

        archive_complete = config.CHECK_ARCHIVES and check_archive_complete(config, rev)

        needs_mosaic = config.DO_MOSAICING and not mosaic_complete
        needs_archive = config.OFFLOAD and not archive_complete

        if rev_map.empty:
            continue

        if config.DO_PROCESSING:
            progress.update_status(
                "heading",
                f"Processing revolution {rev} ({i + 1}/{len(to_process)})",
            )
            n_obs = process(config, rev, rev_map, progress=progress)

        proc_time = time.time()

        n_imgs = 0
        mosaic_time = 0.0
        n_offload = 0
        offload_time = 0.0

        offload_thread = None
        offload_result = {"n": 0, "error": None, "time": 0.0}
        mosaic_error = None

        if needs_archive:

            def _run_offload(rev=rev):
                try:
                    offload_result["n"], offload_result["time"] = offload(rev, config)
                except Exception as e:
                    offload_result["error"] = e

            offload_thread = threading.Thread(target=_run_offload)
            offload_thread.start()

        try:
            if needs_mosaic:
                progress.update_status(
                    "heading",
                    f"Mosaicking revolution {rev} ({i + 1}/{len(to_process)})",
                )

                n_imgs = mosaic(config, rev, rev_map, progress=progress)
                mosaic_time = time.time() - proc_time

        except Exception as e:
            mosaic_error = e

        finally:
            if offload_thread is not None:
                offload_thread.join()

        if offload_result["error"] is not None:
            raise offload_result["error"]

        if mosaic_error is not None:
            raise mosaic_error

        if offload_thread is not None:
            n_offload = offload_result["n"]
            offload_time = offload_result["time"]
            archive_complete = True
        else:
            offload_time = 0.0

        if config.OFFLOAD and archive_complete:
            cleanup_processed(
                rev,
                config.OUTPUT_PROCESSED,
            )

        progress.rev_done()
        progress.refresh()

        end_time = time.time() - rev_start_time

        with open("./proc/benchmark.txt", "a") as f:
            f.write(f"Revolution {rev}\n")
            f.write(f"Started: {rev_start_date}\n")
            if config.DO_PROCESSING:
                f.write(
                    f"Processed {n_obs} observations in {(proc_time - rev_start_time):.2f}s\n"
                )
            if config.OFFLOAD:
                f.write(f"Offloaded {n_offload} observations in {offload_time:.2f}s\n")
            if config.DO_MOSAICING:
                f.write(f"Mosaicked {n_imgs} sky images in {mosaic_time:.2f}s\n")
            f.write(f"Duration: {end_time:.2f}\n")
            f.write(f"Ended: {datetime.now()}\n\n")

    duration = time.time() - start_time

    progress.wipe_status()
    progress.update_status("heading", "Pipeline finished")
    progress.update_status("duration", f"Run took {duration}s")
    progress.update_status(
        "average", f"Average per revolution: {duration / (len(to_process))}s"
    )

    progress.stop()

    if config.DO_PROCESSING and not config.KEEP_PROCESSED:
        shutil.rmtree(config.OUTPUT_PROCESSED)
    if config.DO_MOSAICING and not config.KEEP_MOSAICED:
        shutil.rmtree(config.OUTPUT_MOSAICED)

    return
