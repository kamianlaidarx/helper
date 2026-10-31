"""插件契约：框架在加载时会硬校验 on_message，这里把每个内置插件都过一遍。

`wxbot_framework/plugins.py` 实例化插件后立刻断言 on_message 可调用，
不满足直接 raise TypeError("Plugin 必须实现 on_message")。`main.py --check`
不实例化插件，所以这个坑只在真正启动时才炸——必须在测试里挡一次。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
import unittest
import uuid
from pathlib import Path

from wxbot_framework.config import PLUGIN_NAME

PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def bundled():
    for directory in sorted(PLUGINS.iterdir()):
        if directory.is_dir() and PLUGIN_NAME.fullmatch(directory.name):
            yield directory


def load(directory):
    if "_contract_plugins" not in sys.modules:
        package = types.ModuleType("_contract_plugins")
        package.__path__ = [str(PLUGINS)]
        sys.modules[package.__name__] = package
    module_name = f"_contract_plugins.{directory.name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name, directory / "__init__.py", submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class PluginContractTests(unittest.TestCase):
    def test_every_bundled_plugin_implements_on_message(self):
        found = list(bundled())
        self.assertTrue(found)
        for directory in found:
            with self.subTest(plugin=directory.name):
                plugin = load(directory).Plugin()
                self.assertTrue(callable(getattr(plugin, "on_message", None)),
                                f"{directory.name} 缺少 on_message，启动时会 TypeError")

    def test_growth_only_reacts_to_its_own_timer(self):
        plugin = load(PLUGINS / "growth").Plugin()
        self.assertFalse(plugin.on_message(None, None))
        self.assertEqual(plugin.consolidate_now(), {"updated": 0})


if __name__ == "__main__":
    unittest.main()
