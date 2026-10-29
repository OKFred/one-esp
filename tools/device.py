"""Back up and upload MicroPython code without copying Wi-Fi secrets or tests."""

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "apps" / "firmware"


def release_bridge(url):
    request = Request(url.rstrip("/") + "/api/disconnect", data=b"{}",
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise RuntimeError("Bridge did not release its serial connection")
    except URLError as error:
        if getattr(error, "code", None):
            raise RuntimeError("Bridge refused to release its serial connection") from error
        # A stopped local bridge has no serial connection to release.


def mpremote(port, *arguments):
    if importlib.util.find_spec("mpremote") is None:
        raise RuntimeError("Run uv sync --locked from the repository root first")
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "resume", *arguments],
                   check=True, cwd=ROOT)


def backup(port, restart=True):
    from mpremote.transport_serial import SerialTransport

    target = ROOT / ".device-backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target.mkdir(parents=True)
    transport = SerialTransport(port)
    entered = False
    complete = False
    manifest = {}
    try:
        transport.enter_raw_repl(soft_reset=False)
        entered = True
        transport.exec("import os, ubinascii, uhashlib, gc; gc.collect()")

        def copy_directory(remote, local):
            for name, mode, *_ in transport.eval("list(os.ilistdir(" + repr(remote) + "))"):
                # Never allow a device filename to escape the local backup folder.
                if name in (".", "..") or "/" in name or "\\" in name:
                    raise RuntimeError("Unsafe device filename")
                source = remote.rstrip("/") + "/" + name
                destination = local / name
                if mode & 0x4000:
                    destination.mkdir()
                    copy_directory(source, destination)
                    continue
                transport.exec("_backup_file=open(" + repr(source) + ",'rb')")
                digest = hashlib.sha256()
                offset = 0
                # Hex avoids older MicroPython bytes repr containing non-UTF8 bytes.
                with destination.open("wb") as stream:
                    while True:
                        for attempt in range(3):
                            encoded = transport.exec("_backup_file.seek(" + str(offset) + "); _backup_chunk=_backup_file.read(128); print(ubinascii.hexlify(_backup_chunk).decode())")
                            try:
                                chunk = bytes.fromhex(encoded.decode('ascii').strip())
                                break
                            except (UnicodeError, ValueError):
                                if attempt == 2:
                                    raise RuntimeError("Device backup transfer is corrupt; no files were uploaded")
                        if not chunk:
                            break
                        stream.write(chunk)
                        digest.update(chunk)
                        offset += len(chunk)
                remote_digest = transport.exec("_backup_file.seek(0); _backup_hash=uhashlib.sha256()\nwhile True:\n _backup_chunk=_backup_file.read(256)\n if not _backup_chunk: break\n _backup_hash.update(_backup_chunk)\nprint(ubinascii.hexlify(_backup_hash.digest()).decode())\n_backup_file.close()").decode('ascii').strip()
                if digest.hexdigest() != remote_digest:
                    raise RuntimeError("Device backup checksum mismatch: " + source)
                manifest[source] = {"sha256": remote_digest, "size": destination.stat().st_size}

        copy_directory("/", target)
        (target / "backup-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        complete = True
    finally:
        try:
            if entered and (restart or not complete):
                transport.exec_raw_no_follow("import machine; machine.reset()")
        finally:
            transport.close()
    print("Device backup:", target)
    return target


def build_upload_plan(firmware=FIRMWARE):
    """Prepare staged copies and a checksum/compile barrier before any rename."""
    files = sorted(path for path in firmware.rglob("*.py")
                   if "tests" not in path.relative_to(firmware).parts
                   and "__pycache__" not in path.parts
                   and not any(part.startswith(".env") for part in path.relative_to(firmware).parts))
    relative = [path.relative_to(firmware).as_posix() for path in files]
    digests = {remote: hashlib.sha256(path.read_bytes()).hexdigest()
               for path, remote in zip(files, relative)}
    directories = sorted({str(Path(path).parent).replace("\\", "/")
                          for path in relative if "/" in path}, key=lambda path: path.count("/"))
    setup = "import os\nfor path in " + repr(directories) + ":\n try:\n  os.mkdir(path)\n except OSError:\n  pass\n"
    arguments = ["exec", setup]
    for path, remote in zip(files, relative):
        # Old MicroPython bytes repr can corrupt mpremote's binary hash reply.
        # Always copy; the ASCII-hex verification below replaces its skip check.
        arguments.extend(["fs", "cp", "-f", str(path), ":" + remote + ".upload", "+"])
    # Verify every staged file, then compile every file, before replacing any.
    # Small chunks and collection between files leave space for the compiler.
    finish = "import os, ubinascii, uhashlib, gc\ndigests=" + repr(digests) + "\n"
    finish += "for path, expected in digests.items():\n gc.collect()\n digest=uhashlib.sha256()\n with open(path+'.upload','rb') as stream:\n  while True:\n   chunk=stream.read(256)\n   if not chunk: break\n   digest.update(chunk)\n if ubinascii.hexlify(digest.digest()).decode('ascii') != expected:\n  raise RuntimeError('Upload checksum mismatch: '+path)\n del digest, chunk\n gc.collect()\n"
    finish += "for path in digests:\n gc.collect()\n with open(path+'.upload') as stream:\n  source=stream.read()\n compile(source,path,'exec')\n del source\n gc.collect()\n"
    finish += "for path in digests:\n os.rename(path+'.upload',path)\n"
    finish += "print('Code uploaded; existing device credentials preserved')\n"
    arguments.extend(["exec", finish])
    return arguments


def upload(port):
    backup(port, restart=False)
    try:
        mpremote(port, *build_upload_plan())
    finally:
        mpremote(port, "reset")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "upload", "status"))
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--bridge", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    if args.action == "status":
        with urlopen(args.bridge.rstrip("/") + "/api/state", timeout=3) as response:
            state = json.load(response)
        print(json.dumps({"connection": state["connection"], "lastSeen": state["lastSeen"]}, indent=2))
        return
    release_bridge(args.bridge)
    if args.action == "backup":
        backup(args.port)
    else:
        upload(args.port)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, URLError) as error:
        print("Device operation failed:", error, file=sys.stderr)
        raise SystemExit(1)
