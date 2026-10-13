import ast
import os
import unittest
from pathlib import Path
from unittest import mock


class ApiRuntimeTests(unittest.TestCase):
    def run_startup(self, environment, scheduled=False):
        tree = ast.parse((Path(__file__).parents[1] / 'api.py').read_text(encoding='utf-8'))
        main = next(node for node in tree.body if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == '__name__')
        app = mock.Mock()
        backlog = mock.Mock()
        backlog.store.schedule_enabled.return_value = scheduled
        backlog.store.scheduled_autostart.return_value = scheduled
        namespace = {'__name__': '__main__', 'os': os, 'app': app,
                     'library_backlog': backlog, 'ensure_directory': mock.Mock(),
                     'CAMELIA_TEMP': 'unused-test-temp', 'CAMELIA_OUTPUT': 'unused-test-output'}
        with mock.patch.dict(os.environ, environment, clear=True):
            exec(compile(ast.Module(body=[main], type_ignores=[]), 'api_startup', 'exec'), namespace)
        return app, backlog

    def test_normal_startup_does_not_reload_or_enable_debugger(self):
        app, backlog = self.run_startup({})
        app.run.assert_called_once_with(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
        backlog.resume_queue.assert_not_called()

    def test_scheduled_autostart_works_in_normal_serving_process(self):
        _, backlog = self.run_startup({}, scheduled=True)
        backlog.resume_queue.assert_called_once_with(retry_failed=False)

    def test_opt_in_debug_parent_does_not_own_scheduled_worker(self):
        app, backlog = self.run_startup({'CAMELIA_DEBUG': 'true'}, scheduled=True)
        self.assertTrue(app.run.call_args.kwargs['use_reloader'])
        backlog.resume_queue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
