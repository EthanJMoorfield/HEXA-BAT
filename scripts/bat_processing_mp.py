import glob
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import fitsio
import multiprocess
import pandas as pd

from .compress_fits import compress as compress_fits
from .util import get_proc_marker, write_marker

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_WORKER_ENV = None
_WORKER_ID = None
_WORKER_CONFIG = None
_WORKER_START_TIME = None

EXPECTED_ERRORS = {
    "invalid_dph_times": re.compile(r"^ERROR: input DPH has invalid times"),
    "zero_exposure": re.compile(r"^ERROR: zero exposure in observation"),
    "no_master_gti": re.compile(r"^ERROR: master GTI contained no time intervals"),
    "no_standard_gti": re.compile(r"/gti/.*\.gti contained no good times"),
    "no_overlapping_gtis": re.compile(
        r"^WARNING: no overlapping good time intervals were found"
    ),
}


def classify_failure(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()

        for reason, pattern in EXPECTED_ERRORS.items():
            if pattern.search(stripped):
                return reason

    return None


def split_paths(a, n):
    """
    Splits images evenly among requested processes
    """

    k, m = divmod(len(a), n)
    return (a[i * k + min(i, m) : (i + 1) * k + min(i + 1, m)] for i in range(n))


def filter_log(stdout_text: str) -> str:
    noise_pattern = re.compile(r"\b(?:good|bad)\d+\b")

    lines = stdout_text.splitlines()
    kept = []
    run_count = 0
    run_label = None

    for line in lines:
        match = noise_pattern.search(line)

        if match:
            label = match.group(0)

            if run_count and label != run_label:
                kept.append(f"[{run_label} x{run_count} similar lines suppressed]")
                run_count = 0

            run_count += 1
            run_label = label
            continue

        if run_count:
            kept.append(f"[{run_label} x{run_count} similar lines suppressed]")
            run_count = 0
            run_label = None

        kept.append(line)

    if run_count:
        kept.append(f"[{run_label} x{run_count} similar lines suppressed]")

    return "\n".join(kept)


def read_gti(path: str) -> pd.DataFrame:
    dates = {}
    for dph_path in glob.glob(os.path.join(path, "bat", "survey", "*.dph.gz")):
        with fitsio.FITS(dph_path) as f:
            if "GTI" not in [hdu.get_extname() for hdu in f]:
                continue

            header = f["GTI"].read_header()
            data = f["GTI"].read()

            if header.get("MJDREFI"):
                MJD_REF = header["MJDREFI"]
                if header.get("MJDREFF"):
                    MJD_REF += header["MJDREFF"]
            else:
                MJD_REF = header["MJDREF"]

            start = data["START"]
            stop = data["STOP"]
            durations = stop - start
            mid_mjds = MJD_REF + (start + stop) / 2.0 / 86400.0

        obs = os.path.basename(dph_path)

        # accumulate exposure-per-day for this DPH file
        exposure_by_date = {}
        for mjd, dur in zip(mid_mjds, durations):
            dt = datetime(1858, 11, 17) + timedelta(days=mjd)
            date = (dt.strftime("%Y"), dt.strftime("%j"))
            exposure_by_date[date] = exposure_by_date.get(date, 0.0) + dur

        if not exposure_by_date:
            continue

        # if DPH file spans multiple days
        if len(exposure_by_date) > 1:
            majority_date = max(exposure_by_date, key=exposure_by_date.get)
            date = majority_date
        else:
            date = list(exposure_by_date)[0]

        # clamp to most recent pattern noise map
        date = (int(date[0]), int(date[1]))
        if date > (2019, 212):
            date = (2019, 212)

        dates.setdefault(date, []).append(obs)

    return dates


def stage_observation(obs_dir, dph_filenames):
    obs_dir = Path(obs_dir).resolve()
    dph_filenames = set(dph_filenames)
    staged_dir = Path("./proc/staging") / obs_dir.name
    staged_dir.parent.mkdir(exist_ok=True)
    if staged_dir.exists():
        shutil.rmtree(staged_dir)
    staged_dir.mkdir(parents=True)

    survey_dir = obs_dir / "bat" / "survey"
    bat_dir = obs_dir / "bat"

    for item in obs_dir.iterdir():
        if item.resolve() != bat_dir.resolve():
            os.symlink(item, staged_dir / item.name)

    staged_bat = staged_dir / "bat"
    staged_bat.mkdir()
    for item in bat_dir.iterdir():
        if item.resolve() != survey_dir.resolve():
            os.symlink(item, staged_bat / item.name)

    staged_survey = staged_bat / "survey"
    staged_survey.mkdir()
    for fname in os.listdir(survey_dir):
        if (
            fname.endswith(".dph") or fname.endswith(".dph.gz")
        ) and fname not in dph_filenames:
            continue
        os.symlink(survey_dir / fname, staged_survey / fname)

    return staged_dir


def _init_worker(config):
    global _WORKER_ENV, _WORKER_ID, _WORKER_CONFIG, _WORKER_START_TIME

    _WORKER_ID = os.getpid()

    base_pfile_paths = os.environ["PFILES"].split(";")
    system_pfiles = base_pfile_paths[1]

    local_pfiles = Path("./proc/proc_pfiles") / f"pfiles_{_WORKER_ID}"
    os.makedirs(local_pfiles, exist_ok=True)

    if len(base_pfile_paths) < 2:
        raise RuntimeError(
            f"Expected 'local;system' PFILES format, got: {os.environ['PFILES']}"
        )

    shutil.copytree(base_pfile_paths[1], local_pfiles, dirs_exist_ok=True)

    env = os.environ.copy()
    env["PFILES"] = f"{local_pfiles};{system_pfiles}"

    prof_dir = os.path.abspath(f"./proc/profiling/proc_{_WORKER_ID}")
    env["PROF_DIR"] = prof_dir

    # disable query/prompt access and redirect stdin
    env["HEADASNOQUERY"] = ""
    env["HEADASPROMPT"] = "/dev/null"

    _WORKER_CONFIG = {
        "timing_file": os.path.join(prof_dir, "timing.txt"),
        "config": config,
    }

    os.makedirs(prof_dir, exist_ok=True)
    subprocess.run([PROJECT_ROOT / "files" / "monitor.sh"], check=True, env=env)

    env["PATH"] = os.path.join(prof_dir, "exec") + os.pathsep + env["PATH"]

    timing_file = os.path.join(prof_dir, "timing.txt")
    with open(timing_file, "a") as f:
        f.write(f"Profiling for proc_id {_WORKER_ID}\n\n")

    _WORKER_ENV = env

    _WORKER_START_TIME = time.time()


def check_clutter(fname):
    if fname.endswith(".img"):
        return False
    if fname.endswith(".var"):
        return False
    if fname.endswith("_status.txt"):
        return False
    return True


def store_results(work_outdir, outdir):
    backup_outdir = os.path.join(
        os.path.dirname(outdir),
        f".{os.path.basename(outdir)}.old.{os.getpid()}",
    )
    old_moved = False

    shutil.rmtree(backup_outdir, ignore_errors=True)

    try:
        # move existing (old) results to backup directory
        if os.path.exists(outdir):
            os.replace(outdir, backup_outdir)
            old_moved = True
        # move working directory to permanent directory
        os.replace(work_outdir, outdir)
    except Exception:
        if old_moved and os.path.exists(backup_outdir) and not os.path.exists(outdir):
            os.replace(backup_outdir, outdir)

        raise
    else:
        # delete old results
        if old_moved:
            shutil.rmtree(backup_outdir, ignore_errors=True)


def _process_one(path):
    global _WORKER_ENV, _WORKER_CONFIG, _WORKER_ID

    start_time = time.time()

    config = _WORKER_CONFIG["config"]
    timing_file = _WORKER_CONFIG["timing_file"]

    if not os.path.exists(path):
        raise FileNotFoundError(f"Archive path {path} not found.")

    obs = os.path.basename(path)
    month = os.path.basename(Path(path).parents[0])

    with open(timing_file, "a") as f:
        f.write(f"# {obs}\n")

    if config.NOISE_CORRECTION:
        dph_dates = read_gti(path)
        if not dph_dates:
            return None
    else:
        dph_dates = {
            (0, 0): [
                os.path.basename(p)
                for p in glob.glob(os.path.join(path, "bat", "survey", "*.dph.gz"))
            ]
        }

    proc = None
    staged_dirs = []
    logs = []

    requested_bands = config.ENERGY_BANDS.split(",")
    edges = [int(e) for e in sum([band.split("-") for band in requested_bands], [])]
    limits = f"{min(edges)}-{max(edges)}"

    month_outdir = os.path.join(config.OUTPUT_PROCESSED, month)
    outdir = os.path.join(month_outdir, obs)

    os.makedirs(month_outdir, exist_ok=True)

    # write everything to a temporary working per-obs directory
    # Once processing has finished this replaces any existing proc results
    work_outdir = os.path.join(
        month_outdir,
        f".{obs}.inprogress.{os.getpid()}",
    )

    # This should always be a completely fresh observation result
    shutil.rmtree(work_outdir, ignore_errors=True)
    os.makedirs(work_outdir)

    commited = False

    try:
        for (year, day), dph_list in dph_dates.items():
            staged = stage_observation(path, dph_list)
            staged_dirs.append(staged)

            group_outdir = os.path.join("./proc/staging", f"{obs}_out_{year}{day:03d}")

            shutil.rmtree(group_outdir, ignore_errors=True)
            os.makedirs(group_outdir)

            command = [
                "batsurvey",
                f"indir={staged}",
                f"outdir={group_outdir}",
                f"energybins={config.ENERGY_BANDS}",
                f"elimits={limits}",
                f"detthresh={config.DETTHRESH}",
                f"detthresh2={config.DETTHRESH2}",
                f"expothresh={config.EXPOTHRESH}",
                f"rateminthresh={config.RATEMINTHRESH}",
                "filter_midnight=YES",
                "clobber=YES",
            ]

            if config.CLEANSNR:
                command.extend([f"cleansnr={config.CLEANSNR}"])

            if config.INCATALOG:
                command.extend(
                    [f"incatalog={config.INCATALOG}", "cleanexpr=ALWAYS_CLEAN"]
                )

            if config.NOISE_CORRECTION:
                command.extend(
                    [
                        f"global_pattern_map=files/one_band_pattern_maps/pattern_noise_survey8a_{year}{day:03d}_14-75.dpi",
                        f"global_pattern_mask=files/one_band_pattern_maps/pattern_noise_survey8a_{year}{day:03d}_14-75.detmask",
                    ]
                    # [
                    #     f"global_pattern_map=files/hexa_pattern_maps/pattern_noise_map_{year}{day:03d}.fits.gz",
                    #     f"global_pattern_mask=files/one_band_pattern_maps/pattern_noise_survey8a_{year}{day:03d}_14-75.detmask",
                    # ]
                )

            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                env=_WORKER_ENV,
            )

            output = proc.stdout or ""
            if not output:
                raise RuntimeError("Received no logging information from batsurvey.")

            logs.append(output)

            if proc.returncode != 0:
                failure_reason = classify_failure(output)

                if failure_reason is not None:
                    shutil.rmtree(group_outdir, ignore_errors=True)

                    logs.append(
                        f"\nSkipped due to expected data failure: "
                        f"{failure_reason}. Return code: {proc.returncode}\n"
                    )
                    continue

                with open("proc/crash_log.txt", "w+") as f:
                    f.write(output)

                raise RuntimeError(
                    f"batsurvey failed for observation: {obs}, "
                    f"{year}-{day:03d}. Return Code: {proc.returncode}. "
                    f"Crash Log written to proc/crash_log.txt."
                )

            for root, dirs, files in os.walk(group_outdir):
                rel = os.path.relpath(root, group_outdir)

                # write to (temporary) working directory
                dest_root = (
                    os.path.join(work_outdir, rel) if rel != "." else work_outdir
                )
                for fname in files:
                    src = os.path.join(root, fname)
                    dst = os.path.join(dest_root, fname)
                    if config.DECLUTTER:
                        if not check_clutter(os.path.basename(src)):
                            os.makedirs(dest_root, exist_ok=True)
                            shutil.move(src, dst)
                    else:
                        os.makedirs(dest_root, exist_ok=True)
                        shutil.move(src, dst)

        if config.COMPRESS:
            compress_fits(work_outdir, config.COMPRESSION_LEVEL)

        if proc:
            logfile = os.path.join(work_outdir, f"{obs}_log.txt")

            with open(logfile, "w+") as f:
                for log in logs:
                    f.write(filter_log(log))
                    f.write("\n")

        with open(timing_file, "a") as f:
            f.write(f"Observation took {time.time() - start_time}s\n\n")

        # create completion marker
        marker = get_proc_marker(
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

        # write completion marker to working directory before it is comitted
        write_marker(
            os.path.join(work_outdir, "proc_completion.json"),
            "proc",
            marker,
        )

        # commit working directory
        store_results(work_outdir, outdir)
        commited = True

    finally:
        # if something went wrong, delete working directory
        if not commited:
            shutil.rmtree(work_outdir, ignore_errors=True)


def process(
    paths,
    config,
    progress=None,
):
    staging_dir = Path("./proc/staging")
    pfiles_dir = Path("./proc/proc_pfiles")

    shutil.rmtree(staging_dir, ignore_errors=True)
    os.makedirs(staging_dir, exist_ok=True)

    shutil.rmtree(pfiles_dir, ignore_errors=True)
    pfiles_dir.mkdir(parents=True)

    # sort in descending order of how long observations should take to process
    paths = sorted(
        paths,
        key=lambda p: len(glob.glob(os.path.join(p, "bat", "survey", "*.dph.gz"))),
        reverse=True,
    )

    if len(set(paths)) != len(paths):
        raise ValueError("Process received duplicate paths.")

    progress.reset_processing(len(paths))
    progress.update_status("workers", f"Initialised {config.N_PROC_PROCESS} workers.")

    try:
        with multiprocess.Pool(
            processes=config.N_PROC_PROCESS,
            initializer=_init_worker,
            initargs=(config,),
        ) as pool:
            for _ in pool.imap_unordered(_process_one, paths, chunksize=1):
                if progress:
                    progress.advance_processing()
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(pfiles_dir, ignore_errors=True)
