"""The worker's side of root-level actions: ask the root helper (ct.helper) over its unix socket. The worker never runs these itself."""
import base64
import json
import socket


def call(sock_path, job_bytes, approval_text, timeout=60, connect=socket.socket):
    """Returns the helper's answer {status, note, exit_code, output}. A helper that is not there is an error answer, not an exception."""
    body = json.dumps({"job_b64": base64.b64encode(job_bytes).decode(), "approval": approval_text}).encode()
    s = connect(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(body)
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except FileNotFoundError:
        return {"status": "error", "note": "the root helper is not running on this worker (it starts when the registry has a root-level action)", "output": ""}
    except (OSError, socket.timeout) as e:
        return {"status": "error", "note": f"the root helper could not be reached ({getattr(e, 'strerror', None) or type(e).__name__})", "output": ""}
    finally:
        s.close()
    try:
        out = json.loads(data.decode("utf-8", "replace"))
        return out if isinstance(out, dict) and "status" in out else {"status": "error", "note": "the root helper's answer was not understood", "output": ""}
    except ValueError:
        return {"status": "error", "note": "the root helper's answer was not understood", "output": ""}
