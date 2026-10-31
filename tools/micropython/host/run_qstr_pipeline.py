"""Run the upstream cat/sed/GCC/sed pipeline without cmd.exe quote parsing."""

from pathlib import Path
import subprocess
import sys

gcc, response_file, output, *sources = sys.argv[1:]
host = Path(__file__).resolve().parent
data = b"".join(Path(source).read_bytes() for source in sources)
quoted = subprocess.run(
    ["sed", "-f", str(host / "qstr-quote.sed")], input=data, capture_output=True, check=True
).stdout
preprocessed = subprocess.run(
    [gcc, "-E", "@" + response_file, "-"], input=quoted, capture_output=True, check=True
).stdout
result = subprocess.run(
    ["sed", "-f", str(host / "qstr-unquote.sed")],
    input=preprocessed,
    capture_output=True,
    check=True,
).stdout
Path(output).write_bytes(result)
