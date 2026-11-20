"""Guard against terminating unrelated processes when switching models."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


class LauncherCase(unittest.TestCase):
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


class LifecycleGuards(LauncherCase):
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

    def test_ollama_switch_preserves_foreign_strata_service(self):
        self.module.ENTRIES.append({'kind':'ollama','model_id':'gemma4:26b'})
        with patch.object(self.backend,'stop_server'), patch.object(self.backend,'ensure_ollama') as ensure, patch.object(self.module.requests,'get',return_value=Mock()):
            with self.assertRaisesRegex(RuntimeError,'occupied'):
                self.backend.start(1)
        ensure.assert_not_called()

    def test_unconfigured_context_is_refused_before_stop(self):
        self.backend.context = 1048576
        with patch.object(self.backend,'stop_server') as stop:
            with self.assertRaises(ValueError):
                self.backend.start(0)
        stop.assert_not_called()


class SupervisedStop(LauncherCase):
    """Stop must also unload a model that a persistent supervisor serves."""
    command = ['python','control.py','stop','--profile','deepseek']

    def setUp(self):
        super().setUp()
        self.serving = 'deepseek'
        self.module.ENTRIES[0].update(supervisor_stop=self.command, source_root=str(self.root))

    def health(self, url, timeout):
        if self.serving is None:
            raise self.module.requests.ConnectionError()
        return Mock(json=Mock(return_value={'model':self.serving,'loaded':True,'max_context':32768}))

    def stop(self, returncode=0, unloads=True):
        def run(command, **_):
            if unloads:
                self.serving = None
            return Mock(returncode=returncode, stdout='', stderr='profile missing')
        with patch.object(self.module.requests,'get',side_effect=self.health), \
             patch.object(self.module.subprocess,'run',side_effect=run) as run_command, \
             patch.object(self.backend,'stop_server') as stop_owned, \
             patch.object(self.backend,'owned_process',return_value=None), \
             patch.object(self.module.time,'sleep'):
            try:
                return self.backend.stop(0), run_command
            finally:
                stop_owned.assert_called_once()

    def test_supervised_model_is_stopped_and_unloaded(self):
        status, run_command = self.stop()
        self.assertEqual(run_command.call_args.args[0], self.command)
        self.assertEqual(run_command.call_args.kwargs['cwd'], str(self.root))
        self.assertEqual(status, 'Offline')

    def test_other_served_model_is_preserved(self):
        self.serving = 'another-model'
        status, run_command = self.stop()
        run_command.assert_not_called()
        self.assertTrue(status.startswith('Unloaded'))

    def test_undeclared_supervisor_is_not_contacted(self):
        del self.module.ENTRIES[0]['supervisor_stop']
        status, run_command = self.stop()
        run_command.assert_not_called()
        self.assertTrue(status.startswith('Ready'))

    def test_failed_stop_command_is_reported(self):
        with self.assertRaisesRegex(RuntimeError,'profile missing'):
            self.stop(returncode=1, unloads=False)

    def test_model_still_running_is_reported(self):
        self.module.ENTRIES[0]['stop_timeout'] = 0
        with self.assertRaisesRegex(RuntimeError,'still running'):
            self.stop(unloads=False)

    def test_window_selects_the_served_model(self):
        self.module.ENTRIES.append({'label':'Qwen','key':'qwen','kind':'strata','model_id':'qwen',
                                    'config':self.module.ENTRIES[0]['config'],'contexts':[32768]})
        self.serving = 'qwen'
        with patch.object(self.module.requests,'get',side_effect=self.health):
            self.assertEqual(self.module.initial_selection(), 1)
        self.serving = None
        with patch.object(self.module.requests,'get',side_effect=self.health):
            self.assertEqual(self.module.initial_selection(), 0)


class HaruLifecycle(LauncherCase):
    def setUp(self):
        super().setUp()
        self.entry = {'key': 'haru', 'kind': 'haru', 'python': str(self.root / 'python.exe'),
                      'checkpoint': str(self.root), 'port': 18998}
        self.module.ENTRIES.append(self.entry)

    def test_incomplete_install_preserves_loaded_model(self):
        Path(self.entry['python']).touch()
        for name in ('config.json', 'tokenizer.json', 'model.safetensors.partial'):
            (self.root / name).touch()
        with patch.object(self.backend, 'stop_server') as stop, self.assertRaisesRegex(RuntimeError, 'checkpoint'):
            self.backend.start(1)
        stop.assert_not_called()

    def test_occupied_port_preserves_loaded_model(self):
        with patch.object(self.backend, 'haru_installed', return_value=True), \
             patch.object(self.module.socket, 'socket') as socket, patch.object(self.backend, 'stop_server') as stop:
            socket.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaisesRegex(RuntimeError, 'port 18998 is occupied'):
                self.backend.start(1)
        stop.assert_not_called()

    def test_unowned_ready_endpoint_is_ignored(self):
        with patch.object(self.backend, 'haru_installed', return_value=True), \
             patch.object(self.module.requests, 'get') as get:
            self.assertEqual(self.backend.status(1), 'Offline')
        get.assert_not_called()

    def test_stopping_haru_does_not_terminate_another_model(self):
        with patch.object(self.module.psutil, 'Process') as process:
            self.backend.stop(1)
        process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
