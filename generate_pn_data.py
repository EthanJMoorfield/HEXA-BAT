from dataclasses import dataclass

from scripts.main import pipeline


@dataclass
class PipelineConfig:
    # ----------------------
    # CHANGE THESE IF NEEDED
    # ----------------------

    HEADAS = ""
    ARCHIVE_ROOT = ""
    N_PROC_PROCESS = 8
    START_REV = 0
    END_REV = 2177
    ENERGY_BANDS = "14-20,20-24,24-35,35-50,50-75,75-100,100-150,150-195"
    OUTPUT_PROCESSED = "../temp_proc/"
    OFFLOAD_DIR = ""

    # ----------------------------
    # SHOULDN'T NEED TO BE CHANGED
    # ----------------------------

    AUTOMATE_ENVIRONMENT = True
    N_PROC_MOSAIC = 8
    DO_MOSAICING = False
    DO_PROCESSING = True
    CHECK_MOSAICED = False
    CHECK_PROCESSED = True
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
