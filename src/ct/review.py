"""The approval screen: what a person reads before allowing a profile run.

One layout for both places it is shown (on the client and on the worker). Plain text first, so it can be captured and diffed; colour is
added only for a terminal. Every claim the requester makes is labelled as theirs, and what the worker itself looked up is labelled
separately, so the two cannot be confused.
"""
import re
import shlex
import shutil
import sys
import textwrap

from .ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, _color_on, paint

REVIEW_LABELS = ("WHAT IT DOES", "WHY NOW", "WHAT TO EXPECT", "WHAT IT READS, WRITES AND CONTACTS", "BAD SIGNS TO WATCH FOR", "FLAGS",
                 "INPUT FILES", "TIME LIMIT")
FACT_NAMES = {"binary": "Program", "size_kb": "Size", "package": "Package", "summary": "Summary"}


def _labels(product):
    return REVIEW_LABELS + (f"LOOKED UP BY {product.upper()} ITSELF",)


def review_text(job, facts=None, package_text=None, glance=None, width=None, product="the worker"):
    """The approval screen as plain text: every label on its own line, its content indented under it, one blank line between sections,
    input files as an aligned vertical list, and any WARNING set apart at the end."""
    width = width or max(60, min(shutil.get_terminal_size((100, 24)).columns, 110))
    argv = [str(x) for x in job.get("argv", [])]
    L = [f"PROFILE RUN. If you approve, {product} will run exactly this command once, as its unprivileged",
         "worker user, in an empty folder, with hard limits on time, memory, heat and disk:", "",
         "    " + " ".join(shlex.quote(x) for x in argv), ""]

    def section(label, lines, note="", as_list=False):
        L.append(label.upper() + (f"  {note}" if note else ""))
        if as_list:
            L.append("")
        L.extend(lines)
        L.append("")

    def prose(text):
        return textwrap.wrap(str(text), width) or [""]      # nochip: the requester's own words about its job: data to read, never rewritten
    for label, key in (("What it does", "what"), ("Why now", "why"), ("What to expect", "expect"),
                       ("What it reads, writes and contacts", "side_effects"), ("Bad signs to watch for", "watch_for")):
        if job.get(key):
            section(label, prose(job[key]))
    flags = job.get("flags") or {}
    if flags:
        fw = max(len(k) for k in flags)
        rows = []
        for k, v in flags.items():
            wrapped = textwrap.wrap(str(v), max(30, width - fw - 4)) or [""]      # nochip: the requester's own words
            rows.append(f"{k:<{fw}}  {wrapped[0]}")
            rows += [" " * (fw + 2) + x for x in wrapped[1:]]
        section("Flags", rows, as_list=True)
    missing = [t for t in argv[1:] if t.startswith("-") and t.split("=")[0] not in flags]
    if missing:
        section("NOT EXPLAINED by the requester", [" ".join(missing)])
    files = job.get("files") or {}
    if files:
        nw = max(len(k) for k in files)
        sizes = {k: f"{len(v):,} B" for k, v in files.items()}
        sw = max(len(s) for s in sizes.values())
        total = sum(len(v) for v in files.values())
        section("Input files", [f"{k:<{nw}}  {sizes[k]:>{sw}}" for k in sorted(files)],
                f"({len(files)} file{'s' if len(files) != 1 else ''}, {total:,} B in all)", as_list=True)
    if job.get("packages"):
        L += [package_text or (f"INSTALL REQUIRED. This command needs packages: {', '.join(job['packages'])} ({product} has not published the plan yet)"), ""]
    if job.get("timeout_s"):
        section("Time limit", [f"{job['timeout_s']} s"])
    if facts:
        rows = []
        for k, v in facts.items():
            if k == "interpreter":
                continue
            v = " ".join(str(v).split())                               # collapse the padding the man page summary carries
            rows.append((FACT_NAMES.get(k, k.replace("_", " ").capitalize()), f"{v} KB" if k == "size_kb" else v))
        kw = max([len(r[0]) for r in rows] or [0])
        section(f"Looked up by {product} itself", [f"{k:<{kw}}  {v}" for k, v in rows], "(not from the requester)", as_list=True)
        if facts.get("interpreter"):
            L.append("")                                              # an extra line break: the warning stands apart
            section("WARNING", prose("This is an interpreter or wrapper; its arguments ARE the program. Read them carefully."))
    else:
        L += [f"({product} has not published its own facts about this command yet; run again in a minute for them.)", ""]
    if glance:
        L += [glance, ""]
    return "\n".join(L).rstrip("\n") + "\n"


def color_review(text, product="the worker"):
    """Colour the approval screen: the command that runs, labels, installs and problems. Plain when not a terminal."""
    P = lambda t, *c: paint(t, *c, stream=sys.stdout)       # noqa: E731
    if not _color_on(sys.stdout):
        return text
    labels = _labels(product)
    out, in_cmd = [], False
    cmd_re = re.compile(r"^    (apt-get|dpkg-deb|npm|python3 -m pip) ")
    pkg_re = re.compile(r"^    (apt|npm|pip): ")
    for line in text.split("\n"):
        label = next((l for l in labels if line == l or line.startswith(l + "  (")), None)
        if line.startswith("PROFILE RUN"):
            line, in_cmd = P(line, BOLD), True
        elif line.startswith("RUNS AS ROOT"):
            line = P(line, BOLD, "root")                          # its own colour: it must not look like any other warning
        elif line.startswith("INSTALL REQUIRED"):
            line = P(line, BOLD, YELLOW)
        elif line.startswith(("PROBLEMS", "WARNING")):
            line = P(line, BOLD, RED)
        elif line.startswith("NOT EXPLAINED"):
            line = P(line, BOLD, YELLOW)
        elif line.startswith("AT A GLANCE"):
            line = P(line, BOLD, CYAN)
        elif line.startswith("Already installed"):
            line = P(line, DIM)
        elif label:
            line = P(label, BOLD, CYAN) + P(line[len(label):], DIM)   # cyan label; a trailing note in dim
        elif cmd_re.match(line):
            line = P(line, CYAN)
        elif pkg_re.match(line):
            line = P(line, BOLD, YELLOW)
        elif in_cmd and line.startswith("    "):
            line, in_cmd = P(line, BOLD, GREEN), False                # the exact command that will run
        out.append(line)
    return "\n".join(out)


def text_card(jid, text, product="the worker", digest=None, kind="REVIEW"):
    """The approval screen as a card (see ct.cards) for `ui.say`: the job id as its subject, the review text inside it as it is, and the digest the approval is pinned to.
    A profile run executes a command nobody has seen, so it is drawn in the caution outline; anything else gets the plain rail."""
    from . import cards, ui
    caution = text.startswith("PROFILE RUN") or kind == "PROFILE RUN"
    rows = [cards.Block(text.split("\n"))] if text else []
    if digest:
        rows += [("Digest", f"{digest[:12]}...  approval is pinned to this exact file")]
    return dict(title=jid, details=rows, tag="PROFILE RUN" if caution else kind, color=ui.YELLOW if caution else ui.PRIMARY)
