"""The refusal catalog: every rule that refuses a job on the job path, defined ONCE (scope: docs/design/REFUSAL-CATALOG.md).

A refusal is a typed thing, not a sentence written where it is raised. Each entry says what it is for, so a rule can be questioned against the app's objective instead of being accepted because the code has it:
  code      a stable name, `area.thing.problem`, shown to the person as the last row and carried in the published result
  kind      security (protects the worker or its owner), format (the job is malformed), limit (a size or count), placement (this worker cannot take it), policy (a choice),
            error (something went wrong or is not ready: the real cause and what to do, never a bare failure)
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

KINDS = ("security", "format", "limit", "placement", "policy", "error")
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
       "job timestamp is in the future", steps=["Run date to see this computer's clock, then send the job again with ct-run submit."])
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
       "The class's command uses an undeclared parameter", "template uses undeclared param {name}", steps=["Open the class with ct-admin class show CLASS and declare the parameter."])

# ---- attached files ----
define("files.not_accepted", "policy", "Only classes that were registered to take input files get any, so files cannot influence a command that was not made for them.",
       "The class does not take files", "this job class does not accept files", depends="the class: accepts_files", steps=["See which classes take files with ct-run classes."])
define("files.too_many", "limit", "Bounds how much one job asks of the worker and of the channel that carries it.", "Too many files are attached", "files must be an object with at most {limit} entries",
       depends="the transport (these limits were written for GitHub)", config="the worker's registry: limits.files.max_count (moving to the transport config)",
       steps=["Attach fewer files with --include on ct-run submit."])
define("files.name.path", "security", "Keeps a file inside the job's work folder: the name must be a relative path with no '..', empty part, control character or backslash, and a sane length. "
       "Nothing else about a name is checked on Linux and a Mac: an earlier character list refused names like helper@.service.in and protected nothing. A Windows worker also refuses what Windows reads as something else: a drive, a data stream, a device name, a trailing dot or space (the client applies the same rules before sending when the worker says it runs Windows, and the worker checks again where it writes).", "A file name could leave the job's folder",
       "bad file name: {name!r} ({reason})", steps=["Rename the file, or leave it out with --include on ct-run submit."])
define("files.name.hidden", "policy", "A class whose tool reads its settings from its working folder (an AI tool) must not get a settings file planted by a job, so such a class takes no name that starts with a dot.",
       "This kind of class takes no file whose name starts with a dot", "a name may not start with a dot for this kind of class: {name!r}",
       depends="the class kind: claude (decided by the kind, never by a setting the person makes)", steps=["Leave the file out with --include on ct-run submit."])
define("files.not_text", "limit", "A job file carries file contents as text, so a file that is not text cannot be sent yet. It is reported and never dropped silently; archive transfer is a later design.",
       "A file is not text", "file {name!r} is not text, so it cannot be sent in a job", who="client", depends="the carrier (text inside the job file)",
       steps=["Leave it out with --include on ct-run submit."])
define("files.unreadable", "format", "A file that cannot be read cannot be sent, and the person is told which one and why, not left to find a missing file in the result.",
       "A file could not be read", "file {name!r} could not be read: {reason}", who="client", steps=["Run ls -l on the file to see its permissions, then run the same command again."])
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
       "timeout_s must be an integer from 10 to {cap}", config="the class's max_timeout_s", steps=["Give a value in range with --timeout-s on ct-run offload profile.",
              "A command that needs longer than the cap cannot be measured whole: profile a smaller part of it, or ask the worker's owner to raise max_timeout_s for profile-run."])

# ---- placement: why a worker cannot take a job, and why a job cannot be placed (the client decides; every reason is reported, not the first) ----
define("placement.unreachable", "placement", "A worker that cannot be reached cannot be asked anything, so nothing else about it can be known: this reason stands alone.", "A worker cannot be reached",
       "cannot be reached now ({reason})", who="client", steps=["Run ct-admin check to see why it does not answer."])
define("placement.no_state", "placement", "A worker that has published no state cannot be described, so no job is placed on it blind.", "A worker has published no state",
       "has published no state (an older release, or not set up yet), so nothing can be said about it", who="client", steps=["Update it with ct-admin apply."])
define("placement.silent", "placement", "A worker that has not published for a day may be off, broken or cut off, so no new job goes to it until it does.", "A worker has gone silent",
       "has published nothing for {age} (its latest reading is older than health.silent_hours), so no job is placed on it", who="client", config="the client setting health.silent_hours (default 24 hours)",
       steps=["Look at the computer: is it on and on the network?", "It is used again as soon as it publishes a new reading."])
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
define("placement.transport.mismatch", "placement", "A job sent over a connection the worker does not listen on is never picked up (this computer pushes to GitHub, the worker reads its own mailbox, or the other way round), so it is not sent: "
       "the worker is left out, and a worker named with --worker is refused, instead of the job disappearing.", "This computer and the worker use different connections",
       "this computer sends over {connection}, but the worker listens on {listens}, so a job sent now would never be picked up", who="client", depends="the transport",
       steps=["Point this computer at the worker again: ct-setup install --worker NAME", "Or move the worker to the connection this computer uses: ct-setup transport direct --worker NAME (or github)"])
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
       steps=["Name each package like apt:jq, npm:eslint or pip:requests on ct-run offload profile."])

# ---- what the client does before sending ----
define("files.incomplete", "policy", "A test or any job run on part of the files answers a question about something else, so the person decides whether to go on without the files that could not be sent.",
       "Some files could not be sent", "{count} could not be sent, so the job would run on an incomplete set of files", who="client",
       steps=["Make the files listed above sendable or leave them out with --include, then run the same command again.", "Or run the same command again with --send-incomplete to send the rest."])

# ---- what the person types or sends from this computer (the client decides) ----
define("files.set.too_big", "limit", "Bounds the size of one job for the channel that carries it: a set over the limit is never split. Written for GitHub; the Direct transport will not have it.", "These files do not fit in one job",
       "{count} / {kb} KB is over the {limit} KB this worker accepts in one job, and a bigger tree is never split. Attach only the files the command needs (--include 'src/**/*.js').", who="client",
       depends="the transport (written for GitHub)", config="the worker's registry: limits.files.max_total_kb (moving to the transport config)", steps=["Attach fewer files with --include on ct-run submit."])
define("files.set.not_batchable", "limit", "A class that needs its files together cannot be split across jobs, so a set over the count limit cannot be sent.", "These files do not fit in one job",
       "{count} is over the {limit}-file limit, and this class needs its files together so it cannot be split. Attach only the files it needs (--include), or run it yourself.", who="client",
       depends="the transport (written for GitHub) and the class: batchable", config="the worker's registry: limits.files.max_count", steps=["Attach fewer files with --include on ct-run submit."])
define("files.set.batches", "limit", "Bounds how many jobs one set of files may become.", "These files do not fit in one job", "{count} would need {jobs} (limit {limit}). Attach fewer files (--include).", who="client",
       depends="the transport (written for GitHub)", config="the worker's registry: limits.files.max_batches", steps=["Attach fewer files with --include on ct-run submit."])
define("files.set.profile_too_many", "limit", "Bounds the input files of one profile run for the channel that carries them.", "Too many input files", "One job can carry at most {files} and {kb} KB of text.", who="client",
       depends="the transport (written for GitHub)", config="the worker's registry: limits.files", steps=["Attach only the files the command needs: --include 'src/**/*.js'"])
define("param.unreadable", "format", "A job parameter is a name and a value, so each one can be checked against its declared type.", "Unreadable -p value", "Write each job parameter as a name, an equals sign, then a value.",
       who="client")
define("profile.flag.unreadable", "format", "A flag explanation is a flag and what it means, shown to the person who approves.", "Unreadable --flag", "Write each flag as the flag, an equals sign, then what it means.", who="client")
define("profile.package.unreadable", "format", "A package is named by where it comes from and what it is called, so the worker knows how to fetch it.", "Unreadable --package",
       "Start with where the package comes from, then a colon, then its name.", who="client", steps=["apt:openssh-client", "npm:eslint   or   npm:eslint@8", "pip:requests   or   pip:requests==2.31.0"])
define("profile.spec.line", "format", "A description file is lines of key: value.", "A line is not `key: value`", "Write each line of the description file as a key, a colon, then the value.", who="client")
define("profile.spec.field", "format", "A description file may only use the fields a profile run knows.", "Unknown field", "Fields: {fields}, and `flag -x: meaning`", who="client")
define("profile.spec.number", "format", "An estimate in a description file is a number.", "A value must be a number", "Give a number for this field in the description file.", who="client")
define("profile.command.missing", "format", "A profile run describes exactly one command, given after a double dash.", "No command to profile", "Put the command after a double dash, exactly as you would type it.", who="client")
define("profile.command.sudo", "security", "A profile run never uses privilege tools. The worker refuses them too (profile.command.denied); this tells the person before anything is sent.", "That command cannot be profiled",
       "A profile run never uses sudo, su, doas or pkexec.", who="client")
define("profile.details.missing", "format", "Whoever approves a profile run sees these details next to the worker's hard limits, so they are required.", "This profiling run is missing required details",
       "Whoever approves it sees these next to the worker's hard limits, so they are required.\n  The command below is yours with the missing flags added. Change the example values to your own before running it.",
       who="client", depends="a profile run that needs approval (not on Direct, once that setting exists)")
define("pack.recipe.unsafe", "security", "A saved packaging recipe may only name folders below the one you run the command in, so a registry cannot make a job read files elsewhere on your computer.",
       "This class's saved packaging is not safe to use", "{reason}", who="client", steps=["Send the files yourself: ct-run submit <class> --dir <folder>"])
define("class.name.invalid", "format", "A class name is short, lowercase and plain, so it is safe as a file name and easy to type.", "That class name is not allowed", "Use lowercase letters, digits and dashes, 2 to 41 characters.", who="client")
define("class.name.unusable", "format", "A class name must not collide with a command word or an existing class.", "That name cannot be used", "{reason}", who="client")
define("class.name.taken", "policy", "A class name names one class, so a second with the same name would replace it.", "A class with that name already exists", "Another class already has this name.", who="client",
       steps=["Pick another name with --name"])
define("class.batchable.needs_files", "policy", "A class takes its files in several jobs only if it was profiled with input files, which is how its files are known to be independent.", "--batchable only makes sense for a command profiled with input files",
       "The profile run had no input files.", who="client", steps=["Profile the command again with --dir or --file, then adopt it."])
define("class.proposal.waiting", "policy", "One proposal per class at a time, so two proposals cannot overwrite each other.", "A proposal for that class is already waiting", "Branch {branch} exists. Install it, or pick another name with --name.",
       who="client")
define("patch.refused", "security", "A patch from a worker is applied only after it passes the checks on the paths it touches, so a worker cannot write outside the folder it was sent.", "This patch will not be applied", "{reasons}",
       who="client")
define("pin.file.unreadable", "format", "A folder's .tenta.toml is read as TOML, so a broken file is named and not guessed at.", "A .tenta.toml file cannot be read", "{reason}", who="client",
       steps=["Open it and fix the line named above, or run ct-run use --clear to start again."])
define("pin.file.bad_value", "format", "A pin names workers, so every value in the file must be a worker name.", "A .tenta.toml file has a value that is not a worker name",
       "Check: {bad}. A worker name is letters, digits, dot, dash or underscore.", who="client", steps=["Open the file and fix it, or run ct-run use --clear to start again."])
define("name.invalid", "format", "A worker or command name is plain, so it is safe in a file and on a command line.", "Not a valid name", "Letters, digits, dot, dash and underscore only.", who="client")

# ---- errors on this computer: something went wrong, or is not ready yet ----
define("result.not_ready", "error", "Says plainly that there is nothing to read yet, so an empty result is never mistaken for a finished job.", "No result yet", "The job has not finished, or the id is wrong.", who="client",
       steps=["Wait for it with ct-run result JOB --wait, or list your jobs with ct-run jobs."])
define("profile.result.not_ready", "error", "Says plainly that a profile run has no result yet, so the person does not adopt something that was never measured.", "No profile result yet",
       "The worker may still be running it, or it was never approved.", who="client", steps=["Wait for it with ct-run result JOB --wait."])
define("patch.none", "error", "Only a job of a class that returns a patch, and that ended ok, has one.", "That job returned no patch",
       "Its status is {status}. Only a job of a class that returns a patch, and that ended ok, has one.", who="client", steps=["See what it did with ct-run result JOB."])
define("patch.misfit", "error", "A patch is checked against the folder before it is applied, so nothing is half-changed.", "The patch does not fit this folder", "{reason}", who="client",
       steps=["Go to the folder the job was sent from (the files must be where they were), then run this again."])
define("patch.apply_failed", "error", "git refused the patch after the check passed; the real reason is shown.", "git could not apply the patch", "{reason}", who="client",
       steps=["Save the patch and read it with ct-run patch JOB --out patch.diff."])
define("profile.needs_longer", "limit", "A command that only ran out of time says nothing about whether it suits the worker, so it is neither adopted nor recorded as an exclusion.",
       "That profile run needs a longer time limit", "It stopped at its time limit while still inside the memory and heat limits.", who="client",
       config="the person's own --timeout-s (the worker's ceiling applies only on a shared computer)")
define("profile.inconclusive", "error", "A profile run that did not complete cleanly proves nothing, so no class is proposed from it.", "Nothing to propose: the profiling run was inconclusive",
       "The command did not complete cleanly.", who="client", steps=["Run the command by hand to see why it fails, correct it, then profile it again."])
define("registry.shape", "error", "A registry that is not in the expected shape is reported, never edited blind.", "registry.yaml has no `classes:` section", "The registry proposal on main is not in the expected shape.", who="client",
       steps=["Open registry.yaml on the main branch of your queue repository and check it has a classes: section."])
define("repo.git_failed", "error", "The real error text of a git command that failed on one of the two repositories, with the command, so it is never guessed at.", "A git command failed", "{reason}", who="client",
       steps=["Run the command shown above by hand in that folder to see the same error."])
define("repo.not_set_up", "error", "Nothing can be sent or read until this computer has its two repository clones.", "This computer is not set up yet", "The two repository clones are not on this computer.", who="client",
       steps=["Run ct-run setup."])
define("repo.clone_failed", "error", "The real error text of a clone that failed, with a plain meaning and the steps for that cause.", "Could not clone a repository", "{url}: {detail}", who="client",
       steps=["Run git ls-remote on the repository address to test that it can be reached, then run ct-run setup again."])
define("repo.file_missing", "error", "A push that succeeded is not proof the file is there: say so loudly rather than report a job sent that the worker will never see.", "A file did not arrive on the branch",
       "It was pushed, but is not on the branch afterwards. Nothing was lost on your side: send it again.", who="client", steps=["Send it again with the same command."])
define("repo.push_failed", "error", "A branch that could not be pushed is reported with how to reach it, never as a job sent.", "Could not push to the branch", "Check your connection and access, then try again.", who="client",
       steps=["Check your connection and access, then run the same command again."])

# ---- administering the worker from this computer (ct-admin): actions, proposals, releases, updates, ssh ----
define("action.source.missing", "error", "A root-level action is added from a definition file or a pack folder, so a path that is neither is said plainly.", "Cannot find a definition",
       "Give a YAML file, or a pack folder with an actions/ folder in it.", who="client", steps=["Give the path of a definition file or a pack folder."])
define("action.file.unreadable", "error", "The real error text of a definition file that cannot be read.", "Cannot read a definition file", "{reason}", who="client", steps=["Open the file named above and correct it."])
define("action.file.shape", "format", "A definition holds only actions and the classes that use them, so nothing else is silently ignored.", "That is not an action definition",
       "It may only contain an `actions:` and a `classes:` section.", who="client", steps=["Remove every other section from the definition."])
define("action.duplicate", "format", "An action name names one program that runs as root, so a second definition would silently replace it.", "An action is defined twice", "in {kind}", who="client",
       steps=["Give each action its own name in the definition."])
define("action.none", "format", "An action says which program runs as root; without one there is nothing to register.", "That defines no action",
       "An action says which program runs as root; without one there is nothing to register.", who="client", steps=["Add an action to the definition."])
define("action.class.unlinked", "format", "A class that runs as root must name one action in the same definition, so what it runs can be checked.", "A class does not use one of these actions",
       "A class here must say `kind: privileged` and `action:` the name of an action in the same definition.", who="client", steps=["Add kind: privileged and action: NAME to the class."])
define("action.worker.silent", "error", "The worker checks a program before it may run as root; no answer means it was not checked.", "The worker did not answer", "{reason}", who="client",
       steps=["Is the worker up to date? Run: ct-admin update"])
define("action.path.invalid", "security", "A program that runs as root must be a plain file owned by root, in a system folder, that nobody else can write.", "That path cannot be a root-level action",
       "{problem}", who="client", steps=["The program must be a plain file owned by root, in a system folder, that nobody else can write."])
define("job.id.not_an_id", "format", "A job id is plain, so it is safe as a file name and on a command line.", "That is not a job id", "Job ids are lowercase letters, digits and dashes.", who="client",
       steps=["List your jobs with ct-run jobs."])
define("proposal.branch.protected", "policy", "main, jobs and data are managed by the system, so they are never merged as a proposal.", "That is not a branch you can merge",
       "main, jobs and data are managed by the system.", who="client", steps=["List the proposals with ct-admin proposals."])
define("proposal.branch.missing", "error", "A proposal is a branch in your queue repository; one that is not there cannot be merged.", "No such branch on GitHub", "The branch is not in your queue repository on GitHub.",
       who="client", steps=["List the proposals with ct-admin proposals."])
define("proposal.merge.conflict", "error", "A branch that does not merge cleanly is not merged: the conflict is for the person to resolve.", "A branch does not merge cleanly into main", "{detail}", who="client",
       steps=["Rebase the branch on main in your copy of the queue repository, then run this again."])
define("proposal.branch.nothing_new", "policy", "A branch whose changes are all in main has nothing to merge.", "A branch has nothing that is not already in main", "Its changes are already in main.", who="client",
       steps=["List the proposals with ct-admin proposals."])
define("proposal.push.private_email", "error", "GitHub refuses a push that would publish a private email address; the real cause is said, not 'main moved'.", "GitHub refused: the commit would publish a private email address",
       "Your GitHub account blocks pushes that expose your email.", who="client", steps=["Use your GitHub noreply address in this clone: git config user.email <id>+<handle>@users.noreply.github.com"])
define("proposal.push.main_moved", "error", "A push refused because main has new commits is retried by running the command again.", "main moved while you were reviewing",
       "GitHub refused the push because main has new commits.", who="client", steps=["Run the same command again."])
define("proposal.push.refused", "error", "The real text of a push GitHub refused, in plain words.", "GitHub refused the push", "{detail}", who="client",
       steps=["Read the reason above, then run the same command again."])
define("release.source.missing", "error", "An update needs a release source to read from.", "No release source is configured", "updates.source is empty.", who="client", steps=["Run ct-setup client."])
define("release.fetch_failed", "error", "The real error text of a release source that could not be fetched.", "Could not fetch the release source", "{reason}", who="client",
       steps=["Run git ls-remote on the release source to test that it can be reached."])
define("release.commit.missing", "error", "A commit that is not in the release source cannot be installed.", "A commit was not found in the release source", "Has it been pushed?", who="client",
       steps=["Push the commit to the release source, then run this again."])
define("release.commit.off_branch", "policy", "The worker only installs commits that are on the release branch, so what it runs has been through that branch.", "A commit is not on the release branch",
       "The worker only installs commits that are on the release branch.", who="client", steps=["Merge it first, then try again."])
define("update.dirty", "error", "A checkout with uncommitted changes is never touched by an update.", "Your checkout has uncommitted changes", "{root} was not touched.", who="client",
       steps=["Commit or stash them, then run ct-admin update again."])
define("update.fetch_failed", "error", "The real error text of a release that could not be fetched.", "Could not fetch the release", "{reason}", who="client", steps=["Run git ls-remote on the release source to test that it can be reached."])
define("update.not_fast_forward", "error", "A checkout that has commits the release does not is not rewound or merged for the person.", "Your checkout cannot be fast-forwarded to that release",
       "It has commits the release does not (or the history differs), so nothing was changed.", who="client", steps=["Merge the release into your checkout yourself with git merge."])
define("update.pip_failed", "error", "The real error text of a pip install that failed.", "pip could not install that release", "{reason}", who="client", steps=["Run the pip command shown to see the error."])
define("ssh.control_folder.long", "error", "A unix socket path has a short limit; a folder that exceeds it cannot be used for the connection.", "The SSH control folder is too long",
       "the SSH control folder {path} is too long for a unix socket", who="client", steps=["Set CT_SSH_DIR to a shorter folder."])
define("ssh.address.missing", "error", "A connection needs a server address.", "No server address is configured", "server.host is empty or not an address.", who="client", steps=["Run ct-setup client."])
define("ssh.connect_failed", "error", "The real error text of a connection that failed.", "Could not connect", "{reason}", who="client", steps=["Run ssh USER@HOST true to test key login from this computer."])
define("worker.name.invalid", "format", "A worker name is short and plain, so it is safe in a file and on a command line.", "A worker name is not allowed",
       "a worker name is lowercase letters, digits and dashes, starting with a letter", who="client")
define("worker.config.missing", "error", "A worker is added from its configuration file, so a path with none is said plainly.", "There is no configuration file there", "there is no configuration file at {path}", who="client")
define("worker.repo.shared", "security", "Each GitHub worker has its OWN private queue and results repositories: a deploy key covers a whole repository, so two workers on one repository could read or forge each other's jobs and results. No setting turns this off.", "Two workers would share a repository",
       "{slug} is already used by {other}; every worker needs its own queue and results repositories, and a queue and results repository must differ", who="client", steps=["Give this worker its own repository names (github.queue_repo and github.results_repo), then run the command again."])

define("files.held.secret_name", "security", "Protects the person from sending a credential to a worker by accident: a file whose name conventionally holds a secret (.env, *.pem, id_rsa, .npmrc ...) is held back "
       "unless an --include names it exactly. A wildcard never sends one.", "A file that usually holds a secret was held back", "{name!r} was held back: its name is one that usually holds a secret",
       who="client", config="the list of names in ct.client.submit (SECRET_NAMES), not configurable yet", steps=["Send it anyway by naming it: --include 'PATH' on ct-run submit."])
define("files.held.secret_content", "security", "Protects the person from sending a credential out of their own network by accident: on the GitHub transport a job is pushed to a private GitHub repository, so a file whose TEXT looks like a private key "
       "or an API token is held back unless an --include names it exactly. On Direct it is only warned about (files.warn.secret_content), because nothing leaves the person's own network.", "A file that looks like it contains a secret was held back",
       "{name!r} was held back: its text looks like a private key or an API token, and this transport sends jobs through GitHub", who="client", config="which transport is set up ([transport] kind)",
       steps=["If it is not a real secret, send it by naming it: --include 'PATH' on ct-run submit."])
define("files.warn.secret_content", "security", "Tells the person that a file being sent contains text shaped like a private key or an API token. It is only a warning, not a hold: source and test files often hold examples "
       "(23 of this project's own 283 files match).", "A file being sent looks like it contains a secret", "{name!r} contains text that looks like a private key or an API token",
       who="client", steps=["If it is real, leave the file out with --include on ct-run submit."])

# ---- ct-admin: approving, signing, workers, root-level actions, proposals, classes, apply ----
define("prompt.no_terminal", "policy", "A question that needs the person's answer is never answered for them when nobody is at the keyboard.", "This needs your answer", "It asks a question, and nobody is at the keyboard.", who="client",
       steps=["Run it in a terminal, or add --yes if you are sure."])
define("review.show_failed", "error", "Jobs that could not be shown are not marked as viewed, so they cannot be approved unseen.", "Could not show those jobs", "They were NOT marked as viewed.", who="client",
       steps=["Run ct-admin check to see why the worker cannot be read."])
define("approval.key.untrusted", "security", "Approvals are signed here and checked on the worker against the key it was set up with, so a worker that trusts another key (or none) will not run the job.",
       "A worker does not trust your approval key yet", "Approvals are signed here and checked on the worker against a key it was set up with. This worker has {has}.", who="client",
       steps=["Run the install again; it gives the worker your key: ct-setup install", "Or approve this one by logging in to the worker: add --ssh"])
define("approval.sign_failed", "error", "The real error text of an approval that could not be signed.", "Could not sign the approval", "{reason}", who="client", steps=["Run ct-setup install to make the key again."])
define("registry.none_on_main", "error", "A registry that has not been merged yet cannot be signed.", "There is no registry.yaml on main to sign", "Nothing has been merged into your registry yet.", who="client",
       steps=["List the proposals with ct-admin proposals."])
define("registry.sign_failed", "error", "The real error text of a registry that could not be signed.", "Could not sign the registry", "{reason}", who="client", steps=["Run ct-setup install to make the key again."])
define("order.sign_failed", "error", "The real error text of an order that could not be signed.", "Could not sign the order", "{reason}", who="client", steps=["Run ct-setup install to make the key again."])
define("review.none_viewed", "policy", "Jobs are approved together only after they have been looked at, so nothing is approved unseen.", "None of the waiting jobs have been viewed",
       "Review them first; that is what lets them be approved together.", who="client", steps=["Look at them with ct-admin review."])
define("approve.which", "format", "An approval names the job it covers.", "Say which job to approve", "Give a job id, or --all for every job you have already viewed.", who="client",
       steps=["Give a job id, or run ct-admin approve --all."])
define("job.expired.approve", "security", "A job older than the worker's age limit is refused by the worker, so it is never approved here: replay protection seen from this side.", "That job has expired",
       "It was sent more than a day ago, and {name} does not run a job that old, so it cannot be approved. Send it again.", who="client", steps=["Send it again with ct-run submit."])
define("job.unknown", "error", "A job that is neither on the jobs branch nor waiting on the worker cannot be approved.", "No such job",
       "Its file is not on the jobs branch, and {name} does not show it waiting for approval.", who="client", steps=["List your jobs with ct-run jobs."])
define("update.check_failed", "error", "The real error text of a release check that failed.", "Could not look for a newer release", "{reason}", who="client", steps=["Run git ls-remote on the release source to test that it can be reached."])
define("worker.rename.name_missing", "format", "A rename needs the new name.", "Say the new name", "ct-admin workers rename WORKER NAME gives a worker a name here; --clear removes it.", who="client",
       steps=["Give the name: ct-admin workers rename WORKER NAME"])
define("worker.rename.unusable", "format", "A name must not collide with another worker or a command word.", "That name cannot be used", "{problem}", who="client")
define("worker.rename.no_terminal", "policy", "Naming every worker asks about each one, which needs a person at a terminal.", "Naming every worker needs a terminal", "It asks about each one in turn.", who="client",
       steps=["Name one worker without a terminal: ct-admin workers rename WORKER NAME"])
define("worker.add.args", "format", "A worker is added with its own name and configuration file.", "Say the worker's name and its configuration file", "Each worker is a complete install with its own configuration.", who="client",
       steps=["Give both: ct-admin workers add NAME --config FILE"])
define("worker.add.failed", "error", "The real error text of a worker that could not be added.", "Could not add that worker", "{reason}", who="client")
define("worker.unlisted", "error", "Only workers in the list can be removed from it.", "There is no such worker in the list", "The worker in your main configuration is always listed and is not removed here.",
       who="client", steps=["List your workers with ct-admin workers."])
define("action.argument", "format", "A root-level action command names what it acts on.", "That command needs an argument", "It needs {need}.", who="client")
define("action.definition.refused", "security", "A definition that the worker would refuse is stopped here, before anything is sent.", "That definition would be refused", "{problems}", who="client",
       steps=["Edit the YAML file to correct each problem listed above, then run the same command again. Nothing was sent anywhere."])
define("action.program.duplicate", "policy", "The same program under a second name would be two ways to run it as root.", "That program is already registered",
       "The same program under a second name would be two ways to run it as root.", who="client")
define("action.unknown", "error", "Only a registered root-level action can be removed.", "There is no such root-level action", "Registered: {known}.", who="client")
define("proposal.registry.invalid", "policy", "A branch that would leave an invalid registry is not merged.", "A branch would leave an invalid registry", "{problems}", who="client")
define("proposal.which_branch", "format", "A merge names the branch it merges, or all of them.", "Say which branch to merge", "Or use --all.", who="client", steps=["Name the branch, or run ct-admin proposals merge --all."])
define("proposal.action.unknown", "format", "proposals takes one of three actions.", "Unknown action", "Use list, merge or tidy.", who="client", steps=["See the actions with ct-admin help proposals."])
define("adopt.result.missing", "error", "A class is adopted from a finished profile run.", "No result yet", "Profile it first, then adopt it.", who="client", steps=["Wait for it with ct-run result JOB --wait."])
define("class.unknown_on_main", "error", "Only a class in your registry can be retired.", "There is no such class on main", "Nothing to retire.", who="client", steps=["List the classes with ct-run classes."])
define("class.retire.protected", "policy", "The class profiling runs on is never retired: without it nothing new could be tested on the worker.", "That class cannot be retired",
       "It is what profiling runs on; without it nothing new can be tested on the worker.", who="client")
define("class.unknown", "error", "Only a class in your registry or on the worker can be changed.", "No such class", "It is neither in your registry nor installed on the worker.", who="client",
       steps=["List the classes with ct-run classes."])
define("class.from_pack", "policy", "A class that comes from a pack is changed in the pack, so the change survives the next pack update.", "That class comes from a pack",
       "Its settings are changed in the pack, not here.", who="client")
define("class.set.nothing", "format", "A change names what to change.", "Say what to change", "Give at least one of --tier, --network or --tools.", who="client")
define("class.set.invalid", "policy", "A change that would leave an invalid registry is not made.", "That would leave an invalid registry", "{problems}", who="client")
define("class.network.needs_terminal", "security", "A class that uses the network without approval is only set by the person answering the question themselves; --yes does not answer it.",
       "This needs your answer at a terminal", "A class that uses the network without your approval can only be set by answering this question yourself; --yes does not answer it.", who="client",
       steps=["Run it in a terminal and answer the question."])
define("apply.install_failed", "error", "The real step that failed during an install, with the host and exit code.", "The install failed", "The update tool restores what it changed when it can. Read the messages above.",
       who="client", steps=["Read the error above: it names the step that failed. Correct it on the worker or on this computer, then run the same ct-admin apply command again."])

# ---- the Windows setup, and the small checks that keep a value from reaching PowerShell as anything but a plain value ----
define("win.step.failed", "error", "The real error text of the step that failed, with the computer, so a rerun continues from there.", "A step of the Windows setup failed", "{what}: {detail}", who="client",
       steps=["Run the same command again: it continues from where it stopped."])
define("win.removal.step_failed", "error", "The real error text of the removal step that failed; each step only removes what is still there, so a rerun is safe.", "A step of the Windows removal failed", "{what}: {detail}", who="client",
       steps=["Run the same command again: each step only removes what is still there."])
define("win.key.not_made", "error", "The mailbox key is made on this computer; if ssh-keygen fails here nothing is sent to the worker.", "The mailbox key could not be made", "{reason}", who="client",
       steps=["Run: ssh-keygen -t ed25519 -N '' -f /tmp/ct-test-key (it must make a key without an error)"])
define("win.release.lookup_failed", "error", "The real error text of a Windows release that could not be looked up.", "The Windows release could not be looked up", "{reason}", who="client",
       steps=["Open the release source in a browser on this computer; if it does not load, the connection to it is the problem."])
define("win.release.none", "error", "A Windows worker is installed from a release tag, so without one there is nothing to install.", "No Windows release has been published yet",
       "A Windows worker is installed from a release-win-X.Y.Z tag of the project, and there is none.", who="client",
       steps=["Ask the project's owner to publish one, or give a commit with --set updates.source=<your fork>"])
define("win.unreachable", "error", "A computer that cannot be reached over SSH is named as that before any step runs, so it is not reported as a failure of a later step.", "The Windows computer cannot be reached over SSH",
       "{said}", who="client", steps=["Run ssh USER@HOST hostname to see whether it answers, then run the same command again."])
define("win.mailbox.unreachable", "error", "The worker's mailbox is reached over SSH from this computer; the real error and how to test the key by hand are shown.", "This computer could not reach the worker's mailbox",
       "{reason}", who="client", steps=["Test the key by hand with ssh -i KEY USER@HOST true."])
define("win.first_job.failed", "error", "The first job proves the whole loop works; if it does not come back the setup is not called done.", "The worker is set up, but its first job did not come back",
       "The test job {job} ended as {status}.", who="client", steps=["Run the same command again once it is fixed: nothing is set up twice."])
define("win.arg.url", "security", "A release source is an https address, so nothing else is ever fetched from by the installer.", "That is not a release source the installer will fetch from",
       "not an https release source this will fetch from: {value!r}", who="client")
define("win.arg.commit", "security", "A release is pinned to a full commit id, never a name that could move.", "That is not a full commit id", "not a full commit id: {value!r}", who="client")
define("win.arg.folder", "security", "Only a plain folder under the install root is written to; nothing that could carry PowerShell syntax is accepted.", "That is not a folder the installer will write to",
       "not a folder this will write to: {value!r}", who="client")
define("win.arg.tools_folder", "security", "Only a plain tools folder is written to by the tools installer.", "That is not a folder the tools installer will write to",
       "not a folder this installer will write to: {value!r}", who="client")
define("win.arg.account", "security", "An account name is plain, so it is safe inside a PowerShell script.", "That is not an account name the installer will create",
       "not an account name this will create: {value!r}", who="client")
define("win.arg.mailbox_account", "security", "A mailbox account name fits Windows (3 to 20 lowercase letters, digits or dashes).", "That is not a mailbox account name that fits Windows",
       "not a mailbox account name that fits Windows (3 to 20 lowercase letters, digits or -): {value!r}", who="client")
define("win.arg.public_key", "security", "Only one public key line of a known type is written to the mailbox account's authorized keys.", "That is not one public key line",
       "not one public key line (ssh-ed25519, ssh-rsa or ecdsa-sha2-nistp256)", who="client")
define("win.arg.task", "security", "A task name is plain, so it is safe inside a PowerShell script.", "That is not a task name the installer will register", "not a task name this will register: {value!r}", who="client")
define("win.arg.arguments", "security", "A task's arguments are plain text: nothing a shell or PowerShell would read as more.", "Those are not arguments the task will run", "not arguments this will run: {value!r}", who="client")
define("packages.list.invalid", "format", "A job's packages are a short list of fully named packages, so what the worker installs is known before it runs.", "The package list is not valid",
       "packages must be a list of at most {limit} entries like 'apt:jq', 'npm:eslint', 'pip:pyflakes'", who="both")
define("packages.spec.invalid", "format", "A package is named by where it comes from and what it is called.", "A package is not valid",
       "bad package {name!r}: use apt:<name>, npm:<name>[@version] or pip:<name>[==version]", who="both")
define("registry.file.unreadable", "error", "The real error text of a registry file that cannot be read.", "The registry file cannot be read", "cannot read {path}: {reason}", who="both")
define("registry.file.yaml", "error", "The real error text of a registry file that is not valid YAML.", "The registry file is not valid YAML", "{path} is not valid YAML: {reason}", who="both")
define("approval.key.not_ssh", "format", "An approval key is an SSH public key, so nothing else is trusted as one.", "That is not an SSH public key", "that is not an SSH public key", who="both")
define("config.host.missing", "error", "A configuration without a server.host line cannot be re-pointed at a new address.", "The configuration has no server.host line", "no server.host line in {path}", who="client")

# ---- the setup commands (ct-setup): server, install, migrate, rename, client, add-worker, uninstall ----
define('setup.command.failed', 'error', 'The real text a setup command printed when it failed, so the cause is never guessed at.', 'A setup command failed', '`{command}` said: {said}', who="client")
define('setup.answers.missing', 'format', 'A setup that needs an answer with no default asks for it; it never invents one.', 'Some answers are needed', 'No default exists for: {missing}', who="client")
define('setup.config.invalid', 'format', 'A configuration that does not validate is never written.', 'The answers do not make a valid configuration', '{problems}', who="client")
define('setup.worker_account.missing', 'error', 'The worker runs as an account that must exist before anything is moved.', 'The worker account does not exist on this machine', 'Nothing was changed.', who="client")
define('setup.job.running', 'policy', 'A running job is never interrupted by a move or a rename.', 'A job is running', '{running} is still running. Wait for it, or stop it (ct-admin kill), then try again; nothing was changed.', who="client")
define('setup.mailbox_key.not_made', 'error', 'The mailbox key is made here; if ssh-keygen fails nothing can use the mailbox.', 'The mailbox key could not be made', '{reason}', who="client")
define('server.machine.unsupported', 'policy', 'Only machines the worker is known to run on are set up.', 'This machine is not supported', '{problem}', who="client")
define('server.not_root', 'policy', 'The server half creates accounts and installs system files, so it runs as root.', 'Run this as root', 'It creates accounts and installs system files.', who="client")
define('server.hardening.unknown', 'format', 'Only protections the setup knows are applied.', 'Unknown hardening module', '{names}', who="client")
define('server.admin_account.missing', 'error', 'The admin account is the one you log in as; it must exist first.', 'The admin account does not exist', 'It is the account you log in as; create it first.', who="client")
define('server.worker_account.differs', 'policy', "A second worker account would leave the first one's repositories and state unreadable.", "This machine's worker already runs as another account", "Creating a second account would leave {have}'s repositories and state unreadable and every job stuck. Nothing was changed by this check.", who="client")
define('server.release.unknown', 'error', 'The release a worker runs is recorded, so one that cannot be identified is not installed.', 'Cannot tell which release this is', '{source} is not a git checkout; pass --rev <commit> if you know it.', who="client")
define('server.install.refused', 'error', "The install's own checks refused it; the reasons are shown.", 'The install was refused', '{errors}', who="client")
define('server.queue.unreadable', 'error', 'The worker must read the queue repository before the registry is installed.', 'The worker could not read the queue repository, so the job registry was not installed', '{reason}', who="client")
define('server.registry.not_installed', 'error', 'A registry with a problem is not installed.', 'The job registry was not installed', '{reason}', who="client")
define('server.ai_tool.not_running', 'error', 'An AI tool that was installed but does not run is reported, not left half-working.', 'The Claude tool does not run on this machine', 'It installed but `claude --version` failed.', who="client")
define('server.token.invalid', 'format', 'A token is one long line with no spaces.', 'That does not look like a token', 'It should be one long line with no spaces. Run ct-setup install again to enter it.', who="client")
define('server.protection.failed', 'error', 'A protection that could not be applied stops the worker from starting.', 'A protection could not be applied, so the worker was not started', '{failed}', who="client")
define('install.package.failed', 'error', 'The real error text of a release that could not be packaged.', 'Could not package the release', '{reason}', who="client")
define('install.workdir.failed', 'error', 'The real error text of a working folder that could not be made on the server.', 'Could not make a working folder on the server', '{reason}', who="client")
define('install.send.failed', 'error', 'The real error text of a release that could not be sent.', 'Could not send the release to the server', '{reason}', who="client")
define('install.config.stale', 'format', 'A saved configuration that no longer validates is not reused.', 'The configuration from the earlier run is not valid', '{problems}', who="client")
define('install.ssh.login_failed', 'error', 'The real reason SSH login to the server failed.', 'Cannot log in over SSH', '{why}', who="client")
define('install.server.unsupported', 'policy', 'Only servers the worker is known to run on are set up.', 'The server is not supported', '{problem}', who="client")
define('install.deploy_keys.waiting', 'error', 'The server half stops until its two deploy keys are on GitHub, and continues when run again.', 'The server is still waiting for its deploy keys', 'They were not added in time.', who="client")
define('install.server.stopped', 'error', 'The server half stopped; its messages are above and a rerun continues.', 'The server setup stopped', 'Read the messages above. Running ct-setup install again continues where it left off.', who="client")
define('migrate.config.invalid', 'format', 'A connection change that does not validate is never written.', 'The new connection does not make a valid configuration', '{problems}', who="client")
define('migrate.method.unknown', 'format', 'Only known connection methods are moved to.', 'That is not a connection method', 'Choose one of: {methods}.', who="client")
define('migrate.method.same', 'policy', 'Moving to the method already in use changes nothing.', 'The install already uses that method', 'There is nothing to move.', who="client")
define('migrate.to_github.unsupported', 'policy', 'Moving back to GitHub is not done by this command yet.', 'Moving back to GitHub is not done by this command yet', 'Nothing was changed. Your GitHub repositories were never touched, so they still hold the history up to the move.', who="client")
define('migrate.clone.missing', 'error', 'A clone that is missing cannot be copied into the mailbox.', 'The worker has no clone of that repository', '{path} is missing, so there is nothing to copy into the mailbox.', who="client")
define('migrate.release.missing', 'error', 'The release to install must be a release checkout.', 'The release to install is missing', '{source} is not a release checkout.', who="client")
define('migrate.key.not_arrived', 'error', 'Without the mailbox key nothing could use the mailbox, so nothing is changed.', "Your computer's mailbox key did not arrive", 'Without it nothing could use the mailbox, so nothing was changed.', who="client")
define('migrate.step.failed', 'error', 'The real text of the migration step that failed; a rerun carries on and skips what is done.', 'A migration step failed', '`{command}` said: {said}', who="client")
define('rename.config.invalid', 'format', 'A rename that does not validate is never written.', 'The new name does not make a valid configuration', '{problems}', who="client")
define('rename.name.invalid', 'format', 'An install name is short and plain, so it is safe as a folder and a service name.', 'That is not a valid install name', '3 to 24 lowercase letters, digits or -, starting with a letter.', who="client")
define('rename.name.same', 'policy', 'Renaming to the same name changes nothing.', 'The install already has that name', '{old} is what its configuration says, so there is nothing to rename.', who="client")
define('rename.folder.exists', 'error', 'A folder already using the new name is never overwritten.', 'That name already exists', "Something else is using the new name's folder, so nothing was changed.", who="client")
define('rename.service.exists', 'error', 'A service already using the new name is never overwritten.', 'A service with that name already exists', 'Something else is using the new name, so nothing was changed.', who="client")
define('rename.release.missing', 'error', 'The release to install under the new name must be a release checkout.', 'The release to install under the new name is missing', '{source} is not a release checkout.', who="client")
define('rename.step.failed', 'error', 'The real text of the rename step that failed; a rerun carries on and skips what is done.', 'A rename step failed', '`{command}` said: {said}', who="client")
define('client.tools.missing', 'error', 'The setup needs git and ssh on this computer.', 'Missing tools', '{missing} must be installed first', who="client")
define('client.github.not_yours', 'policy', 'Repositories are created only under the signed-in account or an organisation it belongs to.', 'That GitHub account is not yours', 'You are signed in to GitHub as {login}, but the repositories would be created under {owner}, which is neither your account nor an organisation you belong to.', who="client")
define('client.mailbox.unreadable', 'error', "The real reason this computer cannot read the worker's mailbox.", "This computer cannot read the worker's mailbox", '{why}', who="client")
define('addworker.question.unanswered', 'format', 'A question with no answer stops the setup; it never guesses.', 'That question was not answered', '{problem}', who="client")
define('worker.name.taken', 'policy', 'A worker name names one worker on this computer.', 'That worker name cannot be used', '{reason}', who="client")
define('addworker.windows.direct_only', 'policy', 'A Windows worker uses the Direct connection only in this version.', 'A Windows worker uses the Direct connection only', 'The GitHub connection is not available for a Windows worker in this version.', who="client")
define('uninstall.step.failed', 'error', 'The real text of the removal step that failed.', 'A removal step failed', '`{command}` said: {said}', who="client")
define('uninstall.config.unreadable', 'error', "A removal needs the install's own configuration to find what to remove.", "Cannot read this install's configuration", '{problems}', who="client")
define("files.link.skipped", "security", "A link is never followed: it can point at a file outside the project (a key, a configuration), whose contents would be sent under the link's own, harmless-looking name. "
       "Send the file itself if you mean to.", "A link was not followed", "{name!r} is a link, so it was not followed", who="client", steps=["Copy the file into the project and send that, or leave it out."])

# ---- what a worker can hold ----
define("job.too_big_for_worker", "limit", "A worker accepts only a job it can hold: its file is refused, unread, when it is over the ceiling (a quarter of free disk and memory, at most 1024 MB, or hardware.max_job_mb), so one job cannot fill the disk or the memory.",
       "The job is too big for this worker", "this job file is {size} MB, over the {ceiling} MB this worker accepts right now; it was not read", depends="the worker's free disk and memory, or hardware.max_job_mb",
       config="the worker's hardware.max_job_mb (unset: automatic)", steps=["Send fewer or smaller files with --include on ct-run submit, or send it to a worker with more room."])

# ---- what the mailbox takes in before the worker reads anything (finding F5: a push is on the disk before a job file is read) ----
define("job.push.disk_nearly_full", "limit", "A worker's mailbox takes no push while the disk is below its reserve, so a client cannot fill the last of the disk, however small each job.",
       "This worker's disk is nearly full", "{free} GB is free on this worker and {reserve} GB is kept in reserve, so it takes no job until space is back. Nothing was sent.",
       who="worker", depends="the worker's free disk (the same reserve that stops it starting a job)", config="none: the reserve is 10% of the disk, at least 2 GB",
       steps=["On the worker, free disk space: ct-worker status shows how much is free.", "Or send it to another worker."])
define("job.push.too_big", "limit", "A worker's mailbox stops a push that is over what the worker accepts as a job, while it is still arriving, so one push cannot fill the disk before the job file is ever checked.",
       "The job is too big for this worker", "this push is over the {ceiling} MB this worker accepts right now (plus {slack} MB for git); it was stopped and nothing was kept.",
       who="worker", depends="the worker's free disk and memory, or hardware.max_job_mb", config="the worker's hardware.max_job_mb (unset: automatic)",
       steps=["Send fewer or smaller files with --include on ct-run submit, or send it to a worker with more room."])

# ---- replacing a Direct worker's mailbox key (ct-setup rotate-key) ----
define("rotate.not_direct", "policy", "Only the Direct connection has a mailbox key to replace: GitHub workers use deploy keys that ct-setup install manages.", "Only a Direct worker has a mailbox key",
       "This worker uses {connection}, which has no mailbox key.", who="client", steps=["Run ct-setup install to see how the worker is connected."])
define("rotate.no_key", "error", "There is no key to replace on this computer; a first key is made by the install.", "This computer has no mailbox key for that worker", "The key file is missing.", who="client",
       steps=["Run ct-setup install --worker NAME to make one."])
define("rotate.install_failed", "error", "The worker did not take the new key, so nothing was changed: the old key still works and the new one was deleted.", "The worker did not take the new key", "{reason}", who="client",
       steps=["Run ct-setup rotate-key again once the worker answers: nothing was changed."])
define("rotate.verify_failed", "error", "The worker was given the new key but the check that it works failed; the worker now trusts only the new key, so it is kept as the key in use and the old one is kept aside.",
       "The new key is installed but the check failed", "The worker now trusts only the new key, which is in place here; the check said: {reason}", who="client",
       steps=["Try the connection again with ct-admin check.", "If it still fails, restore the old key from the file shown and run ct-setup install again."])

# ---- a big set sent compressed on Direct (task direct-compress; security finding F5) ----
# Release compatibility: a worker can unpack only from the release that added the `compress` entry to its registry.json (published when it starts). A client that does not find it refuses before sending.
define("files.compress.worker_cannot", "placement", "A job whose files are compressed would reach an older worker with its files missing, so the client sends it compressed only to a worker that said it can unpack.",
       "This worker cannot unpack compressed files", "{count} ({size}) is over {threshold}, which Direct sends compressed, but {worker} has not said it can unpack them. It runs a release older than this one, or its worker service has not restarted since it was updated.",
       who="client", depends="the worker's release (it must publish `compress` in registry.json) and the transport (Direct)", config="send without compression: --no-compress",
       steps=["Send it again without compression: add --no-compress to the same command (it can be slow and large), or update the worker (ct-admin update) and run it again.", "Or attach fewer files with --include."])
define("files.compress.over_worker_cap", "limit", "Checked here first so a set the worker would refuse as a zip bomb is not sent at all.", "These files are too big for this worker, even compressed",
       "{count} is {size} before compression, over the {cap} MB this worker unpacks (a quarter of its free disk, and within its memory). Attach only the files the command needs (--include).",
       who="client", depends="the worker's free disk and memory and its release (`compress` in registry.json, from the release that added compression)", config="none: it follows the worker's free disk",
       steps=["Attach fewer files with --include on ct-run submit.", "Or send it without compression with --no-compress (the worker may still refuse it as too big)."])
define("files.unpack.too_big", "security", "Zip-bomb guard (finding F5): a small compressed job cannot grow into more than the worker will hold, whatever size it claims.", "The files unpack to more than this worker accepts",
       "the files would unpack to more than {cap} MB, the most this worker unpacks right now (a quarter of its free disk, and within its memory); nothing was written",
       depends="the worker's free disk and memory; the worker release that can unpack (the one that publishes `compress`)", config="none: it follows the worker's free disk",
       steps=["Attach fewer files with --include on ct-run submit.", "Or send it again without compression: add --no-compress to the same command."])
define("files.unpack.no_room", "limit", "The worker checks free disk before it unpacks, so one job cannot fill the disk (finding F5).", "This worker has no room to unpack these files",
       "the files unpack to {size} MB but this worker has {free} GB free and keeps {reserve} GB back; nothing was written", depends="the worker's free disk; the worker release that can unpack",
       config="the worker's disk reserve (10% of the volume, at least 2 GB)", steps=["Free some disk on the worker, or send it to a worker with more room.", "Or attach fewer files with --include, or add --no-compress to the same command."])
define("files.unpack.unreadable", "format", "Compressed data that cannot be read exactly as sent is never run.", "The compressed files could not be read",
       "the compressed files could not be unpacked: {detail}", depends="the worker release that can unpack (the one that publishes `compress`)",
       steps=["Send it again: ct-run submit does the compression itself, so a damaged copy is not repeated.", "Or add --no-compress to the same command."])
