"""Back up and upload MicroPython code without copying Wi-Fi secrets or tests."""

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "apps" / "firmware"
DEFAULT_MPY_VERSION = "1.29.0.post2"
# Package post-releases describe the host packaging, not the emitted firmware
# version. Check both the actual compiler version and format before compiling.
MPY_COMPILERS = {
    "1.22.2": ("1.22.2", "6.2"),
    "1.29.0.post2": ("1.29.0", "6.3"),
}
# Both formats encode this same pure-bytecode header: native sub-version and
# architecture flags are absent. ESP32 uses 31-bit small integers.
MPY_HEADER = b"M\x06\x00\x1f"


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


def firmware_sources(firmware=FIRMWARE):
    """Select Python sources only; private configuration/certificates never qualify."""
    firmware = Path(firmware)
    return sorted(path for path in firmware.rglob("*.py")
                   if "tests" not in path.relative_to(firmware).parts
                   and "__pycache__" not in path.parts
                   and not any(part.startswith(".") for part in path.relative_to(firmware).parts))


def validate_mpy(path):
    with Path(path).open("rb") as stream:
        header = stream.read(4)
    if header != MPY_HEADER:
        raise RuntimeError("Expected MicroPython v6 pure bytecode with 31-bit small ints")


def precompile_firmware(firmware=FIRMWARE, build_root=None, mpy_version=DEFAULT_MPY_VERSION):
    """Compile with pinned uv tooling and return {remote_path: local_path}.

    No native architecture or optimization flags are passed. Source filenames
    embedded in bytecode are relative, so artifacts do not expose host paths.
    boot.py is also compilation-checked but remains a source entrypoint: the
    ESP32 startup sequence opens that filename literally, not boot.mpy.
    """
    if not isinstance(mpy_version, str) or mpy_version not in MPY_COMPILERS:
        raise RuntimeError("Unsupported mpy-cross version")
    target_version, mpy_format = MPY_COMPILERS[mpy_version]
    package = "mpy-cross==" + mpy_version
    command = ("uv", "tool", "run", "--from", package, "mpy-cross")
    firmware = Path(firmware).resolve()
    sources = firmware_sources(firmware)
    if not sources:
        raise RuntimeError("No firmware Python sources found")
    version = subprocess.run([*command, "--version"], check=True,
                             capture_output=True, text=True, cwd=firmware)
    expected = (r"\bMicroPython v" + re.escape(target_version) +
                r"(?:\s|$)[^\r\n]*\bmpy v" + re.escape(mpy_format) + r"(?:\s|$)")
    if not re.search(expected, version.stdout):
        raise RuntimeError("Expected mpy-cross " + mpy_version + " emitting mpy v" + mpy_format)
    cache = Path(build_root) if build_root is not None else ROOT / ".device-backups" / "build-mpy"
    target = cache.resolve() / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target.mkdir(parents=True)
    mapping, manifest = {}, {}
    for source in sources:
        relative = source.relative_to(firmware).as_posix()
        compiled_relative = str(Path(relative).with_suffix(".mpy")).replace("\\", "/")
        output = target / compiled_relative
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*command, "-o", str(output), "-s", relative, relative],
                       check=True, capture_output=True, text=True, cwd=firmware)
        validate_mpy(output)
        remote = relative if relative == "boot.py" else compiled_relative
        local = target / "boot.py" if remote == "boot.py" else output
        if remote == "boot.py":
            shutil.copyfile(source, local)
        mapping[remote] = local
        manifest[relative] = {"sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                              "artifact": compiled_relative,
                              "artifactSha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                              "uploadPath": remote}
    (target / "build-manifest.json").write_text(json.dumps({
        "compiler": package, "micropython": target_version, "format": mpy_format,
        "header": list(MPY_HEADER),
        "native": False, "sources": manifest,
    }, indent=2) + "\n", encoding="utf-8")
    return mapping


def _upload_files(firmware, compiled):
    sources = firmware_sources(firmware)
    if compiled is None:
        return {path.relative_to(firmware).as_posix(): path for path in sources}
    expected = {path.relative_to(firmware).as_posix() if path.relative_to(firmware).as_posix() == "boot.py"
                else path.relative_to(firmware).with_suffix(".mpy").as_posix() for path in sources}
    if isinstance(compiled, Mapping):
        if set(compiled) != expected:
            raise RuntimeError("Compiled upload mapping does not match firmware modules")
        result = {remote: Path(compiled[remote]) for remote in sorted(expected)}
    else:
        directory = Path(compiled)
        result = {remote: directory / remote for remote in sorted(expected)}
    for remote, local in result.items():
        if remote.endswith(".mpy"):
            validate_mpy(local)
        elif remote == "boot.py" and local.read_bytes() != (firmware / "boot.py").read_bytes():
            raise RuntimeError("Compiled upload must preserve the firmware boot.py entrypoint")
    return result


def build_upload_plan(firmware=FIRMWARE, compiled=None):
    """Stage source or compiled files and verify all before any replacement.

    compiled accepts precompile_firmware's mapping or its build directory.
    The caller must complete backup() before executing a plan. Compiled plans
    archive source shadows only after every new file has passed the barrier.
    """
    firmware = Path(firmware)
    mapping = _upload_files(firmware, compiled)
    relative, files = list(mapping), list(mapping.values())
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
    finish += "for path in digests:\n gc.collect()\n if path.endswith('.mpy'):\n  with open(path+'.upload','rb') as stream:\n   header=stream.read(4)\n  if header != " + repr(MPY_HEADER) + ":\n   raise RuntimeError('Incompatible staged bytecode: '+path)\n else:\n  with open(path+'.upload') as stream:\n   source=stream.read()\n  compile(source,path,'exec')\n  del source\n gc.collect()\n"
    if compiled is not None:
        # Resolve archive destinations before replacement. Never overwrite a
        # previous .py.source backup; preserve repeated migrations with suffixes.
        finish += "def _source_exists(path):\n try:\n  os.stat(path)\n  return True\n except OSError as error:\n  if error.args and error.args[0] == 2:\n   return False\n  raise\n"
        finish += "shadow_moves=[]\nfor path in digests:\n if path.endswith('.mpy'):\n  source=path[:-4]+'.py'\n  if _source_exists(source):\n   archived=source+'.source'\n   suffix=0\n   while _source_exists(archived):\n    suffix+=1\n    if suffix > 100:\n     raise RuntimeError('Source archive limit reached')\n    archived=source+'.source.'+str(suffix)\n   shadow_moves.append((source,archived))\n"
    finish += "for path in digests:\n os.rename(path+'.upload',path)\n"
    if compiled is not None:
        finish += "for source, archived in shadow_moves:\n os.rename(source,archived)\n"
    finish += "print('Code uploaded; existing device credentials preserved')\n"
    arguments.extend(["exec", finish])
    return arguments


def _has_source_shadows(backup_directory, firmware=FIRMWARE):
    manifest = json.loads((Path(backup_directory) / "backup-manifest.json").read_text(encoding="utf-8"))
    existing = {name.lstrip("/") for name in manifest}
    return any(path.relative_to(firmware).with_suffix(".mpy").as_posix() in existing
               for path in firmware_sources(firmware) if path.relative_to(firmware).as_posix() != "boot.py")


def upload(port, precompile=False, mpy_version=DEFAULT_MPY_VERSION):
    compiled = precompile_firmware(mpy_version=mpy_version) if precompile else None
    snapshot = backup(port, restart=False)
    try:
        if not precompile and _has_source_shadows(snapshot):
            raise RuntimeError("Device has precompiled modules; use upload --precompile to avoid source shadowing")
        mpremote(port, *build_upload_plan(compiled=compiled))
    finally:
        mpremote(port, "reset")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "upload", "status"))
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--bridge", default="http://127.0.0.1:8787")
    parser.add_argument("--precompile", action="store_true",
                        help="upload bytecode built with an isolated pinned compiler; keep boot.py as source")
    parser.add_argument("--mpy-version", choices=tuple(MPY_COMPILERS),
                        help="compiler package version for --precompile (default: " + DEFAULT_MPY_VERSION + ")")
    args = parser.parse_args()
    if args.precompile and args.action != "upload":
        parser.error("--precompile is only available for upload")
    if args.mpy_version is not None and not (args.action == "upload" and args.precompile):
        parser.error("--mpy-version requires upload --precompile")
    if args.action == "status":
        with urlopen(args.bridge.rstrip("/") + "/api/state", timeout=3) as response:
            state = json.load(response)
        print(json.dumps({"connection": state["connection"], "lastSeen": state["lastSeen"]}, indent=2))
        return
    release_bridge(args.bridge)
    if args.action == "backup":
        backup(args.port)
    else:
        upload(args.port, precompile=args.precompile,
               mpy_version=args.mpy_version or DEFAULT_MPY_VERSION)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, URLError) as error:
        print("Device operation failed:", error, file=sys.stderr)
        raise SystemExit(1)
