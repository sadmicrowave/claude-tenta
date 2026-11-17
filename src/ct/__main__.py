"""python3 -m ct config check|show [path]"""
import sys

from . import config

if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["config"]:
        sys.exit(config.main(args[1:]))
    from . import ui
    ui.say("Nothing to run", "Use: python3 -m ct config check | show [path]", tag="ERROR", color=ui.RED)
    sys.exit(2)
