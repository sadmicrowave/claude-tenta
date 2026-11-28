"""What a worker accepts onto its disk, in one place (security finding F5): the job ceiling and the disk reserve.

The worker uses it when it reads a job file, and the mailbox shell uses it BEFORE git receives a push, so a client holding the mailbox key cannot put more on the disk than the worker would accept.
Standard library only (the mailbox account runs it)."""

CEILING_MAX_MB = 1024.0       # a job file is never accepted above this (it travels in one JSON file staged in memory)
PUSH_SLACK_MB = 16            # what a push may carry besides the job file: git's own objects and the branch's other small files


def reserve_gb(total_gb):
    """Free disk kept back: 10% of the volume, never less than 2 GB. Below it the worker starts no job and its mailbox takes no push."""
    return max(2.0, round(total_gb * 0.10, 1))


def ceiling_mb(owner_mb, disk_free_gb, mem_available_mb):
    """The largest job file, in MB: the owner's hardware.max_job_mb, or the smallest of a quarter of the free disk, a quarter of the free memory and 1024 MB. A reading that is missing does not limit."""
    if owner_mb:
        return int(owner_mb)
    terms = [CEILING_MAX_MB]
    if disk_free_gb is not None:
        terms.append(disk_free_gb * 1000 / 4)
    if mem_available_mb is not None:
        terms.append(mem_available_mb / 4)
    return int(max(1, min(terms)))


def push_decision(free_gb, total_gb, owner_mb, mem_mb):
    """What the mailbox does with a push: (None, cap_bytes) to let git receive it, capped at the ceiling plus slack, or ("disk", facts) when the disk is below the reserve."""
    reserve = reserve_gb(total_gb)
    if free_gb < reserve:
        return "disk", {"free": f"{free_gb:.1f}", "reserve": f"{reserve:g}"}
    ceiling = ceiling_mb(owner_mb, free_gb, mem_mb)
    return None, {"ceiling": ceiling, "cap_bytes": (ceiling + PUSH_SLACK_MB) * 1_000_000}
