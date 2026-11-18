# Finding a worker again after its address changed

Status: **started** (2026-11-18). Today a worker is found by the name or address in its configuration (`server.host`). A name such as `box.example` stops working when the machine gets a new
address and the network's name service is slow to follow (seen: a stale answer cost 30 s per connection); a raw address stops working the moment the router hands out another one.

## What identifies a worker

Not its name and not its address: its **SSH host key**, which this computer already recorded the first time it connected (`known_hosts`). A machine that answers on the SSH port and shows
that same key is that worker, wherever it now is. mDNS can be forged by anyone on the network, so a name is only ever a *hint*; the key is the proof, and a match is never accepted without it.

## The design

1. **`ct-admin locate [worker]`** (read-only): when the configured name or address does not answer, scan the networks this computer is on (the `ct-setup add-worker` scan: private ranges only,
   short connections to the SSH port), read the host keys of what answers (`ssh-keyscan`, nothing logged in to), and report the machine whose key is the recorded one. Several matches
   (a cloned machine shares its key) or none are reported as such, never guessed.
2. **Offer to save it** (asks first): the new address replaces `server.host` in that worker's configuration, with the old value kept in a comment so it can be undone.
3. **Say it where the user is already looking:** the "worker unreachable" message of every command names `ct-admin locate` as the next step, and `ct-admin workers` shows "not at its address; found at X" when the scan was run.
4. **mDNS as a hint, from this computer only:** the names announced on the network (already read by the scan) are shown beside what was found, and make the scan faster by putting the likely
   address first. No change is made to the worker for this.

## Decision to make: should the worker also announce itself?

Announcing (a small avahi service file with the install name) would make a worker findable by name before any key check. It needs the worker to run `avahi-daemon` and to accept UDP 5353 from
the network, which is one more listening service on a machine whose design goal is "nothing but SSH listens", and the scan above finds the worker without it. **Recommendation: do not announce;
revisit only if scanning proves too slow on big networks.** Left to the owner.

## Tests (each fails first)

- Parsing recorded `known_hosts` lines and `ssh-keyscan` output (fixtures recorded from the real tools on every supported release).
- The matcher: exactly one match, none, several, a key of another type, a hashed `known_hosts` entry.
- The routes: the name stops resolving (stale) and the address changes; the worker is unreachable and the key matches elsewhere; a different machine at the old address (key mismatch) is refused.
