"""The ePIC adder boundary, without requiring a PanDA server installation."""
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class BaseAdder:
    execute = Mock()


module = ModuleType("pandaserver.dataservice.adder_simple_plugin")
module.AdderSimplePlugin = BaseAdder
spec = importlib.util.spec_from_file_location(
    "epic_adder_under_test",
    Path(__file__).parents[1] / "swf_epicprod/panda/EpicAdderPlugin.py",
)
plugin_module = importlib.util.module_from_spec(spec)
with patch.dict("sys.modules", {module.__name__: module}):
    spec.loader.exec_module(plugin_module)


class EpicAdderTest(unittest.TestCase):
    def setUp(self):
        BaseAdder.execute.reset_mock(side_effect=True, return_value=True)
        self.files = [SimpleNamespace(status=s) for s in
                      ("ready", "nooutput", "failed", "ready")]
        self.plugin = plugin_module.EpicAdderPlugin()
        self.plugin.job = SimpleNamespace(Files=self.files)

    def test_registration_sees_only_available_files_and_restores_all(self):
        seen = []
        BaseAdder.execute.side_effect = lambda: seen.extend(self.plugin.job.Files)
        self.plugin.execute()
        self.assertEqual(seen, [self.files[0], self.files[3]])
        self.assertIs(self.plugin.job.Files, self.files)
        self.assertEqual([f.status for f in self.files],
                         ["ready", "nooutput", "failed", "ready"])

    def test_absent_only_still_uses_upstream_completion(self):
        self.files[:] = [SimpleNamespace(status="nooutput")]
        seen = []
        BaseAdder.execute.side_effect = lambda: seen.append(self.plugin.job.Files)
        self.plugin.execute()
        self.assertEqual(seen, [[]])
        self.assertIs(self.plugin.job.Files, self.files)

    def test_upstream_error_state_is_preserved(self):
        def fail():
            self.plugin.job.ddmErrorCode = 200
            self.plugin.result = "fatal"
        BaseAdder.execute.side_effect = fail
        self.plugin.execute()
        self.assertEqual(self.plugin.job.ddmErrorCode, 200)
        self.assertEqual(self.plugin.result, "fatal")
        self.assertIs(self.plugin.job.Files, self.files)

    def test_exception_restores_files_and_propagates(self):
        BaseAdder.execute.side_effect = RuntimeError("registration failure")
        with self.assertRaisesRegex(RuntimeError, "registration failure"):
            self.plugin.execute()
        self.assertIs(self.plugin.job.Files, self.files)
