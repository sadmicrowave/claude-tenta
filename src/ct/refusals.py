"""The refusal catalog: every rule that refuses a job on the job path, defined ONCE (scope: docs/design/REFUSAL-CATALOG.md).

A refusal is a typed thing, not a sentence written where it is raised. Each entry says what it is for, so a rule can be questioned against the app's objective instead of being accepted because the code has it:
  code      a stable name, `area.thing.problem`, shown to the person as the last row and carried in the published result
  kind      security (protects the worker or its owner), format (the job is malformed), limit (a size or count), placement (this worker cannot take it), policy (a choice)
  who       worker (decided on the worker, the authority), client (a courtesy check before sending) or both
  protects  what the rule is for, in one sentence, and from whom
  depends   what it depends on: nothing, a class kind, the transport, trust
  config    where the person can change it, or None
  title     a fixed sentence naming the refusal (the facts go in the text, never in the title)
  why       the text shown to the requester; {facts} are filled by `refuse`
  steps     what to do next, each saying what to open or run

Modules refuse with `raise refusals.refuse("code", fact=value)`. A test fails on a refusal written as a literal string in a module listed in tests/test_refusals.py.
Operational errors (a step that failed, a file that cannot be read) are not here: they follow the same method in a later task.
"""
from dataclasses import dataclass

KINDS = ("security", "format", "limit", "placement", "policy")
WHO = ("worker", "client", "both")


class Reject(Exception):
    """A job that must not run. The message is shown to the requester, so it never includes anything from the machine."""


@dataclass(frozen=True)
class Entry:
    code: str
    kind: str
    who: str
    protects: str
    title: str
    why: str
    depends: str = "nothing"
    config: str = ""
    steps: tuple = ()


CATALOG = {}


def define(code, kind, protects, title, why, *, who="worker", depends="nothing", config="", steps=()):
    assert code not in CATALOG, f"two refusals named {code}"
    CATALOG[code] = Entry(code, kind, who, protects, title, why, depends, config, tuple(steps))


class Refused(Reject, ValueError):
    """A refusal from the catalog. It is a Reject (the worker's catch) and a ValueError (the validators' catch), so every existing `except` still works; `str()` is the text shown to the requester."""

    def __init__(self, code, why, facts=None):
        super().__init__(why)
        self.code, self.facts = code, dict(facts or {})

    @property
    def entry(self):
        return CATALOG[self.code]


class _Facts(dict):
    def __missing__(self, key):
        return "{" + key + "}"                                        # a fact the caller did not give leaves its own name in the text: still a message, never a KeyError


def explain(code, **facts):
    """The text of an entry without raising it: for a rule that is not a refusal of the whole job (a file held back, a warning) but must still say what it protects and be listed."""
    return CATALOG[code].why.format_map(_Facts(facts))


def refuse(code, **facts):
    """The refusal to raise: `raise refuse("files.too_many", limit=200)`. An unknown code is a mistake in the code and fails loudly."""
    entry = CATALOG[code]
    return Refused(code, entry.why.format_map(_Facts(facts)), facts)


# ---- the job file itself ----
define("job.id.invalid", "format", "Keeps job ids plain so they are safe as file names and in logs.", "The job id is not allowed", "bad job id",
       steps=["Send the job again with ct-run submit, which makes a valid id."])
define("job.id.mismatch", "security", "Stops a job file from claiming to be a different job than its file name, so an approval or result cannot be attached to the wrong one.",
       "The job file's id does not match its name", "id missing or does not match filename", steps=["Send the job again with ct-run submit."])
define("job.json.invalid", "format", "A job that is not readable data cannot be checked, so it is never run.", "The job file is not valid JSON", "job file is not valid JSON",
       steps=["Send the job again with ct-run submit."])
define("job.created.invalid", "format", "The age check (replay protection) needs the job's own creation time.", "The job has no readable creation time", "missing or invalid 'created' timestamp",
       steps=["Send the job again with ct-run submit, which writes it."])
define("job.created.no_timezone", "format", "A creation time without a timezone cannot be compared with the worker's clock.", "The job's creation time has no timezone",
       "'created' needs a timezone (use UTC, e.g. 2026-01-01T00:00:00Z)", steps=["Send the job again with ct-run submit, which writes it in UTC."])
define("job.expired", "security", "Replay protection: a job file that was captured earlier cannot be run again later by whoever holds it.", "The job is too old to run",
       "job expired: it is more than {hours} old when the worker looked at it, so it is not run; send it again", config="the worker's max_age_s (default 24 hours)",
       steps=["Send the job again with ct-run submit."])
define("job.created.future", "security", "A job dated in the future would stay valid longer than the age limit allows.", "The job claims to be from the future",
       "job timestamp is in the future", steps=["Check this computer's clock, then send the job again with ct-run submit."])
define("job.class.unknown", "security", "Only classes the owner installed may run: a job can never name a command of its own, except a profile run, which needs approval.",
       "The job names a class the worker does not have", "class not in installed registry: {name!r}", steps=["List what the worker offers with ct-run classes."])
define("job.params.not_object", "format", "Parameters must be named values so each one can be checked against its declared type.", "The job's parameters are not an object",
       "params must be an object", steps=["Give each parameter as -p name=value on ct-run submit."])
define("job.invalid", "format", "A catch-all for a check that has not been given its own entry yet (these are being moved into the catalog).", "The job did not pass a check",
       "{detail}")

# ---- parameters ----
define("param.enum", "security", "A parameter that must be one of a fixed list can never carry anything else into the command.", "A parameter is not one of the allowed values",
       "{name}: must be one of {options}", steps=["See the allowed values with ct-run classes."])
define("param.int", "security", "A numeric parameter stays inside the range the class declares.", "A number parameter is out of range", "{name}: must be an integer in [{low}, {high}]",
       steps=["See the allowed range with ct-run classes."])
define("param.regex", "security", "A text parameter must match the pattern the class declares, so it cannot carry extra arguments or shell text into the command.",
       "A parameter does not match its allowed pattern", "{name}: does not match the allowed pattern", steps=["See the allowed pattern with ct-run classes."])
define("param.untyped", "format", "A class parameter with no declared type cannot be checked, so it is refused rather than passed through.", "A class parameter has no type",
       "{name}: registry spec has no type", steps=["Give the parameter a type in the class, with ct-admin class."])
define("param.policy", "security", "The owner's deny rules (patterns that must never reach a command) apply to every parameter.", "A parameter matches a deny rule", "not allowed: {reason}",
       config="the registry's policy: deny_params", steps=["Change the value, or review the rule in your registry (ct-admin class)."])
define("param.unknown", "format", "A parameter the class did not declare is never passed on.", "The job has a parameter the class does not declare", "unknown params: {names}",
       steps=["See the declared parameters with ct-run classes."])
define("param.missing", "format", "A required parameter that is absent would leave a placeholder in the command.", "A required parameter is missing", "missing param: {name}",
       steps=["Give it as -p {name}=value on ct-run submit."])
define("template.undeclared", "format", "A class whose command template names a parameter it never declared is a mistake in the class, so it is refused.",
       "The class's command uses an undeclared parameter", "template uses undeclared param {name}", steps=["Correct the class with ct-admin class."])

# ---- attached files ----
define("files.not_accepted", "policy", "Only classes that were registered to take input files get any, so files cannot influence a command that was not made for them.",
       "The class does not take files", "this job class does not accept files", depends="the class: accepts_files", steps=["See which classes take files with ct-run classes."])
define("files.too_many", "limit", "Bounds how much one job asks of the worker and of the channel that carries it.", "Too many files are attached", "files must be an object with at most {limit} entries",
       depends="the transport (these limits were written for GitHub)", config="the worker's registry: limits.files.max_count (moving to the transport config)",
       steps=["Attach fewer files with --include on ct-run submit."])
define("files.name.unsafe", "policy", "Keeps a file name inside the job's work folder. The character list goes beyond that: it was written as a defensive default, and it is under review.",
       "A file name is not allowed", "bad file name: {name!r}", depends="nothing (under review: only the path checks protect anything)",
       steps=["Rename the file, or leave it out with --include on ct-run submit."])
define("files.too_big", "limit", "Bounds one file's size so a single file cannot fill the job file.", "A file is too big", "file {name!r}: content must be text under {limit} KB",
       depends="the transport (written for GitHub)", config="the worker's registry: limits.files.max_file_kb (moving to the transport config)",
       steps=["Leave the file out with --include on ct-run submit."])
define("files.total_too_big", "limit", "Bounds the whole job's size for the channel that carries it.", "The attached files are too big together", "files total over {limit} KB",
       depends="the transport (written for GitHub)", config="the worker's registry: limits.files.max_total_kb (moving to the transport config)",
       steps=["Attach only the files the command needs with --include on ct-run submit."])

# ---- a profile run (one exact command) ----
define("profile.argv.shape", "limit", "A command is a short list of plain words, so it can be shown whole to the person who approves it and cannot hide NUL bytes.",
       "The command is not a list of 1 to 40 words", "argv must be a list of 1-40 non-empty strings", steps=["Give the command after a double dash on ct-run offload profile."])
define("profile.command.denied", "security", "A profile run is an arbitrary command, so privilege tools are refused outright whatever else is allowed.", "The command is not allowed in a profile run",
       "{program} is not allowed in a profile run", config="the registry's policy: deny_commands")
define("profile.packages.path", "format", "With packages the program comes from the throwaway folder, so a path to a program on the worker would be ambiguous.",
       "A command with packages must be given by name", "with packages, give the command by name (the program comes from the throwaway folder)",
       steps=["Give the command by name, without a path, on ct-run offload profile."])
define("profile.command.path_invalid", "security", "A command with a path must be one exact, absolute, executable file, never a relative path the job could place.",
       "The command's path is not an absolute executable", "a command with a path must be an absolute path to an executable", steps=["Give the full path, or the command's name."])
define("profile.command.not_installed", "placement", "The worker can only run what it has; it says so instead of failing in the middle of the run.", "The worker does not have that command",
       "{program!r} is not installed on the worker (looked in {looked}); if a package provides it, list the package (packages: ['apt:<name>'])",
       steps=["Name the package with --package on ct-run offload profile."])
define("profile.field.invalid", "format", "The description fields are what the person who approves reads, so they must be present and short enough to read.",
       "A description field is missing or too long", "{field}: {rule}", depends="a profile run that needs approval", steps=["Fill it in with the matching option on ct-run offload profile."])
define("profile.estimate.required", "format", "The estimates are compared with what the run measured, so the worker's limits and the verdict can be set from them.",
       "An estimate is missing", "{field}: required (your honest estimate, as a number)", steps=["Give it with the matching option on ct-run offload profile."])
define("profile.estimate.range", "limit", "An estimate must be a plausible number so a typo cannot set an absurd limit.", "An estimate is out of range",
       "{field}: must be a number from 0 to 1000000", steps=["Give a number with the matching option on ct-run offload profile."])
define("profile.flags.invalid", "limit", "The flag explanations are shown on the approval screen, which has room for a few short ones.", "The flag explanations are not usable",
       "flags must map each flag to a short explanation (at most 20)", depends="a profile run that needs approval", steps=["Shorten them with --flag on ct-run offload profile."])
define("profile.timeout.range", "limit", "A profile run's time limit stays between a floor that lets it start and the cap the owner set.", "The time limit is out of range",
       "timeout_s must be an integer from 10 to {cap}", config="the class's max_timeout_s", steps=["Give a value in range with --timeout on ct-run offload profile."])

# ---- placement: why a worker cannot take a job, and why a job cannot be placed (the client decides; every reason is reported, not the first) ----
define("placement.unreachable", "placement", "A worker that cannot be reached cannot be asked anything, so nothing else about it can be known: this reason stands alone.", "A worker cannot be reached",
       "cannot be reached now ({reason})", who="client", steps=["Check it with ct-admin check."])
define("placement.no_state", "placement", "A worker that has published no state cannot be described, so no job is placed on it blind.", "A worker has published no state",
       "has published no state (an older release, or not set up yet), so nothing can be said about it", who="client", steps=["Update it with ct-admin apply."])
define("placement.locked", "placement", "The worker locked itself after a security event and runs nothing until the owner has looked.", "A worker is locked",
       "is LOCKED (a security lockdown): unlock it after you have looked", who="client", steps=["Look at what happened with ct-admin quarantine, then run ct-admin unlock."])
define("placement.isolation_off", "placement", "A job runs only where its isolation is on.", "A worker's job isolation is off", "has its job isolation OFF", who="client",
       steps=["See why with ct-admin status."])
define("placement.reserved", "placement", "The owner reserved this worker for certain classes, and it takes nothing else.", "A worker is reserved for other work", "is reserved for {classes}",
       who="client", config="ct-admin workers use WORKER --command CLASS", steps=["Release it with ct-admin workers use WORKER --clear."])
define("placement.class_missing", "placement", "A worker can only run the classes its registry has.", "A worker does not offer the class", "does not offer the class {cls}", who="client",
       steps=["See which workers offer it with ct-run classes."])
define("placement.tools.full_isolation", "placement", "A class that needs the tool folders already on a worker runs only where the owner allowed that.", "A worker's isolation cannot show the tool folders",
       "is in full isolation, so it cannot show the tool folders {cls} needs", who="client", steps=["Allow the tool folders with ct-admin class."])
define("placement.tools.not_allowed", "placement", "A class that needs a tool folder runs only where the owner allowed that folder.", "A worker does not allow the tool folder",
       "does not allow the tool folder {folder} that {cls} needs", who="client", steps=["Allow the folder with ct-admin class."])
define("placement.connection", "placement", "The owner limited this class to some kinds of connection.", "A worker is reached over a connection the class does not allow",
       "is reached over {listens}, and {cls} is limited to {allowed}", who="client", depends="the transport", config="ct-admin class", steps=["See the limits with ct-admin class."])
define("placement.ai.no_signin", "placement", "An AI class runs only where the AI tool is signed in.", "A worker has no AI sign-in", "has no Claude sign-in", who="client", depends="the class kind: claude",
       steps=["Sign it in with ct-setup install --worker NAME."])
define("placement.ai.no_tool", "placement", "An AI class runs only where its tool is installed.", "A worker has no AI tool", "has no Claude tool installed", who="client", depends="the class kind: claude",
       steps=["Install it with ct-setup install --worker NAME."])
define("placement.ai.paused", "placement", "An AI worker stops taking AI jobs for the day when its limit is reached, so the person's usage is bounded.", "A worker paused AI jobs for today",
       "has paused Claude jobs for today (its daily limit)", who="client", depends="the class kind: claude", config="the worker's daily limits", steps=["See its usage with ct-admin status."])
define("placement.none_available", "placement", "A job with no worker that can take it is not sent: the person is told why for each worker.", "No worker can take this job right now",
       "Each one, and why it cannot:", who="client", steps=["Compare the workers with ct-admin workers."])
define("placement.unknown_worker", "placement", "A job sent to a named worker needs the name to be one of the person's workers.", "There is no such worker",
       "Workers are the main one and those in ct-admin workers.", who="client", steps=["List your workers with ct-admin workers."])
define("placement.pin.unknown_worker", "placement", "A folder's .tenta.toml that names a worker which is not the person's would send jobs nowhere.", "The folder file sends jobs to a worker that is not yours",
       "The worker named in the file is not one of your workers.", who="client", steps=["Send this folder's jobs to one of your workers: ct-run use WORKER", "ct-run use --clear"])
define("placement.capacity.in_flight", "limit", "Bounds how many jobs this computer has running across all workers at once.", "Too many jobs are already running",
       "Sending {adding} more would pass limits.max_in_flight. In flight now: {where}.", who="client", config="your configuration: [limits] max_in_flight (0 removes it)",
       steps=["Wait for some to finish: ct-run jobs shows them.", "Or raise the cap in your configuration under [limits]: max_in_flight = <number> (0 removes it)."])
define("placement.locked.submit", "placement", "A worker that locked itself after a security event takes no new job, so nothing is sent to it.", "The worker is locked, so this job was not submitted",
       "It locked itself after a security event and runs nothing until you have reviewed it.", who="client", steps=["First look at what happened, then unlock it:"])

# ---- what the worker decides about the kind of class ----
define("job.profile.params", "format", "A profile run is one exact command, not a class with parameters.", "A profile run was given parameters", "profile-run takes argv, not params",
       steps=["Give the command after a double dash on ct-run offload profile."])
define("job.action.missing", "security", "A root-level class must point at an action in the root-owned registry, or it would run nothing it could be checked against.",
       "The class's root-level action is missing", "the class's action is not in the installed registry", steps=["Reinstall the registry with ct-admin apply."])
define("job.root.params_only", "security", "A root-level job may carry only typed parameters: no files and no prompt can reach something that runs as root.",
       "A root-level job carries more than parameters", "a root-level job takes parameters only")
define("job.prompt.length", "limit", "Bounds what one AI job can spend; the cap is a fixed number today and should become a setting per class.", "The prompt has the wrong length",
       "prompt must be 1-8000 characters", depends="the class kind: claude", steps=["Shorten the prompt, or attach the text as a file."])
define("job.kind.unknown", "format", "A class of a kind this worker does not know cannot be run safely.", "The class is of an unknown kind", "unknown kind {kind!r}",
       steps=["Update the worker with ct-admin apply."])
define("job.packages.plan", "placement", "The packages a job asks for must be installable here, or the job would start and fail.", "The requested packages cannot be installed", "{problems}",
       steps=["Check the package names with ct-run offload profile."])
