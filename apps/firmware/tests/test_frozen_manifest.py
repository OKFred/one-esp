"""Check the application import closure without building or flashing firmware."""

import ast
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

FIRMWARE = Path(__file__).resolve().parents[1]
REPOSITORY = FIRMWARE.parents[1]


class FrozenManifestTests(unittest.TestCase):
    def test_all_internal_runtime_imports_are_in_the_frozen_manifest(self):
        frozen = set()
        def freeze(directory, sources, **_):
            if Path(directory) == FIRMWARE:
                frozen.update(sources)
        with patch.dict(os.environ, {'ONE_ESP_FIRMWARE': str(FIRMWARE),
                                     'ONE_ESP_RECIPES': str(REPOSITORY / 'tools/micropython')}):
            runpy.run_path(str(REPOSITORY / 'tools/micropython/manifest.py'),
                           init_globals={'include': lambda _: None, 'freeze': freeze})
        self.assertTrue(frozen)
        for filename in sorted(frozen):
            source = FIRMWARE / filename
            self.assertTrue(source.is_file(), filename)
            for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split('.')[0] in ('application', 'components', 'lib'):
                        modules = ([node.module + '.' + name.name for name in node.names]
                                   if '.' not in node.module else [node.module])
                        for imported in modules:
                            with self.subTest(source=filename, imported=imported):
                                self.assertIn(imported.replace('.', '/') + '.py', frozen)


if __name__ == '__main__':
    unittest.main()
