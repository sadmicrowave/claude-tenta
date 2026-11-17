"""What the installed Claude tool can do, read from its own help text: a device must be able to restrict a Claude job to exactly the tools its class names, and to refuse to
start helper agents. An older pinned release may lack a flag; then the class refuses to run, with a plain message, instead of running with more power than it was given."""
import subprocess


def flags(claude_command, run=subprocess.run):
    """The set of option names (--tools, --disallowedTools ...) the tool prints in its help, or an empty set when it cannot be asked."""
    try:
        r = run([claude_command, "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    text = (r.stdout or "") + (r.stderr or "")
    return {w.rstrip(",") for w in text.split() if w.startswith("--")}
