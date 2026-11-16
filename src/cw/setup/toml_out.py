"""Write a cw.toml from answers. Only settings that differ from the built-in default are written, each with its one-line explanation,
so the file stays short and a later change of default in a new release reaches everyone who did not choose a value."""
import json

from .. import config


def _value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    return json.dumps(v)          # a TOML basic string is a JSON string for everything the schema allows


def dump(answers, header="Written by cw-setup. Change a value here, or run cw-setup again. Secrets never go in this file (see secrets.env).", full=False):
    """answers: {section: {key: value}} (already validated). Returns the TOML text; round-trips through config.validate."""
    out = [f"# {header}"]
    for section, fields in config.SCHEMA.items():
        lines = []
        for key, (default, _check, doc) in fields.items():
            if key not in (answers.get(section) or {}):
                continue
            v = answers[section][key]
            if full:                           # every answer, even one equal to the default: used to hand a complete set of answers to another machine
                lines.append(f"{key} = {_value(v)}")
                continue
            if v == default or (isinstance(default, str) and "{" in default):      # a templated default ({name}) is only written when changed
                expanded = default.replace("{name}", answers.get("identity", {}).get("name", "claude-worker")).replace(
                    "{worker_user}", answers.get("server", {}).get("worker_user", "cwworker")) if isinstance(default, str) else default
                if v == expanded:
                    continue
            lines.append(f"{key} = {_value(v)}    # {doc}")
        if lines:
            out += ["", f"[{section}]"] + lines
    return "\n".join(out) + "\n"
