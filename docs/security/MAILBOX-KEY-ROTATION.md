# Replacing a Direct worker's mailbox key

`ct-setup rotate-key [--worker NAME]` (preview by default, `--apply` to do it, `--yes` to skip the question). The mailbox key is the only thing that lets a computer queue jobs on a Direct worker, so a lost or copied key must be replaceable without touching anything else (spot-check finding F4).

The order is what keeps you from being locked out:

1. A new key is made beside the old one (`key.new`); the old one is untouched.
2. The worker is told to trust **only** the new key (Linux: the worker half, `ct-setup rotate-key --here`, runs as root and rewrites the mailbox's authorized key; Windows: the same mailbox script the setup uses). If this fails, the new key is deleted and nothing changed.
3. The local key file is swapped. From here the worker accepts only the new key, so it is the one in use whatever happens next; the old key is kept as `key.old`.
4. The new key is checked (`git ls-remote` of the worker's queue). If it works, the old key is deleted. If the check fails, the old key stays aside, and the message says the worker now trusts only the new key.

Only the Direct connection has a mailbox key; any other connection is refused with a plain message. The key has no passphrase by design (it has no other use and the mailbox shell it opens runs only git on two repositories).

Not covered: a worker that is unreachable at step 2 is reported and left alone (run the command again); a key copied before the rotation is useless afterwards, but nothing here finds out whether one was used.
