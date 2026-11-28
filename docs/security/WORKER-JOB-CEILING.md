# What a worker accepts: the job ceiling and the disk reserve

Security spot-check finding F5 (a job with no size limit could fill a worker's disk or memory), and the decision that Direct has no fixed size limit: a Direct worker accepts what it can really hold, and says so.

- **The ceiling.** `[hardware] max_job_mb` in the worker's `armada.toml`; unset means automatic: the smallest of a quarter of the worker's free disk, a quarter of its free memory and 1024 MB. The memory term exists because a job's files travel inside one JSON file that the worker stages in memory (about three to four times its size, for seconds); it goes away when files are streamed to disk as separate objects (the next slice).
- **Checked before the file is read.** At first sight the worker asks git for the job file's size on its own clone (`git cat-file -s`: no network, nothing loaded). A job over the ceiling is refused, unread, with code `job.too_big_for_worker`, and marked done so it is not retried every pass.
- **A reserve.** Below the larger of 2 GB or 10% of the volume, the worker starts no job ("disk nearly full") and tries again when space is back.
- **Published.** On the Direct connection the worker's published `limits.files` follow the ceiling (no 500 KB per-file or 2000 KB total limit) unless the owner set them in the registry; GitHub workers keep the old defaults. The client pre-checks against the published limits, so an oversized set is refused before anything is sent. The ceiling is also in the state document (`observed.job_ceiling_mb`) and shown as "Largest job" in `arm-admin workers`.
- **Waiting jobs are cheap.** When a job is queued the worker writes a small `.meta` sidecar (class, packages, time limit), so every step pass reads that instead of parsing the whole job.

Not in this slice: the 1 GB notice and pre-job summary, archive transfer, binary files, streaming files to disk (which removes the memory term and the 1024 MB cap).
