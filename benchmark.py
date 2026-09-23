import shutil
from dataclasses import dataclass

from scripts.main import pipeline


@dataclass
class PipelineConfig:
    # ----------------------
    # CHANGE THESE IF NEEDED
    # ----------------------

    HEADAS = "/Users/ethan/miniconda3/envs/phd/heasoft"
    ARCHIVE_ROOT = "/Volumes/SSD10/BAT_archive"
    N_PROC_PROCESS = 8
    START_REV = 1500
    END_REV = 1500
    ENERGY_BANDS = "14-75"
    OUTPUT_PROCESSED = "../temp_proc/"
    OFFLOAD_DIR = "../benchmark/"

    # ----------------------------
    # SHOULDN'T NEED TO BE CHANGED
    # ----------------------------

    AUTOMATE_ENVIRONMENT = True
    N_PROC_MOSAIC = 8
    DO_MOSAICING = False
    DO_PROCESSING = True
    CHECK_MOSAICED = False
    CHECK_PROCESSED = False
    CHECK_ARCHIVES = False
    NOISE_CORRECTION = False
    CLEANSNR = 6.0
    DETTHRESH = 15000
    DETTHRESH2 = 10900
    EXPOTHRESH = 120.0
    RATEMINTHRESH = 1000
    INCATALOG = "files/HEXA_incatalog.fits"
    DO_WAVELET = False
    PCODING_FILTER = False
    PCODING_THRESH = 0.05
    KEEP_PROCESSED = True
    KEEP_MOSAICED = False
    OUTPUT_MOSAICED = ""
    LABEL = ""
    DECLUTTER = False
    OFFLOAD = True
    COMPRESS = True
    COMPRESSION_LEVEL = 5


if __name__ == "__main__":
    pipeline(PipelineConfig())

shutil.rmtree(PipelineConfig.OUTPUT_PROCESSED)
