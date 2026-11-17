# Colours and the look of the output

## In plain words

Everything the tool prints to a person (questions, results, warnings, waiting messages, the worker cards, the review screens) is written in one shape and one set of
colours: a blue rail, bold labels, a coloured tag for the kind of news (green for done, amber for take care, red for a problem, magenta for anything that runs as root),
and any command you should copy on a line of its own. Those colours live in **one place**. If you want a different look, you change one file; no command needs editing.
When the output is going to a file or a pipe, or `NO_COLOR` is set, there are no colours at all.

To see what you have: `cw-admin theme`. To change it, create `~/.cw/theme.toml`:

```toml
theme = "light"          # default, light (for a white terminal) or mono (no colour, only weight)
[colors]
accent = "38;5;99"       # any role, as terminal colour numbers
```

## Technical specification

- `src/cw/theme.py` is the only module that writes a terminal colour. Code asks for a **role** (`strong`, `muted`, `accent`, `accent_chip`, `ok`, `ok_chip`, `warn`, `warn_chip`, `bad`,
  `bad_chip`, `root`, `root_chip`, `info`, `neutral_chip`, `brand`, `brand_dim`, `quiet`, `italic`); `ui.paint(text, ui.GREEN)` and the wizard's own helpers resolve the role when
  they print. `cw-admin theme` lists every role with its meaning. The older names (`GREEN`, `RED`, `BOLD` ...) are aliases of roles.
- The theme is read once, first found wins: `CW_THEME_FILE`, `~/.cw/theme.toml`, `theme.toml` next to the active `cw.toml` (so the server's own messages can have one too).
  `CW_THEME=<name>` picks a built-in theme without a file. Codes must be plain colour numbers (`38;5;75`, `1;48;5;27`); anything else, an unknown role or a
  bad file is ignored with the default look and reported by `cw-admin theme`. A broken theme never stops a command, and nothing but digits and semicolons can reach a terminal.
- Colour is on only for a terminal; `CW_COLOR=always|never` and `NO_COLOR` override that (see `cw.ui`).
- A unit test (`tests/test_theme.py`) fails when any module outside `theme.py` writes an escape colour or a numeric colour, so the rule holds as the tool grows.
- The shape of a message is `ui.say(...)`: a coloured TAG and bold title, aligned labelled rows, steps, then copyable commands, each alone on a line. New commands print with it
  (or `ui.fail` for an error), not with a bare `print`, unless the output is data meant for another program (JSON, a job id, a file's contents).
- **Where to go next.** A command that shows data ends, for a person at a terminal, with the commands that usually come next (`ui.related`): a bold line saying what it does and the
  command on its own line beneath it, for example `cw-run classes` ends by offering to add, remove and run a class. It goes to stderr and only when a person is watching, so a pipe,
  a script or `--json` never sees it; `CW_HINTS=never` turns it off. A command that is wrongly typed gets one message in the same shape (what was wrong, the real choices, the nearest
  command to copy), never argparse's own usage text.
