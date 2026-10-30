"""Execute the actual staged upload barrier in a local filesystem sandbox."""

import binascii
import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "device.py"
SPEC = importlib.util.spec_from_file_location("one_esp_device", SOURCE)
device = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(device)


class UploadPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.firmware = self.root / "firmware"
        self.remote = self.root / "device"
        self.firmware.mkdir()
        self.remote.mkdir()
        self.write(self.firmware, "boot.py", "print('new boot')\n")
        self.write(self.firmware, "components/runtime.py", "label = '温度'\n")
        self.write(self.firmware, "tests/test_private.py", "raise RuntimeError('not firmware')\n")
        self.write(self.firmware, "__pycache__/cache.py", "raise RuntimeError('cache')\n")
        self.write(self.firmware, ".env", "PASSWORD=private\n")
        self.write(self.firmware, ".env.secret.py", "PASSWORD='private'\n")
        self.write(self.firmware, ".mqtt.secret.py", "password='private MQTT fixture'\n")
        self.write(self.firmware, ".mqtt.json", '{"password":"private MQTT fixture"}\n')
        (self.firmware / ".mqtt-ca.der").write_bytes(b"\x30private certificate fixture")
        self.write(self.remote, "boot.py", "print('old boot')\n")
        self.write(self.remote, "components/runtime.py", "label = 'old'\n")
        self.write(self.remote, ".env", "PASSWORD=device-private\n")
        self.write(self.remote, ".mqtt.json", '{"password":"device-private MQTT fixture"}\n')
        (self.remote / ".mqtt-ca.der").write_bytes(b"\x30device private certificate fixture")
        self.backup = self.root / "backup"
        self.backup.mkdir()
        (self.backup / "backup-manifest.json").write_text("{}\n")
        self.renames = []
        self.collected = Mock()

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def staged(self, compiled=None):
        plan = device.build_upload_plan(self.firmware, compiled=compiled)
        copies = []
        for index, argument in enumerate(plan):
            if argument == "fs":
                self.assertEqual(plan[index + 1:index + 3], ["cp", "-f"])
                source, target = plan[index + 3:index + 5]
                self.assertTrue(target.startswith(":"))
                destination = self.remote / target[1:]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(Path(source).read_bytes())
                copies.append(target[1:])
        expected = ["boot.py.upload", "components/runtime.mpy.upload" if compiled is not None
                    else "components/runtime.py.upload"]
        self.assertEqual(copies, expected)
        return plan

    def execute_barrier(self, plan):
        def rename(source, target):
            self.renames.append((source, target))
            (self.remote / source).replace(self.remote / target)

        def open_remote(path, mode="r"):
            return builtins.open(self.remote / path, mode, **({} if "b" in mode else {"encoding": "utf-8"}))

        modules = {"os": SimpleNamespace(rename=rename, stat=lambda path: (self.remote / path).stat()), "ubinascii": binascii,
                   "uhashlib": hashlib, "gc": SimpleNamespace(collect=self.collected)}
        builtins_remote = vars(builtins).copy()
        builtins_remote["open"] = open_remote
        builtins_remote["__import__"] = lambda name, *args: modules[name]
        exec(plan[-1], {"__builtins__": builtins_remote})

    def assert_originals_retained(self):
        self.assertEqual((self.remote / "boot.py").read_text(), "print('old boot')\n")
        self.assertEqual((self.remote / "components/runtime.py").read_text(), "label = 'old'\n")
        self.assertEqual((self.remote / ".env").read_text(), "PASSWORD=device-private\n")
        self.assertFalse(self.renames)

    def test_valid_staged_files_verify_compile_and_replace_without_uploading_private_files(self):
        plan = self.staged()
        self.execute_barrier(plan)
        self.assertEqual((self.remote / "components/runtime.py").read_text(encoding="utf-8"), "label = '温度'\n")
        self.assertEqual((self.remote / ".env").read_text(), "PASSWORD=device-private\n")
        self.assertEqual(len(self.renames), 2)
        self.assertGreaterEqual(self.collected.call_count, 8)
        self.assertFalse(list(self.remote.rglob("*.upload")))

    def test_tampered_last_stage_aborts_before_any_original_is_replaced(self):
        plan = self.staged()
        self.write(self.remote, "components/runtime.py.upload", "label = 'transfer corruption'\n")
        with self.assertRaisesRegex(RuntimeError, "Upload checksum mismatch: components/runtime.py"):
            self.execute_barrier(plan)
        self.assert_originals_retained()

    def test_matching_checksum_but_invalid_last_source_aborts_before_any_rename(self):
        self.write(self.firmware, "components/runtime.py", "if invalid syntax\n")
        plan = self.staged()
        with self.assertRaises(SyntaxError):
            self.execute_barrier(plan)
        self.assert_originals_retained()

    def test_upload_keeps_full_backup_and_resets_even_when_transfer_or_plan_fails(self):
        for failure in ("transfer", "plan"):
            with self.subTest(failure=failure):
                backup = Mock(return_value=self.backup)
                plan = Mock(return_value=["exec", "staged code"])
                transport = Mock()
                if failure == "transfer":
                    transport.side_effect = [RuntimeError("transfer failed"), None]
                else:
                    plan.side_effect = OSError("local source unreadable")
                with patch.object(device, "backup", backup), patch.object(device, "build_upload_plan", plan), patch.object(device, "mpremote", transport):
                    with self.assertRaises((RuntimeError, OSError)):
                        device.upload("COM3")
                backup.assert_called_once_with("COM3", restart=False)
                self.assertEqual(transport.call_args.args, ("COM3", "reset"))

    def compiled(self):
        directory = self.root / "compiled"
        self.write(directory, "boot.py", (self.firmware / "boot.py").read_text())
        module = directory / "components/runtime.mpy"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_bytes(device.MPY_HEADER + b"synthetic compiled module")
        # Directory uploads select only mapped modules, not unrelated/private files.
        self.write(directory, ".mqtt.json", '{"password":"not uploaded"}')
        (directory / ".mqtt-ca.der").write_bytes(b"\x30not uploaded")
        self.write(directory, "tests/test_private.py", "raise RuntimeError('not uploaded')\n")
        return directory

    def test_compiled_stages_verify_before_archiving_source_shadows(self):
        directory = self.compiled()
        mapping = {"boot.py": directory / "boot.py",
                   "components/runtime.mpy": directory / "components/runtime.mpy"}
        plan = self.staged(mapping)
        self.execute_barrier(plan)
        self.assertEqual(self.renames, [
            ("boot.py.upload", "boot.py"),
            ("components/runtime.mpy.upload", "components/runtime.mpy"),
            ("components/runtime.py", "components/runtime.py.source"),
        ])
        self.assertEqual((self.remote / "components/runtime.py.source").read_text(), "label = 'old'\n")
        self.assertEqual((self.remote / "components/runtime.mpy").read_bytes(),
                         (directory / "components/runtime.mpy").read_bytes())
        self.assertEqual((self.remote / ".env").read_text(), "PASSWORD=device-private\n")
        self.assertIn("device-private MQTT fixture", (self.remote / ".mqtt.json").read_text())
        self.assertEqual((self.remote / ".mqtt-ca.der").read_bytes(), b"\x30device private certificate fixture")
        self.assertFalse((self.remote / "components/runtime.py").exists())
        self.assertFalse((self.remote / "boot.mpy").exists())

    def test_compiled_directory_is_accepted_and_existing_archived_sources_are_preserved(self):
        self.write(self.remote, "components/runtime.py.source", "older source backup\n")
        plan = self.staged(self.compiled())
        self.execute_barrier(plan)
        self.assertEqual((self.remote / "components/runtime.py.source").read_text(), "older source backup\n")
        self.assertEqual((self.remote / "components/runtime.py.source.1").read_text(), "label = 'old'\n")
        self.assertFalse((self.remote / "components/runtime.py").exists())

    def test_compiled_digest_failure_retains_all_originals_and_source_shadows(self):
        plan = self.staged(self.compiled())
        (self.remote / "components/runtime.mpy.upload").write_bytes(device.MPY_HEADER + b"corrupt transfer")
        with self.assertRaisesRegex(RuntimeError, "Upload checksum mismatch: components/runtime.mpy"):
            self.execute_barrier(plan)
        self.assert_originals_retained()
        self.assertFalse((self.remote / "components/runtime.py.source").exists())

    def test_staged_mpy_header_is_checked_after_digest_even_if_host_check_is_bypassed(self):
        directory = self.compiled()
        (directory / "components/runtime.mpy").write_bytes(b"M\x06\x02\x1fnot pure bytecode")
        with patch.object(device, "validate_mpy"):
            plan = self.staged(directory)
        with self.assertRaisesRegex(RuntimeError, "Incompatible staged bytecode"):
            self.execute_barrier(plan)
        self.assert_originals_retained()

    def test_compiled_boot_source_is_still_compiled_at_device_barrier(self):
        self.write(self.firmware, "boot.py", "if invalid syntax\n")
        plan = self.staged(self.compiled())
        with self.assertRaises(SyntaxError):
            self.execute_barrier(plan)
        self.assert_originals_retained()

    def test_unexpected_compiled_mapping_private_files_and_boot_substitution_are_rejected(self):
        directory = self.compiled()
        mapping = {"boot.py": directory / "boot.py", "components/runtime.mpy": directory / "components/runtime.mpy"}
        with self.assertRaisesRegex(RuntimeError, "mapping"):
            device.build_upload_plan(self.firmware, {**mapping, ".mqtt.json": directory / ".mqtt.json"})
        with self.assertRaisesRegex(RuntimeError, "mapping"):
            device.build_upload_plan(self.firmware, {"../runtime.mpy": directory / "components/runtime.mpy"})
        self.write(directory, "boot.py", "print('different startup')\n")
        with self.assertRaisesRegex(RuntimeError, "boot.py entrypoint"):
            device.build_upload_plan(self.firmware, directory)

    def test_default_upload_refuses_to_shadow_existing_bytecode_and_still_resets(self):
        (self.backup / "backup-manifest.json").write_text(json.dumps({"/application/runtime.mpy": {}}))
        with patch.object(device, "backup", return_value=self.backup) as backup, \
                patch.object(device, "mpremote") as transport, patch.object(device, "build_upload_plan") as plan:
            with self.assertRaisesRegex(RuntimeError, "upload --precompile"):
                device.upload("COM-test")
        backup.assert_called_once_with("COM-test", restart=False)
        plan.assert_not_called()
        transport.assert_called_once_with("COM-test", "reset")

    def test_precompiled_upload_compiles_then_backs_up_before_executing_shadow_plan(self):
        calls = []
        mapping = {"boot.py": Path("boot.py"), "components/test.mpy": Path("test.mpy")}
        with patch.object(device, "precompile_firmware", side_effect=lambda: calls.append("compile") or mapping), \
                patch.object(device, "backup", side_effect=lambda *a, **k: calls.append("backup") or self.backup), \
                patch.object(device, "build_upload_plan", side_effect=lambda **k: calls.append("plan") or ["exec", "verified plan"]) as plan, \
                patch.object(device, "mpremote", side_effect=lambda *a: calls.append(a[-1])):
            device.upload("COM-test", precompile=True)
        self.assertEqual(calls, ["compile", "backup", "plan", "verified plan", "reset"])
        plan.assert_called_once_with(compiled=mapping)

    def test_host_precompile_failure_does_not_interrupt_the_device(self):
        with patch.object(device, "precompile_firmware", side_effect=RuntimeError("compiler failed")), \
                patch.object(device, "backup") as backup, patch.object(device, "mpremote") as transport:
            with self.assertRaisesRegex(RuntimeError, "compiler failed"):
                device.upload("COM-test", precompile=True)
        backup.assert_not_called()
        transport.assert_not_called()

    def compiler(self, header=None, version=None):
        calls = []

        def run(command, **options):
            calls.append((command, options))
            self.assertEqual(command[:len(device.MPY_CROSS_COMMAND)], list(device.MPY_CROSS_COMMAND))
            self.assertFalse(any(value.startswith("-march") or value.startswith("-O") for value in command))
            if command[-1] == "--version":
                return SimpleNamespace(stdout=version or "MicroPython v1.22.2 on 2024-02-24; mpy-cross emitting mpy v6.2\n")
            output = Path(command[command.index("-o") + 1])
            source_name = command[command.index("-s") + 1]
            self.assertFalse(Path(source_name).is_absolute())
            self.assertEqual(command[-1], source_name)
            data = (Path(options["cwd"]) / source_name).read_bytes()
            output.write_bytes((header if header is not None else device.MPY_HEADER) + hashlib.sha256(data).digest())
            return SimpleNamespace(stdout="")

        return Mock(side_effect=run), calls

    def test_precompile_pins_version_compiles_all_sources_and_keeps_boot_entrypoint(self):
        compiler, calls = self.compiler()
        with patch.object(device.subprocess, "run", compiler):
            mapping = device.precompile_firmware(self.firmware, self.root / "cache")
        self.assertEqual(set(mapping), {"boot.py", "components/runtime.mpy"})
        self.assertEqual([command[-1] for command, _ in calls], ["--version", "boot.py", "components/runtime.py"])
        self.assertEqual(mapping["boot.py"].read_bytes(), (self.firmware / "boot.py").read_bytes())
        self.assertEqual(mapping["components/runtime.mpy"].read_bytes()[:4], b"M\x06\x00\x1f")
        manifest = json.loads((mapping["boot.py"].parent / "build-manifest.json").read_text())
        self.assertEqual(manifest["compiler"], "mpy-cross==1.22.2")
        self.assertEqual(manifest["format"], "6.2")
        self.assertFalse(manifest["native"])
        self.assertEqual(set(manifest["sources"]), {"boot.py", "components/runtime.py"})
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertNotIn("private MQTT fixture", json.dumps(manifest))

    def test_precompile_rejects_wrong_compiler_or_mpy_format_before_upload(self):
        for version in ("MicroPython v1.23.0; mpy-cross emitting mpy v6.2", "MicroPython v1.22.2; mpy-cross emitting mpy v6.1"):
            compiler, calls = self.compiler(version=version)
            with self.subTest(version=version), patch.object(device.subprocess, "run", compiler):
                with self.assertRaisesRegex(RuntimeError, "Expected mpy-cross"):
                    device.precompile_firmware(self.firmware, self.root / "cache")
            self.assertEqual(len(calls), 1)
        for header in (b"X\x06\x00\x1f", b"M\x05\x00\x1f", b"M\x06\x02\x1f", b"M\x06\x28\x1f", b"M\x06\x00\x1e"):
            compiler, _ = self.compiler(header=header)
            with self.subTest(header=header), patch.object(device.subprocess, "run", compiler):
                with self.assertRaisesRegex(RuntimeError, "pure bytecode"):
                    device.precompile_firmware(self.firmware, self.root / "cache")

    def test_precompile_relative_names_produce_reproducible_artifacts(self):
        compiler, _ = self.compiler()
        with patch.object(device.subprocess, "run", compiler):
            first = device.precompile_firmware(self.firmware, self.root / "cache")
            second = device.precompile_firmware(self.firmware, self.root / "cache")
        self.assertNotEqual(first, second)
        for remote in first:
            self.assertEqual(first[remote].read_bytes(), second[remote].read_bytes())


if __name__ == "__main__":
    unittest.main()
