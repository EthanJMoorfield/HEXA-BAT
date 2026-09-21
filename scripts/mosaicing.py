import logging
import os
import shutil
import warnings
from pathlib import Path
from queue import Empty

import multiprocess
import numpy as np
import pywt
from astropy.io import fits
from astropy.io.fits.verify import VerifyWarning
from astropy.utils.exceptions import AstropyUserWarning, AstropyWarning
from gammapy.maps import HpxGeom, HpxMap, Map

from .compress_fits import gzip_file
from .util import get_mosaic_marker, write_marker

# filter out warnings
warnings.simplefilter("ignore", category=VerifyWarning)
warnings.simplefilter("ignore", category=AstropyWarning)
warnings.simplefilter("ignore", category=AstropyUserWarning)
warnings.simplefilter("ignore", category=UserWarning)

logging.basicConfig(level=logging.CRITICAL)
np.seterr(divide="ignore", invalid="ignore")


newline = "\n"

PROC_FILE_PATH = "proc/mosaic_pfiles"

DO_LOGGING = True


# set geometry of healpix maps
geom = HpxGeom.create(nside=1024, frame="galactic", nest=True)


def split_image_list(a, n):
    """
    Splits images evenly among requested processes
    """

    k, m = divmod(len(a), n)
    return (a[i * k + min(i, m) : (i + 1) * k + min(i + 1, m)] for i in range(n))


def mosaic_images(
    procid: int, flux_map: Map, err_map: Map, pcoding_map: Map, expo: float, config
):
    # read images
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

    flux = flux_map.data
    var = err_map.data
    np.square(var, out=var)

    pcoding = pcoding_map.data
    expo = pcoding * expo

    expo_map = Map.from_geom(flux_map.geom)

    if config.PCODING_FILTER:
        # masking
        masks = []

        # flux mask
        useful_flux_mask = np.isfinite(flux)
        masks.append(useful_flux_mask)

        # var mask
        useful_var_mask = (np.isfinite(var)) & (var > 0)
        masks.append(useful_var_mask)

        # pcoding mask
        useful_pcoding_mask = pcoding > config.PCODING_THRESH

        # check if map contains 'bad' pcoding pixels and apply stricter pcoding filter
        # not convinced that we need this anymore
        # no_nans_pcoding = pcoding[~np.isnan(pcoding)]
        # bad_pcoding = np.any((pcoding > 0) & (pcoding < BAD_PCODING_THRESH))
        # if bad_pcoding:
        #     useful_pcoding_mask = pcoding > PCODING_BUFFER

        masks.append(useful_pcoding_mask)

        # combine masks
        useful_mask = masks[0]
        for mask in masks[1:]:
            useful_mask = useful_mask & mask

        # enforce combined mask
        flux[~useful_mask] = np.nan
        var[~useful_mask] = np.nan
        expo[~useful_mask] = np.nan

    # store mask of nan flux values and set them to zero temporarily
    flux_nan = np.isnan(flux)
    np.nan_to_num(flux, copy=False)

    # do wavelet filtering (this has to be done AFTER masking)
    if config.DO_WAVELET:
        max_level = pywt.dwtn_max_level(flux.shape, "sym8")
        coeffs = pywt.wavedec2(flux, "sym8", level=max_level)
        coeffs[0].fill(0)
        flux = pywt.waverec2(coeffs, "sym8")

    # set values that were previously nan back to nan
    flux[flux_nan] = np.nan
    var[flux_nan] = np.nan
    expo[flux_nan] = np.nan

    # Check if nans match exactly for both arrays
    if not np.array_equal(np.isnan(flux), np.isnan(var)):
        print("WARNING: Mismatch in flux/var NaNs detected.")

    flux_map.data = flux
    err_map.data = var
    expo_map.data = expo

    # reproject maps to healpix
    flux_proj = flux_map.interp_to_geom(
        geom, preserve_counts=False, fill_value=np.nan, method="nearest"
    )
    var_proj = err_map.interp_to_geom(
        geom, preserve_counts=False, fill_value=np.nan, method="nearest"
    )
    expo_proj = expo_map.interp_to_geom(
        geom, preserve_counts=False, fill_value=np.nan, method="nearest"
    )

    # check if nans match exactly for projected arrays
    if not np.array_equal(np.isnan(flux_proj.data), np.isnan(var_proj.data)):
        if not np.array_equal(np.isnan(flux_proj.data), np.isnan(expo_proj.data)):
            print("WARNING: Mismatch in projected flux/var/expo NaNs detected.")

    # calculate weights
    weights = var_proj.data

    good_weight = np.isfinite(weights) & (weights > 0)

    # var -> weight
    np.reciprocal(weights, out=weights, where=good_weight)
    weights[~good_weight] = 0.0

    if not np.all(np.isfinite(flux_proj.data[good_weight])):
        raise RuntimeError("Non-finite projected flux with non-zero weight detected.")

    expo_proj.data[~good_weight] = 0.0

    # nan/inf pixels get zero weight
    weights[~np.isfinite(weights)] = 0

    # nan pixels get zero value (ok as they also get zero weight)
    flux_proj.data = np.nan_to_num(flux_proj.data, copy=False)

    # nan exposures get zero value, and hence don't contribute
    expo_proj.data = np.nan_to_num(expo_proj.data, copy=False)

    # convert flux map to weighted fluxes
    np.multiply(flux_proj.data, weights, out=flux_proj.data)

    if (
        not config.PCODING_FILTER
        and np.nanmin(flux_proj.data) < -8e5
        or np.nanmax(flux_proj.data) > 8e5
    ):
        return None

    return flux_proj.data, weights, expo_proj.data


def mosaic_dispatch(
    procid: int, pfiles_path: str, image_list: list[str], config, progress_queue=None
):
    requested_bands = config.ENERGY_BANDS.split(",")

    # create local accumulators for each energy band
    accumulators = {}
    for band in requested_bands:
        accumulators[f"local_weighted_{band}"] = HpxMap.from_geom(
            geom, dtype=np.float32
        )
        accumulators[f"local_weights_{band}"] = HpxMap.from_geom(geom, dtype=np.float32)
        accumulators[f"local_expo_{band}"] = HpxMap.from_geom(geom, dtype=np.float32)

    for flux_image in image_list:
        err_image = flux_image.replace(".img", ".var")

        with fits.open(flux_image) as flux_hdul, fits.open(err_image) as err_hdul:
            expo_s = flux_hdul[0].header["EXPOSURE"]
            pcoding_map = Map.from_hdulist(flux_hdul, hdu=len(requested_bands))

            for idx, band in enumerate(requested_bands):
                flux_map = Map.from_hdulist(flux_hdul, hdu=idx)
                err_map = Map.from_hdulist(err_hdul, hdu=idx)

                proj = mosaic_images(
                    procid,
                    flux_map,
                    err_map,
                    pcoding_map,
                    expo_s,
                    config,
                )

                if proj is None:
                    del flux_map
                    del err_map

                    continue

                weighted, weights, expo = proj

                accumulators[f"local_weighted_{band}"].data += weighted
                accumulators[f"local_weights_{band}"].data += weights
                accumulators[f"local_expo_{band}"].data += expo

                del proj
                del weighted
                del weights
                del expo
                del flux_map
                del err_map

        if progress_queue is not None:
            progress_queue.put("progress")

    # write weighted fluxes and weights to proc files for each energy band
    for i, band in enumerate(requested_bands):
        weighted_out = os.path.join(pfiles_path, f"phm_proc{procid}_{band}_wflux.fits")
        accumulators[f"local_weighted_{band}"].write(weighted_out, overwrite=True)

        weights_out = os.path.join(pfiles_path, f"phm_proc{procid}_{band}_weights.fits")
        accumulators[f"local_weights_{band}"].write(weights_out, overwrite=True)

        expo_out = os.path.join(pfiles_path, f"phm_proc{procid}_{band}_expo.fits")
        accumulators[f"local_expo_{band}"].write(expo_out, overwrite=True)

    if progress_queue is not None:
        progress_queue.put("done")


def append_map(path, map_):
    hdu = map_.to_hdu()
    fits.append(
        path,
        hdu.data,
        hdu.header,
        verify=False,
    )


def do_mosaic(rev: int, img_paths: tuple[str] | tuple[Path], config, progress=None):
    """
    Sets up processes and reads final proc files to construct mosaiced maps
    """

    requested_bands = config.ENERGY_BANDS.split(",")

    os.makedirs(os.path.join(config.OUTPUT_MOSAICED), exist_ok=True)

    if not img_paths:
        return

    if not isinstance(img_paths, list):
        img_paths = [img_paths]

    if len(img_paths) != len(set(img_paths)):
        bad_imgs = []
        for scw in set(img_paths):
            temp_imgs = [s for s in img_paths if s == scw]
            if len(temp_imgs) > 1:
                bad_imgs.append(scw)
        raise RuntimeError(f"Duplicate sky images detected:\n {newline.join(bad_imgs)}")

    progress_queue = multiprocess.Queue()

    image_lists = list(split_image_list(img_paths, config.N_PROC_MOSAIC))

    progress.reset_mosaic(len(img_paths))

    pfiles_path = os.path.join(PROC_FILE_PATH, str(os.getpid()))
    os.makedirs(pfiles_path, exist_ok=True)

    jobs, procids = [], []
    for procid in range(config.N_PROC_MOSAIC):
        if image_lists[procid]:
            process = multiprocess.Process(
                target=mosaic_dispatch,
                args=(procid, pfiles_path, image_lists[procid], config, progress_queue),
            )
            jobs.append(process)
            procids.append(procid)

    progress.update_status("workers", f"Initialised {config.N_PROC_MOSAIC} workers.")

    marker_path = os.path.join(
        config.OUTPUT_MOSAICED, f"bat_{str(rev).zfill(4)}_completion.json"
    )
    if os.path.exists(marker_path):
        os.remove(marker_path)

    try:
        # run processes
        for j in jobs:
            j.start()

        finished = 0
        while finished < len(jobs):
            try:
                msg = progress_queue.get(timeout=1)
            except Empty:
                failed = [job for job in jobs if job.exitcode not in (None, 0)]
                if failed:
                    for job in jobs:
                        if job.is_alive():
                            job.terminate()
                    for job in jobs:
                        job.join()
                    raise RuntimeError("Mosaicking worker failed.")
                continue

            if msg == "progress":
                if progress:
                    progress.advance_mosaic()

            elif msg == "done":
                finished += 1

        for j in jobs:
            j.join()

        # check for worker failures
        failed = [j for j in jobs if j.exitcode != 0]
        if failed:
            failure_str = "\n".join(f"pid={j.pid}, exit={j.exitcode}" for j in failed)
            raise RuntimeError(f"Mosaicking Worker Failure:\n{failure_str}.")

        # set up output files
        if not config.LABEL:
            out_path = os.path.join(
                config.OUTPUT_MOSAICED,
                f"bat_{str(rev).zfill(4)}",
            )
        else:
            out_path = os.path.join(
                config.OUTPUT_MOSAICED,
                f"bat_{str(rev).zfill(4)}_{config.LABEL}",
            )

        flux_path = f"{out_path}_flux.fits"
        error_path = f"{out_path}_error.fits"
        sig_path = f"{out_path}_sig.fits"
        expo_path = f"{out_path}_expo.fits"

        out_paths = [
            flux_path,
            error_path,
            sig_path,
            expo_path,
        ]

        for path in out_paths:
            fits.PrimaryHDU().writeto(path, overwrite=True)

        for band in requested_bands:
            weighted = HpxMap.from_geom(geom, dtype=np.float32)
            weights = HpxMap.from_geom(geom, dtype=np.float32)
            exposure = HpxMap.from_geom(geom, dtype=np.float32)

            for procid in procids:
                weighted_file = os.path.join(
                    pfiles_path,
                    f"phm_proc{procid}_{band}_wflux.fits",
                )
                weights_file = os.path.join(
                    pfiles_path,
                    f"phm_proc{procid}_{band}_weights.fits",
                )
                exposure_file = os.path.join(
                    pfiles_path,
                    f"phm_proc{procid}_{band}_expo.fits",
                )

                proc_map = HpxMap.read(weighted_file)
                weighted.data += proc_map.data
                del proc_map

                proc_map = HpxMap.read(weights_file)
                weights.data += proc_map.data
                del proc_map

                proc_map = HpxMap.read(exposure_file)
                exposure.data += proc_map.data
                del proc_map

            valid = np.isfinite(weights.data) & (weights.data > 0)

            # weighted flux -> final flux
            np.divide(
                weighted.data,
                weights.data,
                out=weighted.data,
                where=valid,
            )
            weighted.data[~valid] = np.nan

            # weights -> final error
            np.sqrt(
                weights.data,
                out=weights.data,
                where=valid,
            )

            np.reciprocal(
                weights.data,
                out=weights.data,
                where=valid,
            )

            weights.data[~valid] = np.nan

            # check data validity
            final_mask = (
                np.isfinite(weighted.data)
                & np.isfinite(weights.data)
                & (weights.data > 0)
            )

            weighted.data[~final_mask] = np.nan
            weights.data[~final_mask] = np.nan
            exposure.data[~final_mask] = np.nan

            # At this point:
            # weighted = final flux
            # weights = final error
            # exposure = final exposure
            append_map(flux_path, weighted)
            append_map(error_path, weights)
            append_map(expo_path, exposure)

            # reuse flux array for significance
            np.divide(
                weighted.data,
                weights.data,
                out=weighted.data,
                where=final_mask,
            )
            weighted.data[~final_mask] = np.nan

            # weighted now contains significance
            append_map(sig_path, weighted)

            del weighted
            del weights
            del exposure
            del valid
            del final_mask

        # in case maps are being stored in the same directory as processed data, only gzip these specific files
        if config.COMPRESS:
            for file in out_paths:
                gzip_file(file, config.COMPRESSION_LEVEL)

        if os.path.exists(marker_path):
            os.remove(marker_path)

        # write completion marker to output directory
        marker = get_mosaic_marker(
            band,
            config.PCODING_FILTER,
            config.PCODING_THRESH,
            config.DO_WAVELET,
            config.COMPRESS,
        )
        write_marker(marker_path, "mosaic", marker)

    finally:
        shutil.rmtree(pfiles_path, ignore_errors=True)
