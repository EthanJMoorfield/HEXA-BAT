import os
import re
import shutil
import subprocess
from pathlib import Path

CALDB_PATH = os.path.abspath("files/caldb")

PROC_ROOT = Path("proc")


def update_caldb_init():
    sh_path = Path(os.path.join(CALDB_PATH, "caldbinit.sh"))

    content = sh_path.read_text()
    updated_content = re.sub(
        r"CALDB=.*?; export CALDB", f"CALDB={CALDB_PATH}; export CALDB", content
    )
    sh_path.write_text(updated_content)


def setup_heasoft(headas_path):
    cmd = f"""
    set -e

    export HEADAS={headas_path}
    source "$HEADAS/headas-init.sh"

    export CALDB={CALDB_PATH}
    source "$CALDB/caldbinit.sh"

    export HEADASNOQUERY=
    export HEADASPROMPT=/dev/null

    command -v batsurvey > /dev/null

    caldbinfo infomode=INST chatter=0 mission=SWIFT instrument=BAT > /dev/null

    env
    """

    proc = subprocess.run(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )

    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ[key] = value


def reset_dir(path):
    path = Path(path)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def setup_proc():
    PROC_ROOT.mkdir(parents=True, exist_ok=True)

    # All of these should start every pipeline run empty
    reset_dir(PROC_ROOT / "staging")
    reset_dir(PROC_ROOT / "proc_pfiles")
    reset_dir(PROC_ROOT / "mosaic_pfiles")

    # Profiling from the previous run is discarded here.
    # The new run's profiling is then retained until the next run.
    reset_dir(PROC_ROOT / "profiling")

    # Logs which otherwise risk referring to a previous run
    for filename in [
        "crash_log.txt",
        "offload_log.txt",
    ]:
        path = PROC_ROOT / filename
        if path.exists():
            path.unlink()


def setup(headas_path):
    subprocess.run(["chmod", "+x", "files/monitor.sh"], check=True)

    os.makedirs(CALDB_PATH, exist_ok=True)
    os.environ["CALDB"] = os.path.abspath(CALDB_PATH)

    update_caldb_init()

    setup_heasoft(headas_path)

    # subprocess.run(["caldbinfo", "INST", "SWIFT", "BAT"])

    print()
