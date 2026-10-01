import glob
import os
import queue
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import multiprocess

from .bat_processing_mp import _init_worker, _process_one
from .transfer import cleanup_processed, offload
from .util import check_archive_complete, check_marker, get_obs_paths, get_proc_marker


# finish a ready revolution and log its timing
def _archive_ready_rev(
    rev,
    config,
    progress,
    rev_start,
    processing_duration,
    n_processed,
    offload_result=None,
    remove_empty_months=False,
):
    archive_complete = config.CHECK_ARCHIVES and check_archive_complete(config, rev)
    n_offload = 0
    offload_time = 0.0

    # use the result from this run if offloading just finished
    if offload_result is not None:
        n_offload, offload_time = offload_result
        archive_complete = True
    elif config.OFFLOAD and not archive_complete:
        raise RuntimeError(f"Revolution {rev} has not finished offloading.")

    # remove processed files only after their archive is ready
    if config.OFFLOAD and archive_complete:
        cleanup_processed(
            rev, config.OUTPUT_PROCESSED, remove_empty_months=remove_empty_months
        )

    # mark the revolution complete and record its benchmark
    progress.rev_done()
    progress.refresh()

    duration = time.time() - rev_start

    with open("./proc/benchmark.txt", "a") as f:
        f.write(f"Revolution {rev}\n")
        f.write(f"Started: {datetime.fromtimestamp(rev_start)}\n")
        f.write(f"Processed {n_processed} observations in {processing_duration:.2f}s\n")
        if config.OFFLOAD:
            f.write(f"Offloaded {n_offload} observations in {offload_time:.2f}s\n")
        f.write(f"Duration: {duration:.2f}\n")
        f.write(f"Ended: {datetime.now()}\n\n")


def run_continuous(config, obs_map, revs, progress):
    # get_obs_paths skips observations with valid processing markers (if CHECK_PROCESSED = True)
    rev_map = obs_map[obs_map.rev.isin(revs)]
    paths, _ = get_obs_paths(config, rev_map)

    # deduplicate paths - these are paths that need to be processed
    pending = set(paths)

    # required includes shared observations, since each rev needs them to archive
    required = {rev: set() for rev in revs}
    for _, row in rev_map.iterrows():
        path = os.path.join(config.ARCHIVE_ROOT, row["month"], row["obs"])
        required[row["rev"]].add(path)

    # union combines sets stored in required, subtract pending to get work already done
    completed = set().union(*required.values()) - pending
    owned = {}
    assigned = set()
    for rev in revs:
        # Keep only pending paths not assigned to an earlier revolution
        owned[rev] = (required[rev] & pending) - assigned
        assigned.update(owned[rev])

    # get dict of which rev each path belongs to
    # shared paths use the first rev that needs new processing
    owner = {path: rev for rev in revs for path in owned[rev]}

    # get number of paths in each rev
    # this counts only paths owned by that rev
    remaining = {rev: len(owned[rev]) for rev in revs}

    # More DPH files usually mean more work, so schedule these paths first
    dph_count = {
        path: len(glob.glob(os.path.join(path, "bat", "survey", "*.dph.gz")))
        for path in pending
    }

    # Each deque (double-ended queue) holds sorted jobs for one rev
    waiting = {
        rev: deque(sorted(required[rev] & pending, key=dph_count.get, reverse=True))
        for rev in revs
    }

    # reset pfiles and staging
    staging_dir = Path("./proc/staging")
    pfiles_dir = Path("./proc/proc_pfiles")
    shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True)
    shutil.rmtree(pfiles_dir, ignore_errors=True)
    pfiles_dir.mkdir(parents=True)

    # A completed output must match the settings for this run
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

    progress.update_status("workers", f"Initialised {config.N_PROC_PROCESS} workers.")

    # Worker callbacks and the archive thread post events for the coordinator
    rev_started = {}
    processed_at = {}
    next_archive = 0

    # submitted prevents duplicate jobs, inflight limits queued and running jobs
    submitted = set()
    inflight = {}
    events = queue.Queue()
    archive_thread = None
    archive_result = None

    # display the oldest revolution, including work completed in lookahead
    def show_current_rev():
        rev = revs[next_archive]
        to_process = owned[rev]
        n_processed = len(required[rev]) - len(to_process)

        # reset the bar and credit any of its jobs already finished
        progress.reset_processing(len(to_process))
        for _ in to_process & completed:
            progress.advance_processing()

        progress.update_status(
            "heading", f"Processing revolution {rev} ({next_archive + 1}/{len(revs)})"
        )
        if to_process:
            progress.update_status(
                "to_process",
                f"{len(required[rev])} observations to process, "
                f"{n_processed} already processed",
            )
        else:
            progress.update_status("to_process", "All observations already processed")

    def next_path():
        # Queue jobs only from the oldest rev and its limited lookahead window
        for rev in revs[next_archive : next_archive + config.LOOKAHEAD_REVS]:
            # A shared path may already have been queued via an earlier rev
            while waiting[rev] and waiting[rev][0] in submitted:
                # if observation has been submitted, remove from queue
                waiting[rev].popleft()

            # if there are any observations left, return the next one
            if waiting[rev]:
                return waiting[rev].popleft()

        return None

    # show the first revolution before starting the workers
    show_current_rev()

    try:
        # keep one worker pool alive across all revolutions
        with multiprocess.Pool(
            processes=config.N_PROC_PROCESS,
            initializer=_init_worker,
            initargs=(config,),
        ) as pool:
            # keep processing until every requested revolution is complete
            while next_archive < len(revs):
                # finish only the oldest revolution once all its paths are complete
                if archive_thread is None and required[revs[next_archive]] <= completed:
                    rev = revs[next_archive]

                    # use its first job time, or now if it had no new jobs
                    rev_started.setdefault(rev, time.time())

                    archive_complete = config.CHECK_ARCHIVES and check_archive_complete(
                        config, rev
                    )

                    # write a missing archive without stopping worker scheduling
                    if config.OFFLOAD and not archive_complete:
                        archive_result = {"n": 0, "time": 0.0, "error": None}

                        # send the offload result back to the main loop
                        def _run_offload(rev=rev, result=archive_result):
                            try:
                                result["n"], result["time"] = offload(rev, config)
                            except BaseException as e:
                                result["error"] = e
                            finally:
                                events.put(("archive", rev))

                        thread = threading.Thread(target=_run_offload)
                        thread.start()
                        archive_thread = thread
                    else:
                        # no offload is needed, so finish this revolution now (will not be archived)
                        _archive_ready_rev(
                            rev,
                            config,
                            progress,
                            rev_started[rev],
                            processed_at.get(rev, rev_started[rev]) - rev_started[rev],
                            len(owned[rev]),
                            remove_empty_months=next_archive == len(revs) - 1,
                        )
                        next_archive += 1
                        if next_archive < len(revs):
                            show_current_rev()

                        continue

                # keep a limited backlog of jobs ready for idle workers
                while len(inflight) < 2 * config.N_PROC_PROCESS:
                    path = next_path()
                    if path is None:
                        break

                    # remember when this revolution first entered the queue
                    rev_started.setdefault(owner[path], time.time())
                    submitted.add(path)

                    # callbacks report completion, path=path keeps each job's path
                    inflight[path] = pool.apply_async(
                        _process_one,
                        (path,),
                        callback=lambda _, path=path: events.put(("observation", path)),
                        error_callback=lambda _, path=path: events.put(
                            ("observation", path)
                        ),
                    )

                # stop if nothing can finish the oldest revolution
                if not inflight and archive_thread is None:
                    raise RuntimeError(
                        f"Revolution {revs[next_archive]} has unfinished observations but no running jobs."
                    )

                # wait for a worker result or the offload thread
                try:
                    kind, value = events.get(timeout=1)
                except queue.Empty:
                    continue

                # finish an archive before advancing to the next revolution
                if kind == "archive":
                    archive_thread.join()
                    archive_thread = None
                    # leave processed files intact if offloading failed
                    if archive_result["error"] is not None:
                        raise archive_result["error"]

                    _archive_ready_rev(
                        value,
                        config,
                        progress,
                        rev_started[value],
                        processed_at.get(value, rev_started[value])
                        - rev_started[value],
                        len(owned[value]),
                        (archive_result["n"], archive_result["time"]),
                        remove_empty_months=next_archive == len(revs) - 1,
                    )

                    archive_result = None
                    next_archive += 1

                    if next_archive < len(revs):
                        show_current_rev()
                    continue

                # get() raises any worker error before accepting its output
                path = value
                inflight.pop(path).get()

                # map the raw observation path to its processed output
                outdir = os.path.join(
                    config.OUTPUT_PROCESSED,
                    os.path.basename(os.path.dirname(path)),
                    os.path.basename(path),
                )

                marker_path = os.path.join(outdir, "proc_completion.json")
                if not check_marker(marker_path, "proc", expected_marker):
                    raise RuntimeError(f"Processed observation incomplete: {path}")

                # update the owning revolution after its marker is verified
                completed.add(path)
                rev = owner[path]
                remaining[rev] -= 1

                if not remaining[rev]:
                    # record when the last job owned by this revolution finishes
                    processed_at[rev] = time.time()

                # lookahead jobs do not advance the visible revolution's bar
                if path in owned[revs[next_archive]]:
                    progress.advance_processing()
    finally:
        # wait for any archive, then remove working directories
        if archive_thread is not None:
            archive_thread.join()

        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(pfiles_dir, ignore_errors=True)
