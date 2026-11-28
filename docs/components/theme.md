# Colours and the look of the output

## In plain words

Everything the tool prints to a person (questions, results, warnings, waiting messages, the worker cards, the review screens) is written in one shape and one set of
colours: a purple rail (purple is the main colour; blue is the accent, used for data labels), coloured capital labels, a coloured tag for the kind of news (green for done, amber for take care, red for a problem, magenta for anything that runs as root),
and any command you should copy on a line of its own. Those colours live in **one place**. If you want a different look, you change one file; no command needs editing.
When the output is going to a file or a pipe, or `NO_COLOR` is set, there are no colours at all.

To see what you have: `arm-admin theme`. To change it, create `~/.armada/theme.toml`:

```toml
theme = "light"          # default, light (for a white terminal) or mono (no colour, only weight)
[colors]
accent = "38;5;99"       # any role, as terminal colour numbers
```

## Technical specification

- `src/armada/theme.py` is the only module that writes a terminal colour. Code asks for a **role** (`strong`, `muted`, `accent`, `accent_chip`, `ok`, `ok_chip`, `warn`, `warn_chip`, `bad`,
  `bad_chip`, `root`, `root_chip`, `info`, `neutral_chip`, `brand`, `brand_dim`, `quiet`, `italic`); `ui.paint(text, ui.GREEN)` and the wizard's own helpers resolve the role when
  they print. `arm-admin theme` lists every role with its meaning. The older names (`GREEN`, `RED`, `BOLD` ...) are aliases of roles.
- The theme is read once, first found wins: `ARM_THEME_FILE`, `~/.armada/theme.toml`, `theme.toml` next to the active `armada.toml` (so the server's own messages can have one too).
  `ARM_THEME=<name>` picks a built-in theme without a file. Codes must be plain colour numbers (`38;5;75`, `1;48;5;27`); anything else, an unknown role or a
  bad file is ignored with the default look and reported by `arm-admin theme`. A broken theme never stops a command, and nothing but digits and semicolons can reach a terminal.
- Colour is on only for a terminal; `ARM_COLOR=always|never` and `NO_COLOR` override that (see `armada.ui`).
- A unit test (`tests/test_theme.py`) fails when any module outside `theme.py` writes an escape colour or a numeric colour, so the rule holds as the tool grows.
- The shape of a message is `ui.say(...)`: a coloured TAG and bold title, aligned labelled rows, steps, then copyable commands, each alone on a line. New commands print with it
  (or `ui.fail` for an error), not with a bare `print`, unless the output is data meant for another program (JSON, a job id, a file's contents).
- **Where to go next.** A command that shows data ends, for a person at a terminal, with the commands that usually come next (`ui.related`): a bold line saying what it does and the
  command on its own line beneath it, for example `arm-run classes` ends by offering to add, remove and run a class. It goes to stderr and only when a person is watching, so a pipe,
  a script or `--json` never sees it; `ARM_HINTS=never` turns it off. A command that is wrongly typed gets one message in the same shape (what was wrong, the real choices, the nearest
  command to copy), never argparse's own usage text.
- **Main colour and accent.** The main colour is purple (`primary`, with `primary_chip` for filled labels): rails, section headings, the progress bar, key names, the `TRY IT` breakout and neutral
  headings (`info` follows it). Blue is the accent (`accent`, `accent_chip`, and the `label` role for data labels such as `RISK` and `PROGRAM`). Purple and its chip use exact RGB colours;
  a terminal that cannot show them can set any role to a 256-colour code in `theme.toml`.
- **The header.** The wizard opens with a short text caption naming this tool, its job and its version (`armada/setup/brand.py`). There is no picture for now; it will be redesigned with the armada name in mind.
- **Nothing prints on its own.** Every line a person sees goes through `armada.ui` (`say` for a message, `table` for a list, `document` for the text of something), `armada.setup.report.sink` (the setup's
  streamed progress, every line on the card rail) or a renderer in `armada.cards`. A line that must be something else is marked on its line `# output: data` (what a script reads), `log` (a journal line)
  or `passthrough` (another program's own output). `tests/test_output_style.py` fails on any other print, and on any printer that defaults to the builtin `print`.

## Copying the recommended command

When a card at a terminal ends with a "Run this" command, the first command (the recommended one) is also put on the clipboard with the OSC 52 escape sequence, and the card says so. Nothing is typed or run for the person. Terminals that do not support OSC 52 ignore it. It is skipped when output is piped, captured or plain, and `ARM_COPY=never` turns it off. See `ui.offer_copy`.

## Diffs

Everything that shows git changes to a person is drawn the way GitHub draws it (`armada/diffview.py`): added lines on a soft green band, removed on soft red, hunk headers and neutral file changes on blue-grey, each with stronger text of the same colour, full width, wrapped lines keeping their colour, shades following the terminal background. That covers `arm-admin diff` and `apply` (the changes table by file status, and the diff) and the file counts (`+N -M`) in a proposal merge. Piped output stays plain text. A test fails if a diff or line count is shown without these colours.

## Following the terminal

When no theme was chosen (no `ARM_THEME`, no `theme.toml`), the first colour a command asks for follows the terminal's background: a light terminal gets the `light` theme, a dark one the default. The terminal is asked once per run (see `theme.background`; `ARM_BACKGROUND=light|dark` answers for it), and only when both input and output are a terminal, so a piped command never asks. Choosing a theme, by `ARM_THEME` or a file, always wins.


### Code inside a sentence

A command or name written exactly as typed inside a message is wrapped in backticks, and it is shown in the `code` role (a filled background; `src/armada/cards.py` `inline`, used by every card's notes, facts and steps and by the wizard's question text): "run `arm-setup install` again" is never lost in the prose. The backticks stay, inside the coloured span, so output with colour off still shows what is code. A span may run over a line break. Where a message is a whole command to copy, it is still shown in its own "Run this" block.

## Narrow terminals

A card is drawn inside the width the terminal has (`cards.width`, read at each use, at most 100). From 60 columns it is framed; below that it is the rail (a left border only, which costs two columns instead of four), whatever the caller asked for; below 44 each label stands above its value, and a table too wide for its pane becomes labelled lines per row. A command longer than the pane is drawn alone and never cut, so it can be copied whole. `tests/test_narrow_width.py` draws every kind of card at eight widths from 30 to 100 and fails on any line wider than the terminal: a new kind of card is added to its `KINDS` list.
