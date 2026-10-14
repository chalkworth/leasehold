"""A stand-in for a data-preparation job that people start by hand, outside leasehold: it
sleeps for --seconds (default 300)."""

import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=300)
time.sleep(parser.parse_args().seconds)
