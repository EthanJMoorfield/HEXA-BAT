import gzip
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FITS_MAGIC_HEADER = b"SIMPLE  ="
HEADER_BYTES = 80

with open("files/fits_extensions.txt", "r") as f:
    fits_exts = [line.strip("\n") for line in f.readlines()]


def is_fits(path: str) -> bool:
    ext = "".join(Path(path).suffixes)

    return ext in fits_exts


def gzip_file(path, level):
    gz_path = path + ".gz"
    tmp_path = gz_path + ".tmp"

    try:
        with (
            open(path, "rb") as src,
            gzip.open(tmp_path, "wb", compresslevel=level) as dst,
        ):
            shutil.copyfileobj(src, dst)

        os.replace(tmp_path, gz_path)
        os.remove(path)

    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def compress(root: str, level: int) -> None:

    files = []

    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".gz") and is_fits(name):
                files.append(os.path.join(dirpath, name))

    with ThreadPoolExecutor(max_workers=8) as executor:
        # catches and re-raises exceptions
        for _ in executor.map(gzip_file, files, [level] * len(files)):
            pass
