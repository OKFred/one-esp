"""Run an upstream Python build script with one argument per UTF-8 file line."""

import runpy
import sys

script, argument_file = sys.argv[1:3]
with open(argument_file, encoding="utf-8") as source:
    sys.argv = [script] + [line.rstrip("\r\n") for line in source if line.rstrip("\r\n")]
runpy.run_path(script, run_name="__main__")
