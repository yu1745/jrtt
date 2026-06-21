"""jrtt entry point: python -m jrtt [args]"""

import sys

from jrtt.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))