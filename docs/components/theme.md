# Colours and the look of the output

## In plain words

Everything the tool prints to a person (questions, results, warnings, waiting messages, the worker cards, the review screens) is written in one shape and one set of
colours: a purple rail (purple is the main colour; blue is the accent, used for data labels), coloured capital labels, a coloured tag for the kind of news (green for done, amber for take care, red for a problem, magenta for anything that runs as root),
and any command you should copy on a line of its own. Those colours live in **one place**. If you want a different look, you change one file; no command needs editing.
When the output is going to a file or a pipe, or `NO_COLOR` is set, there are no colours at all.

To see what you have: `ct-admin theme`. To change it, create `~/.ct/theme.toml`:

```toml
theme = "light"          # default, light (for a white terminal) or mono (no colour, only weight)
[colors]
accent = "38;5;99"       # any role, as terminal colour numbers
```

## Technical specification

- `src/ct/theme.py` is the only module that writes a terminal colour. Code asks for a **role** (`strong`, `muted`, `accent`, `accent_chip`, `ok`, `ok_chip`, `warn`, `warn_chip`, `bad`,
  `bad_chip`, `root`, `root_chip`, `info`, `neutral_chip`, `brand`, `brand_dim`, `quiet`, `italic`); `ui.paint(text, ui.GREEN)` and the wizard's own helpers resolve the role when
  they print. `ct-admin theme` lists every role with its meaning. The older names (`GREEN`, `RED`, `BOLD` ...) are aliases of roles.
- The theme is read once, first found wins: `CT_THEME_FILE`, `~/.ct/theme.toml`, `theme.toml` next to the active `ct.toml` (so the server's own messages can have one too).
  `CT_THEME=<name>` picks a built-in theme without a file. Codes must be plain colour numbers (`38;5;75`, `1;48;5;27`); anything else, an unknown role or a
  bad file is ignored with the default look and reported by `ct-admin theme`. A broken theme never stops a command, and nothing but digits and semicolons can reach a terminal.
- Colour is on only for a terminal; `CT_COLOR=always|never` and `NO_COLOR` override that (see `ct.ui`).
- A unit test (`tests/test_theme.py`) fails when any module outside `theme.py` writes an escape colour or a numeric colour, so the rule holds as the tool grows.
- The shape of a message is `ui.say(...)`: a coloured TAG and bold title, aligned labelled rows, steps, then copyable commands, each alone on a line. New commands print with it
  (or `ui.fail` for an error), not with a bare `print`, unless the output is data meant for another program (JSON, a job id, a file's contents).
- **Where to go next.** A command that shows data ends, for a person at a terminal, with the commands that usually come next (`ui.related`): a bold line saying what it does and the
  command on its own line beneath it, for example `ct-run classes` ends by offering to add, remove and run a class. It goes to stderr and only when a person is watching, so a pipe,
  a script or `--json` never sees it; `CT_HINTS=never` turns it off. A command that is wrongly typed gets one message in the same shape (what was wrong, the real choices, the nearest
  command to copy), never argparse's own usage text.
- **Main colour and accent.** The main colour is purple (`primary`, with `primary_chip` for filled labels): rails, section headings, the progress bar, key names, the `TRY IT` breakout and neutral
  headings (`info` follows it). Blue is the accent (`accent`, `accent_chip`, and the `label` role for data labels such as `RISK` and `PROGRAM`). Purple and its chip use exact RGB colours;
  a terminal that cannot show them can set any role to a 256-colour code in `theme.toml`.
- **The header.** The wizard opens with a picture and a caption naming this tool, its job and its version (`ct/setup/brand.py`). The picture is artwork: its colours belong to it and do not follow
  the theme. A terminal under 52 columns gets a one-line header, and with colour off the picture is the same shapes in plain characters.
