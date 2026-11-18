import ast
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from library_backlog import SourcePreparationRefused


class ApiRuntimeTests(unittest.TestCase):
    def source_preparer(self, run):
        tree = ast.parse((Path(__file__).parents[1] / 'api.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == '_configured_source_registration')
        runner = mock.Mock()
        runner.run.return_value = run
        namespace = {'os': os, 'json': json, 'subprocess': runner, 'app': mock.Mock(),
                     'SourcePreparationRefused': SourcePreparationRefused}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'source_preparer', 'exec'), namespace)
        with mock.patch.dict(os.environ, {'CAMELIA_COMIC_AUTOMATION_DATABASE': 'fixture.db'}), \
                mock.patch('os.path.isfile', return_value=True):
            return namespace['_configured_source_registration'](), runner

    def test_source_preparation_accepts_only_bound_known_refusal(self):
        source = r'X:\comix\Series\Book.cbz'
        report = {'schema': 'camelia-source-refusal-v1', 'code': 'non_current_location',
                  'path': source}
        prepare, runner = self.source_preparer(mock.Mock(returncode=3, stdout=json.dumps(report), stderr='refused'))
        with self.assertRaises(SourcePreparationRefused):
            prepare({'source_path': source})
        self.assertEqual(runner.run.call_args.args[0][-3:], ['--prepare-source', source, '--apply'])
        for change in ({'schema':'unknown'}, {'code':'unknown'}, {'path':source + '.other'}, {'path':None}):
            with self.subTest(change=change):
                prepare, _ = self.source_preparer(mock.Mock(returncode=3, stdout=json.dumps(report | change), stderr='refused'))
                with self.assertRaises(RuntimeError) as caught:
                    prepare({'source_path': source})
                self.assertNotIsInstance(caught.exception, SourcePreparationRefused)
        for code, output in ((1,json.dumps(report)), (3,'not json'), (3,'[]')):
            with self.subTest(code=code, output=output):
                prepare, _ = self.source_preparer(mock.Mock(returncode=code, stdout=output, stderr='refused'))
                with self.assertRaises(RuntimeError) as caught:
                    prepare({'source_path': source})
                self.assertNotIsInstance(caught.exception, SourcePreparationRefused)

    def test_source_preparation_success_still_returns_normally(self):
        prepare, _ = self.source_preparer(mock.Mock(returncode=0,
            stdout=json.dumps({'preparation': {'action':'unchanged','archive_id':1}}), stderr=''))
        self.assertIsNone(prepare({'source_path':r'X:\comix\Series\Book.cbz'}))

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
