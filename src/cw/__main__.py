"""python3 -m cw config check|show [path]"""
import sys

from . import config

if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["config"]:
        sys.exit(config.main(args[1:]))
    print("usage: python3 -m cw config check|show [path]", file=sys.stderr)
    sys.exit(2)
