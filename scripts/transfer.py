import glob
import os
import shutil
import tarfile
import time
from datetime import datetime

from astropy.io import fits
from astropy.table import Table

from .util import check_marker, get_proc_marker

with fits.open("files/independent_bat_revs_master.fits") as f:
    obs_map = Table(f[1].data).to_pandas()

obs_final_rev = obs_map.groupby("obs")["rev"].max().to_dict()


def _log(msg):
    mode = "a" if os.path.exists("./proc/offload_log.txt") else "w+"

    with open("./proc/offload_log.txt", mode) as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")


def get_rev_paths(rev, src, config):
    rows = obs_map[obs_map["rev"] == rev][["month", "obs"]].drop_duplicates()

    paths = []

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

    for _, row in rows.iterrows():
        path = os.path.join(src, row["month"], row["obs"])

        if not os.path.isdir(path):
            raise RuntimeError(f"Processed observation missing: {path}")

        marker_path = os.path.join(path, "proc_completion.json")

        if not check_marker(marker_path, "proc", expected_marker):
            raise RuntimeError(
                f"Processed observation incomplete or incompatible: {path}"
            )

        paths.append(path)

    return paths


def archive(paths, src, dst):
    # write to partial file first, if something goes wrong this is left behind and removed if rerun
    partial = dst + ".partial"

    if os.path.exists(partial):
        os.remove(partial)

    try:
        with tarfile.open(partial, "w") as tar:
            for path in paths:
                arcname = os.path.relpath(path, src)
                tar.add(path, arcname=arcname)

        os.replace(partial, dst)

    except Exception:
        if os.path.exists(partial):
            os.remove(partial)
        raise


def offload(rev, config):
    start = time.time()

    src = config.OUTPUT_PROCESSED
    archive_dir = config.OFFLOAD_DIR

    os.makedirs(archive_dir, exist_ok=True)

    _log(f"archive started for rev {rev}")

    paths = get_rev_paths(rev, src, config)

    if not paths:
        raise RuntimeError(f"No observations found for revolution {rev}.")

    archive_path = os.path.join(
        archive_dir,
        f"rev_{rev:04d}.tar",
    )

    archive(paths, src, archive_path)

    duration = time.time() - start

    _log(
        f"archive finished for rev {rev}: {len(paths)} observations in {duration:.2f}s"
    )

    return len(paths), duration


def cleanup_processed(rev, src):
    obs_paths = glob.glob(os.path.join(src, "*", "*"))

    n_removed = 0

    for path in obs_paths:
        obs = os.path.basename(path)

        final_rev = obs_final_rev.get(obs)

        if final_rev is None:
            raise RuntimeError(f"Observation {obs} is missing from the revolution map.")

        if final_rev <= rev:
            shutil.rmtree(path)
            n_removed += 1

    for month_dir in glob.glob(os.path.join(src, "*")):
        if os.path.isdir(month_dir) and not os.listdir(month_dir):
            os.rmdir(month_dir)

    return n_removed
