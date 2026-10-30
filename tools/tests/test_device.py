"""Execute the actual staged upload barrier in a local filesystem sandbox."""

import binascii
import builtins
import hashlib
import importlib.util
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
        self.write(self.remote, "boot.py", "print('old boot')\n")
        self.write(self.remote, "components/runtime.py", "label = 'old'\n")
        self.write(self.remote, ".env", "PASSWORD=device-private\n")
        self.renames = []
        self.collected = Mock()

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def staged(self):
        plan = device.build_upload_plan(self.firmware)
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
        self.assertEqual(copies, ["boot.py.upload", "components/runtime.py.upload"])
        return plan

    def execute_barrier(self, plan):
        def rename(source, target):
            self.renames.append((source, target))
            (self.remote / source).replace(self.remote / target)

        def open_remote(path, mode="r"):
            return builtins.open(self.remote / path, mode, **({} if "b" in mode else {"encoding": "utf-8"}))

        modules = {"os": SimpleNamespace(rename=rename), "ubinascii": binascii,
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
                backup = Mock()
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


if __name__ == "__main__":
    unittest.main()
