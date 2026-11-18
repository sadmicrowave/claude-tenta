# Finding a worker again after its address changed

Status: **started** (2026-11-18). Today a worker is found by the name or address in its configuration (`server.host`). A name such as `box.example` stops working when the machine gets a new
address and the network's name service is slow to follow (seen: a stale answer cost 30 s per connection); a raw address stops working the moment the router hands out another one.

## What identifies a worker

Not its name and not its address: its **SSH host key**, which this computer already recorded the first time it connected (`known_hosts`). A machine that answers on the SSH port and shows
that same key is that worker, wherever it now is. mDNS can be forged by anyone on the network, so a name is only ever a *hint*; the key is the proof, and a match is never accepted without it.

## The design: a ladder, cheapest proof first

A scan of every network takes about 14 seconds, which is fine once, when a person asked, and wrong for a monitoring pass. So the search is a ladder that stops at the first rung whose
candidate shows the recorded key. A rung that finds only other machines is not a failure; the next rung runs.

1. **The configured name or address** (one key read, under a second).
2. **The last address that worked.** Every successful monitoring pass and `ct-admin locate` writes the worker's address and its network card's address (from the neighbour table) to `last-seen.json` in the client home.
3. **The name's multicast answer** (`avahi-resolve` / `dns-sd`, 1 to 2 seconds), from this computer only. A hint: the key still decides.
4. **The neighbour table entry for the same network card**: the machine has just talked to the router, so it is very likely listed under its new address.
5. **A scan, last, and only when a person asked** (`ct-admin locate`): the real networks first, the virtual ones inside this computer (VM and container bridges, most of the addresses and the least likely place) only if needed.

`ct-admin watch` runs rungs 1 to 4 only. It never scans. A worker that does not answer gets an alert that says where it answers now, or that `ct-admin locate --worker NAME` looks for it. Only a
worker reached by address (Direct) can have moved; a GitHub worker does not use one.

`ct-admin locate [--worker NAME]` reports the machine whose key matches, says which rung found it, and offers to save it: the new address replaces `server.host` (asks first; the old value stays in a comment),
and this computer's copies of the queue and results are pointed at it. Several matches (a cloned machine shares its key) or none are reported as such, never guessed.

Not done: stopping the scan itself at the first match (it still reads every address of a network before the keys are checked).

## Decision to make: should the worker also announce itself?

Announcing (a small avahi service file with the install name) would make a worker findable by name before any key check. It needs the worker to run `avahi-daemon` and to accept UDP 5353 from
the network, which is one more listening service on a machine whose design goal is "nothing but SSH listens", and the scan above finds the worker without it. **Decided 2026-11-18: the worker does not announce.** Revisit only if scanning proves too slow on big networks.
