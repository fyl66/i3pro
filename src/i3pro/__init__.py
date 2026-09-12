"""i3pro - MoTeC .ld/.ldx telemetry toolkit (pure Python, no external parsers).

Public API::

    from i3pro import ld

    log = ld.LogFile.read("run.ld")
    log.metadata            # device / date / event name ...
    log.channels            # list[Channel]
    log.channel("Ground Speed").values()   # np.float64 array, engineering units
"""

from . import ld, laps, motec_csv, store

__all__ = ["ld", "laps", "motec_csv", "store"]
__version__ = "0.1.0"
