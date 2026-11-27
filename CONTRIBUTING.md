# Contributing

## Set up

```bash
git clone <your fork> && cd armada
python3 -m venv .venv && .venv/bin/pip install -e . pyyaml
scripts/privacy-check --install-hook         # blocks commits that contain personal or environment data
.venv/bin/python -m unittest discover -s tests
```

## Ground rules

1. **Tests with every change.** The job lifecycle, the wizard and the update tool all run against fakes; add the fake case that shows your change works (and the one that shows it fails safely).
2. **The configuration is the only source of names and paths.** Nothing may hard-code an install's host name, user, folder or repository. Use `Config.path(...)`, `Config.unit(...)` and `render`.
3. **Nothing a person did not choose.** The wizard asks, shows and then acts; every step must appear in the preview, and a dry run must change nothing.
4. **Fail closed.** When something cannot be proved safe (isolation, a signature, a validation), refuse and say why, in plain words and with a command to copy.
5. **Use reserved example values** in code, docs and tests: `example.com`, `192.0.2.0/24`, `someone`. The privacy guard enforces it. Never commit a real host name, address, handle, token or path.
6. **Keep the generated pages current**: `scripts/gen-docs` after you change a setting or a command (a test fails if you forget).
7. Messages for people go through `ct.ui.say` (a tag, a bold title, details, commands each on their own line); status to standard error, data to standard output.

## Writing a pack

A pack is a folder under `packs/` with `pack.toml`, `classes/*.yaml` and/or `policy/*.toml`. Check it with `python3 -m ct.packs check packs/<name>`. Policy can only add rules, and a class name can belong to only one place.
See [packs](docs/components/packs.md). Personal classes (your own tools, your employer's) belong in your private queue repository, not here.

## Writing a hardening module

See [hardening](docs/components/hardening.md): a class with `check`, `preflight`, `plan` and `revert`, tested against the fake machine, and offered through `hardening.modules`.

## Releases

A release is an annotated tag `release-<version>` on the default branch, signed with an SSH key (`git -c gpg.format=ssh -c user.signingkey=<key.pub> tag -s release-0.1.0`). People who use `updates.mode = "signed-tags"` list the signing key's public half in `allowed_signers` on their server.
Before tagging: all tests pass, `scripts/gen-docs --check` is clean, `scripts/privacy-check --history` is clean, and `scripts/e2e-orbstack --fresh` is green.

## Commits

Small and focused, with a message that says why. Commits are authored with a GitHub `noreply` address (the privacy guard refuses anything else).

## Output rule: everything a person sees is a card

Nothing in `src/ct` prints on its own. A person at a terminal sees cards, rail rows, tables and chips; a pipe or a log sees the plain text of the same thing. Use:

- `ct.ui.say(...)` for a message (a coloured tag, a subject, labelled facts, commands to copy beneath it);
- `ct.ui.table(...)` for a list of things, `ct.ui.document(...)` for the text of something (a file, an output);
- `ct.setup.report.sink()` (or the `say` it gives you) for the setup's streamed progress, so every line sits on the card rail;
- theme roles from `ct.theme` for colour, never an escape code.

A line that must be something else (JSON a script reads, a job id, a journal line) is marked `# output: data` or `# output: log`. `tests/test_output_style.py` fails on any other print.
