"""How busy is this computer right now? One cheap, local reading for the "your computer is busy" rung (docs/design/OFFLOAD-REASONS.md, rung 5).

The signal is the load average over the last 5 minutes divided by the number of cores, so a short spike does not count and no state is kept. `read` returns
None when the computer cannot say (Windows has no load average, or the read fails): the rung then says "cannot be judged", never a guess. The platform and the
two readers can be passed in, so tests need no real machine.
"""
import os
import sys


def read(platform=None, getloadavg=None, cpus=None):
    """{'load': 5-minute load average, 'cores': n, 'ratio': load / cores} or None when it cannot be read here."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return None
    try:
        load = float((getloadavg or os.getloadavg)()[1])
        cores = (cpus or os.cpu_count)()
    except (OSError, AttributeError, IndexError, TypeError, ValueError):
        return None
    if not cores or cores < 1:
        return None
    return {"load": load, "cores": int(cores), "ratio": load / cores}
