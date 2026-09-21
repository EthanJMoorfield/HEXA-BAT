from dataclasses import dataclass

from scripts.main import pipeline

# BAT Team Energy Bands:
# "14-20,20-24,24-35,35-50,50-75,75-100,100-150,150-195"

# Our incatalog:
# "files/HEXA_incatalog.fits"

# Our CLEANSNR:
# 6.0

# BAT PCODING_THRESH:
# 0.15


@dataclass
class PipelineConfig:
    # ----------
    # SETUP ARGS
    # ----------

    HEADAS = "/Users/ethan/miniconda3/envs/phd/heasoft"
    ARCHIVE_ROOT = "/Volumes/SSD10/BAT_archive/"
    N_PROC_PROCESS = 12
    N_PROC_MOSAIC = 12
    AUTOMATE_ENVIRONMENT = True
    DO_PROCESSING = True
    DO_MOSAICING = True
    CHECK_PROCESSED = True
    CHECK_MOSAICED = False

    START_REV = 637
    END_REV = 759

    # ---------------
    # PROCESSING ARGS
    # ---------------

    ENERGY_BANDS = "14-75"
    CLEANSNR = 6.0
    INCATALOG = "files/HEXA_incatalog.fits"
    NOISE_CORRECTION = True

    DETTHRESH = 15000
    DETTHRESH2 = 10900
    EXPOTHRESH = 120.0
    RATEMINTHRESH = 1000

    # --------------
    # MOSAICING ARGS
    # --------------

    DO_WAVELET = False
    PCODING_FILTER = True
    PCODING_THRESH = 0.05

    # -------
    # IO ARGS
    # -------

    KEEP_PROCESSED = False
    KEEP_MOSAICED = True
    OUTPUT_MOSAICED = "../HEXA_pipeline_tests/chunk_test/bat_pn_map_nowave_revs/"
    LABEL = "bat_pn_maps_nowave"
    OUTPUT_PROCESSED = "../temp_proc/"
    COMPRESS = True
    COMPRESSION_LEVEL = 5
    DECLUTTER = True
    OFFLOAD = False
    OFFLOAD_DIR = ""


if __name__ == "__main__":
    pipeline(PipelineConfig())
