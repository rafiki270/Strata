"""Guard against terminating unrelated processes when switching models."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


class LifecycleGuards(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        config = self.root/'source.json'
        config.write_text(json.dumps({'args':['--max-context','32768']}))
        registry = self.root/'registry.json'
        registry.write_text(json.dumps({'asset_root':str(self.root),'state_dir':str(self.root),
            'models':[{'label':'DeepSeek','key':'deepseek','kind':'strata','model_id':'deepseek',
                       'config':str(config),'contexts':[32768,262144]}]}))
        self.env = patch.dict(os.environ,{'LOCAL_MODELS_REGISTRY':str(registry)})
        self.env.start()
        spec = importlib.util.spec_from_file_location('launcher',Path(__file__).with_name('local_model_launcher.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.backend = self.module.Backend()
        self.module.STATE.write_text(json.dumps({'pid':123,'created':10,'config':'owned.json','key':'deepseek','context':32768}))

    def tearDown(self):
        self.env.stop()
        self.scratch.cleanup()

    def test_recycled_pid_is_preserved(self):
        process = Mock()
        process.create_time.return_value = 20
        with patch.object(self.module.psutil,'Process',return_value=process):
            self.backend.stop_server()
        process.terminate.assert_not_called()
        process.children.assert_not_called()

    def test_foreign_command_is_preserved(self):
        process = Mock()
        process.create_time.return_value = 10
        process.cmdline.return_value = ['python','other-server.py']
        with patch.object(self.module.psutil,'Process',return_value=process):
            self.backend.stop_server()
        process.terminate.assert_not_called()

    def test_existing_api_is_preserved(self):
        with patch.object(self.backend,'stop_server'), patch.object(self.backend,'owned_process',return_value=None), \
             patch.object(self.module.requests,'get',return_value=Mock()), patch.object(self.module.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError,'occupied'):
                self.backend.start(0)
        spawn.assert_not_called()

    def test_unconfigured_context_is_refused_before_stop(self):
        self.backend.context = 1048576
        with patch.object(self.backend,'stop_server') as stop:
            with self.assertRaises(ValueError):
                self.backend.start(0)
        stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
