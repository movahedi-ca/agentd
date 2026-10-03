"""agentd: local daemon exposing your own agent CLIs over a loopback HTTP API."""
import sys

from agentd.cli import main

if __name__ == "__main__":
    sys.exit(main())
