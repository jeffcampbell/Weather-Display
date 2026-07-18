#!/usr/bin/env python3
"""
Standalone tide predictor using NOAA's published harmonic constituents via
pytides-py3, run as an isolated subprocess rather than imported into the
always-on proxy process — numpy/scipy only get loaded into memory for the
few seconds this runs, then released. Matters on this Pi Zero 2 W's 512MB.

Last-resort tide fallback: NOAA's live predictions API is fronted by a
flaky AWS API Gateway; when it's down long enough that even the proxy's
stale-cache fallback has nothing left, this computes predictions locally
from a station's harmonic constituents (fetched from NOAA once and cached
to disk indefinitely — see server.py's _fetch_and_cache_harmonics). Less
precise than NOAA's own predictions engine (no live recalibration, no
DST-transition edge cases hand-verified) but needs no network at all once
the constituents file exists.

Usage:
    tide_predict.py <harmonics.json> <begin_date YYYY-MM-DD> <end_date YYYY-MM-DD>

Prints {"predictions": [...]} to stdout in the same shape as NOAA's
datagetter response so callers don't need to special-case this source.
"""
import sys
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from pytidespy3 import Tide, constituent

LOCAL_TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# NOAA constituent names that don't match a pytides attribute by simple
# uppercasing (Greek-letter names, and a couple of two-letter ones pytides
# spells with different casing).
_NAME_OVERRIDES = {
    "LAM2": "lambda2",
    "MU2": "mu2",
    "NU2": "nu2",
    "RHO": "rho1",   # NOAA's harcon spells rho1 as "RHO" (no digit)
    "RHO1": "rho1",
    "MM": "Mm",
    "MF": "Mf",
    "SA": "Sa",
    "SSA": "Ssa",
}


def _lookup(name):
    override = _NAME_OVERRIDES.get(name.upper())
    attr = "_" + (override if override else name.upper())
    return getattr(constituent, attr, None)


def main():
    harmonics_path, begin_str, end_str = sys.argv[1:4]
    with open(harmonics_path) as f:
        harcon = json.load(f)

    consts, amps, phases = [], [], []
    skipped = []
    for c in harcon:
        obj = _lookup(c["name"])
        if obj is None:
            skipped.append(c["name"])
            continue
        consts.append(obj)
        amps.append(float(c["amplitude"]))
        phases.append(float(c["phase_GMT"]))

    if skipped:
        print("tide_predict: skipped unrecognized constituents: {}".format(skipped),
              file=sys.stderr)
    if not consts:
        print("tide_predict: no usable constituents in {}".format(harmonics_path),
              file=sys.stderr)
        print(json.dumps({"predictions": []}))
        return

    model = Tide(constituents=consts, amplitudes=amps, phases=phases)

    # Constituent phases are GMT-referenced, so extrema() is fed and returns
    # naive UTC datetimes — convert to local (DST-aware) time for output,
    # matching the civil-time format NOAA's own time_zone=lst_ldt produces.
    t0 = datetime.strptime(begin_str, "%Y-%m-%d")
    t1 = datetime.strptime(end_str, "%Y-%m-%d")

    predictions = []
    for t_utc, height, kind in model.extrema(t0, t1):
        t_local = t_utc.replace(tzinfo=UTC).astimezone(LOCAL_TZ)
        predictions.append({
            "t": t_local.strftime("%Y-%m-%d %H:%M"),
            "v": "{:.3f}".format(height),
            "type": kind,
        })

    print(json.dumps({"predictions": predictions, "source": "local_harmonic"}))


if __name__ == "__main__":
    main()
