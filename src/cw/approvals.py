"""Approving a job from your own computer, without logging in to the worker.

A job of a class that needs approval waits until the administrator says yes to that exact job file. Doing that over SSH meant a login and a sudo password on
every worker, which does not scale to several workers. Instead the administrator SIGNS the approval on their own computer, with a key that exists for this
one purpose, and publishes it next to the job. The worker checks the signature against the public key it was set up with (a root-owned file nobody on GitHub
can change), and accepts only an approval that names this job id, this exact file (its SHA-256), and has not expired.

So GitHub access alone can never approve anything: whoever can push a job to the queue repository can ask, but cannot answer.

The signature is an ordinary SSH signature (`ssh-keygen -Y sign`), so there is nothing new to install on either side.
"""
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time

NAMESPACE = "cw-approval"                 # signatures that approve one job
REGISTRY_NAMESPACE = "cw-registry"        # signatures that approve a version of the job registry (what the worker may be asked to run)
REGISTRY_PATH = "registry/registry.json"
PRINCIPAL = "approver"
BRANCH = "approvals"                      # a branch of the queue repository: approvals/<job id>.json
TTL_S = 7 * 86400                         # an approval not acted on within a week is no longer good
SKEW_S = 300                              # tolerated clock difference between the two computers


def path_in_repo(jid):
    return f"approvals/{jid}.json"


def make_record(jid, digest, now=None, ttl=TTL_S):
    """The statement being signed, as exact text (the signature covers these bytes, so they travel as a string)."""
    now = int(time.time() if now is None else now)
    return json.dumps({"v": 1, "job": jid, "sha256": digest, "issued": now, "expires": now + ttl}, sort_keys=True, separators=(",", ":"))


def bundle(record, signature):
    return json.dumps({"record": record, "signature": signature}, indent=1) + "\n"


def sign(record, key_path, run=subprocess.run, namespace=NAMESPACE):
    """The SSH signature of `record` made with the private key at `key_path` (ssh-keygen asks for its passphrase on the terminal if it has one)."""
    r = run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key_path), "-n", namespace], input=record, capture_output=True, text=True)
    if r.returncode != 0 or "BEGIN SSH SIGNATURE" not in (r.stdout or ""):
        raise ValueError("could not sign the approval: " + ((r.stderr or r.stdout or "ssh-keygen failed").strip()[-200:]))
    return r.stdout


def verify(record, signature, signers_file, run=subprocess.run, namespace=NAMESPACE):
    """True only when `signature` is a valid signature of exactly `record` by a key listed in `signers_file` for this namespace."""
    with tempfile.NamedTemporaryFile("w", suffix=".sig", delete=False) as f:
        f.write(signature)
        sig_path = f.name
    try:
        r = run(["ssh-keygen", "-Y", "verify", "-f", str(signers_file), "-I", PRINCIPAL, "-n", namespace, "-s", sig_path], input=record, capture_output=True, text=True)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.unlink(sig_path)


def check(text, jid, digest, signers_file, now=None, run=subprocess.run):
    """(True, '') when `text` (an approvals/<id>.json file) is a good approval of exactly this job file; else (False, the reason)."""
    now = int(time.time() if now is None else now)
    try:
        doc = json.loads(text)
        record, signature = doc["record"], doc["signature"]
        rec = json.loads(record)
    except (ValueError, KeyError, TypeError):
        return False, "not a valid approval file"
    if not isinstance(rec, dict) or rec.get("v") != 1:
        return False, "unknown approval version"
    if rec.get("job") != jid:
        return False, "approval is for a different job"
    if str(rec.get("sha256", "")).lower() != digest.lower():
        return False, "approval is for a different version of the job file"
    try:
        issued, expires = int(rec["issued"]), int(rec["expires"])
    except (KeyError, ValueError, TypeError):
        return False, "approval has no valid times"
    if issued > now + SKEW_S:
        return False, "approval is dated in the future"
    if expires < now:
        return False, "approval has expired"
    if not os.path.isfile(signers_file):
        return False, "no approver key is installed on this worker"
    if not verify(record, signature, signers_file, run=run):
        return False, "signature does not match the approver key"
    return True, ""


def make_registry_record(registry_sha256, now=None):
    """What is signed to approve a registry: the digest of the exact registry.yaml text, and when. No expiry (it stands until a newer one replaces it), but the
    worker refuses one older than what it already installed, so an old, looser registry cannot be put back."""
    return json.dumps({"v": 1, "kind": "registry", "sha256": registry_sha256, "issued": int(time.time() if now is None else now)}, sort_keys=True, separators=(",", ":"))


def check_registry(text, registry_text, signers_file, now=None, not_before=0, run=subprocess.run):
    """(True, '', record) when `text` is a good approval of exactly this registry.yaml text and no older than `not_before`; else (False, why, None)."""
    now = int(time.time() if now is None else now)
    try:
        doc = json.loads(text)
        record, signature = doc["record"], doc["signature"]
        rec = json.loads(record)
    except (ValueError, KeyError, TypeError):
        return False, "not a valid registry approval file", None
    if not isinstance(rec, dict) or rec.get("v") != 1 or rec.get("kind") != "registry":
        return False, "not a registry approval", None
    if str(rec.get("sha256", "")).lower() != hashlib.sha256(registry_text.encode("utf-8")).hexdigest():
        return False, "the signed approval is for a different registry.yaml than the one on the queue repository", None
    try:
        issued = int(rec["issued"])
    except (KeyError, ValueError, TypeError):
        return False, "approval has no valid time", None
    if issued > now + SKEW_S:
        return False, "approval is dated in the future", None
    if issued < not_before:
        return False, "approval is older than the registry already installed (a rollback is refused)", None
    if not os.path.isfile(signers_file):
        return False, "no approver key is installed on this worker", None
    if not verify(record, signature, signers_file, run=run, namespace=REGISTRY_NAMESPACE):
        return False, "signature does not match the approver key", None
    return True, "", rec


def fingerprint(pubkey):
    """The usual `SHA256:...` fingerprint of an SSH public key line, or None."""
    parts = (pubkey or "").split()
    try:
        blob = base64.b64decode(parts[1] if parts[0].startswith(("ssh-", "ecdsa-", "sk-")) else parts[3], validate=True)
    except (IndexError, ValueError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def fingerprints(signers_text):
    """The fingerprints of the keys in a worker's approvers file (lines of `approver namespaces="..." <type> <key>`)."""
    out = []
    for line in (signers_text or "").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            fp = fingerprint(line)
            if fp:
                out.append(fp)
    return out


def signers_line(pubkey):
    """The line for the worker's approvers file: `approver <type> <key>` (a comment on the public key is dropped)."""
    parts = pubkey.strip().split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise ValueError("that is not an SSH public key")
    return f'{PRINCIPAL} namespaces="{NAMESPACE},{REGISTRY_NAMESPACE}" {parts[0]} {parts[1]}'


def with_approver(existing, pubkey):
    """The approvers file text with exactly one approver: this key (it replaces any earlier one, so a lost key is retired by running setup again)."""
    kept = [l for l in (existing or "").splitlines() if l.strip() and not l.lstrip().startswith(PRINCIPAL + " ") and not l.startswith("#")]
    return "# Keys allowed to approve jobs from the administrator's own computer. Written by cw-setup; root-owned.\n" + "\n".join(kept + [signers_line(pubkey)]) + "\n"
