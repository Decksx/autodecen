import os
import io
import queue
import shutil
import tempfile
import unittest
import zipfile
import re
from pathlib import Path
from unittest import mock

from PIL import Image
from werkzeug.datastructures import MultiDict

import api
import aletheia_integration
import main as camelia_main
from scripts import aletheia_mosaic
from scripts import process_cbz


class ProcessingUpdateTests(unittest.TestCase):
    def test_successful_backup_offload_accepts_missing_command_output(self):
        completed = mock.Mock(returncode=0, stdout=None, stderr=None)
        with mock.patch.object(api.os.path, 'isfile', return_value=True), \
                mock.patch.object(api.os.path, 'isdir', return_value=True), \
                mock.patch.object(api.shutil, 'disk_usage', return_value=mock.Mock(free=2**40)), \
                mock.patch.object(api.subprocess, 'run', return_value=completed) as run:
            offload = api._configured_backup_offload()
            self.assertIsNotNone(offload)
            offload({'source_path': 'book.cbz'}, {'backup_path': 'original.cbz'})
        self.assertIn('--source-file', run.call_args.args[0])

    def test_job_logs_use_full_local_timestamp_and_pass_duration(self):
        session_id = 'timing-test'
        api.process_logs[session_id] = queue.Queue()

        def fake_pass(paths, _model_type, _session, _number, _count, work_dir):
            output = os.path.join(work_dir, 'output.png')
            Path(output).write_bytes(b'output')
            return [{'filename': 'page.png', 'processed_path': output}]

        with tempfile.TemporaryDirectory() as work_dir, mock.patch.object(
            api, '_validate_runtime_files'
        ), mock.patch.object(
            api, '_run_model_pass', side_effect=fake_pass
        ), mock.patch.object(
            api.time, 'perf_counter', side_effect=[100.0, 477.0]
        ):
            api.process_images(
                [os.path.join(work_dir, 'source.png')],
                ['black_bars'],
                session_id,
                work_dir,
            )

        messages = []
        while not api.process_logs[session_id].empty():
            messages.append(api.process_logs[session_id].get())
        self.assertTrue(all(re.match(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ', line)
                            for line in messages))
        self.assertTrue(any(
            '[PASS] Completed black_bars (1/1): 1 output image(s) (6m 17s)' in line
            for line in messages
        ))

    def test_mosaic_output_uses_utf8_even_when_windows_locale_is_cp1252(self):
        non_ascii_line = '[Aletheia-Lens] processed Ӑ\n'
        calls = []

        class FakeProcess:
            def __init__(self, options):
                self.stdout = io.TextIOWrapper(
                    io.BytesIO(non_ascii_line.encode('utf-8')),
                    encoding=options.get('encoding', 'cp1252'),
                    errors=options.get('errors', 'strict'),
                )

            def wait(self):
                return 0

        def fake_popen(_command, **options):
            calls.append(options)
            return FakeProcess(options)

        with mock.patch.object(camelia_main, 'build_mosaic_command', return_value=['fake']), \
                mock.patch.object(camelia_main.subprocess, 'Popen', side_effect=fake_popen):
            self.assertTrue(camelia_main.run_mosaic('input', 'output', 'workspace'))

        self.assertEqual(calls[0]['encoding'], 'utf-8')
        self.assertEqual(calls[0]['errors'], 'replace')

    def test_mosaic_is_a_supported_ordered_stage(self):
        self.assertEqual(
            api.normalize_model_types(['black_bars', 'mosaic', 'mosaic']),
            ['black_bars', 'mosaic'],
        )

    def test_aletheia_adapter_preserves_names_and_processes_each_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / 'input'
            output_dir = root / 'output'
            input_dir.mkdir()
            Image.new('RGBA', (4, 4), (255, 0, 0, 128)).save(input_dir / '002.webp')
            Image.new('RGB', (4, 4), (0, 0, 255)).save(input_dir / '001.jpg')
            calls = []

            def processor(payload):
                calls.append(payload)
                return Image.new('RGB', (4, 4), (0, 255, 0))

            count = aletheia_mosaic.process_directory(input_dir, output_dir, processor)

            self.assertEqual(count, 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(sorted(path.name for path in output_dir.iterdir()), ['001.jpg', '002.webp'])
            for output in output_dir.iterdir():
                with Image.open(output) as image:
                    self.assertEqual(image.format, 'JPEG' if output.suffix == '.jpg' else 'WEBP')

    def test_aletheia_adapter_copies_undetected_pages_without_reencoding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / 'input'
            input_dir.mkdir()
            source = input_dir / '001.jpg'
            Image.new('RGB', (4, 4), (255, 0, 0)).save(source)
            output_dir = root / 'output'
            aletheia_mosaic.process_directory(input_dir, output_dir, lambda _payload: None)
            self.assertEqual((output_dir / source.name).read_bytes(), source.read_bytes())

    def test_aletheia_validation_reports_setup_command(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            aletheia_integration, 'aletheia_python', return_value=Path(temp_dir) / 'missing-python.exe'
        ):
            with self.assertRaisesRegex(FileNotFoundError, 'setup_aletheia.ps1'):
                aletheia_integration.validate_aletheia_runtime(Path(temp_dir))

    def test_avif_cbz_pages_are_extracted_and_reencoded_as_avif(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = io.BytesIO()
            Image.new('RGB', (8, 8), (255, 0, 0)).save(original, format='AVIF')
            source = root / 'Book.cbz'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('pages/001.avif', original.getvalue())

            extracted, _details = api.extract_cbz_images(
                str(source),
                str(root / 'stage'),
                set(),
            )
            self.assertEqual(Path(extracted[0]).suffix.lower(), '.avif')

            processed = root / 'processed.png'
            Image.new('RGB', (8, 8), (0, 255, 0)).save(processed, format='PNG')
            encoded = api._encode_processed_page(
                str(processed),
                original.getvalue(),
                'avif',
            )
            with Image.open(io.BytesIO(encoded)) as rebuilt:
                self.assertEqual(rebuilt.format, 'AVIF')
                self.assertGreater(
                    rebuilt.convert('RGB').getpixel((0, 0))[1],
                    rebuilt.convert('RGB').getpixel((0, 0))[0],
                )

    def test_model_list_is_ordered_deduplicated_and_backward_compatible(self):
        form = MultiDict([
            ('model_types', '["black_bars", "transparent_black", "black_bars"]')
        ])
        self.assertEqual(
            api.parse_model_types(form),
            ['black_bars', 'transparent_black'],
        )
        self.assertEqual(
            api.parse_model_types(MultiDict([('model_type', 'white_bars')])),
            ['white_bars'],
        )
        self.assertEqual(
            api.normalize_model_types(
                ['transparent_black', 'black_bars', 'transparent_black']
            ),
            ['transparent_black', 'black_bars'],
        )

    def test_destination_filter_uses_directory_segments_not_substrings(self):
        self.assertTrue(api.path_is_under_comix(r'C:\Library\Comix\Book.cbz'))
        self.assertFalse(api.path_is_under_comix(r'C:\Library\Comixology\Book.cbz'))
        self.assertFalse(api.path_is_under_comix(r'C:\Library\Manga\Book.cbz'))

    def test_sequential_pass_uses_previous_pass_outputs(self):
        session_id = 'sequence-test'
        api.process_logs[session_id] = queue.Queue()
        calls = []

        def fake_pass(paths, model_type, _session, number, count, work_dir):
            calls.append((list(paths), model_type, number, count))
            output = os.path.join(work_dir, f'{number}.png')
            Path(output).write_bytes(b'output')
            return [{'filename': 'page.png', 'processed_path': output}]

        with tempfile.TemporaryDirectory() as work_dir:
            source = os.path.join(work_dir, 'source.png')
            Path(source).write_bytes(b'input')
            with mock.patch.object(api, '_validate_runtime_files'), mock.patch.object(
                api, '_run_model_pass', side_effect=fake_pass
            ):
                _, results = api.process_images(
                    [source],
                    ['black_bars', 'transparent_black'],
                    session_id,
                    work_dir,
                )

        self.assertEqual([call[1] for call in calls], ['black_bars', 'transparent_black'])
        self.assertEqual(calls[1][0], [calls[0][0][0].replace('source.png', '1.png')])
        self.assertEqual(results[0]['filename'], 'page.png')

    def test_failed_pass_is_logged_with_its_sequence_position(self):
        session_id = 'sequence-failure-test'
        api.process_logs[session_id] = queue.Queue()

        with tempfile.TemporaryDirectory() as work_dir, mock.patch.object(
            api, '_validate_runtime_files'
        ), mock.patch.object(
            api,
            '_run_model_pass',
            side_effect=RuntimeError('injected failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                api.process_images(
                    [os.path.join(work_dir, 'source.png')],
                    ['black_bars', 'transparent_black'],
                    session_id,
                    work_dir,
                )

        messages = []
        while not api.process_logs[session_id].empty():
            messages.append(api.process_logs[session_id].get())
        self.assertTrue(any(
            'Failed black_bars (1/2): RuntimeError: injected failure' in message
            for message in messages
        ))

    def test_cbz_cli_passes_the_ordered_sequence_to_the_shared_pipeline(self):
        session_id = 'cli-sequence-test'
        api.process_logs[session_id] = queue.Queue()
        result = {
            'path': r'C:\Library\Comix\Book.cbz',
            'backup_path': r'C:\Backups\Book.cbz',
        }
        with mock.patch.object(
            process_cbz,
            'process_cbz_archive_sync',
            return_value=(result, session_id),
        ) as process_archive, mock.patch('builtins.print'):
            return_code = process_cbz.main([
                'Book.cbz',
                '--model-type', 'black_bars',
                '--model-type', 'transparent_black',
                '--replace',
                '--backup-dir', r'C:\Backups',
                '--destination-dir', r'X:\Library\Comix',
            ])

        self.assertEqual(return_code, 0)
        self.assertEqual(
            process_archive.call_args.kwargs['model_types'],
            ['black_bars', 'transparent_black'],
        )
        self.assertEqual(
            process_archive.call_args.kwargs['destination_dir'],
            Path(r'X:\Library\Comix'),
        )

    def test_sync_cbz_filter_uses_the_planned_destination_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            incoming = Path(temp_dir) / 'incoming' / 'Book.cbz'
            incoming.parent.mkdir()
            incoming.write_bytes(b'not-a-real-archive')

            with self.assertRaisesRegex(ValueError, 'not under a comix directory'):
                api.process_cbz_archive_sync(
                    incoming,
                    destination_dir=Path(temp_dir) / 'Manga',
                )

            with self.assertRaises(Exception) as accepted_destination:
                api.process_cbz_archive_sync(
                    incoming,
                    destination_dir=Path(temp_dir) / 'Comix',
                )

        self.assertNotIn(
            'not under a comix directory',
            str(accepted_destination.exception).lower(),
        )

    def test_sync_cbz_skips_already_recorded_method_without_running_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / 'Comix' / 'Book.cbz'
            archive.parent.mkdir()
            with zipfile.ZipFile(archive, 'w') as cbz:
                cbz.writestr('001.jpg', b'page')
                cbz.writestr('ComicInfo.xml', '<ComicInfo><Tags>uncensored, camelia:black_bars</Tags></ComicInfo>')
            with mock.patch.object(api, 'process_images_thread') as pipeline:
                result, session_id = api.process_cbz_archive_sync(archive, model_types=['black_bars'])
            self.assertEqual(result['status'], 'skipped')
            self.assertEqual(result['applied_methods'], ['black_bars'])
            self.assertEqual(api.process_status[session_id], 'completed')
            pipeline.assert_not_called()
        self.assertTrue(process_cbz.build_parser().parse_args(['book.cbz', '--reprocess']).reprocess)

    def test_local_source_listing_filters_by_computed_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / 'Library'
            comix = library / 'Comix' / 'Series'
            manga = library / 'Manga' / 'Series'
            comix.mkdir(parents=True)
            manga.mkdir(parents=True)
            (comix / 'keep.cbz').write_bytes(b'not-opened-by-listing')
            (manga / 'skip.cbz').write_bytes(b'not-opened-by-listing')

            response = api.app.test_client().post(
                '/api/local-cbz-sources',
                json={
                    'source_paths': [str(library)],
                    'output_location': 'automatic',
                },
                headers={'Origin': 'http://localhost:3000'},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item['name'] for item in payload['sources']], ['keep.cbz'])
        self.assertEqual([item['name'] for item in payload['skipped']], ['skip.cbz'])
        self.assertIn('not under a comix', payload['skipped'][0]['reason'].lower())

    def test_flask_process_flow_accepts_an_ordered_model_sequence(self):
        captured = {}

        class FakeThread:
            def __init__(self, target, args):
                captured['target'] = target
                captured['args'] = args
                self.daemon = False

            def start(self):
                captured['started'] = True

        with mock.patch.object(api.threading, 'Thread', FakeThread):
            response = api.app.test_client().post(
                '/api/process',
                data={
                    'files': (io.BytesIO(b'image-placeholder'), 'page.png'),
                    'model_types': '["black_bars", "transparent_black"]',
                    'output_location': 'automatic',
                },
                content_type='multipart/form-data',
            )

        try:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(captured['started'])
            self.assertEqual(
                captured['args'][1],
                ['black_bars', 'transparent_black'],
            )
        finally:
            if captured.get('args'):
                shutil.rmtree(captured['args'][3], ignore_errors=True)

    def test_multiline_failure_details_are_valid_server_sent_events(self):
        session_id = 'sse-test'
        api.process_logs[session_id] = queue.Queue()
        api.process_logs[session_id].put('RuntimeError: failed\ntraceback detail')
        api.process_status[session_id] = 'error'
        response = api.app.test_client().get(f'/api/logs/{session_id}')
        body = response.get_data(as_text=True)
        self.assertIn('data: RuntimeError: failed\ndata: traceback detail\n\n', body)

    @unittest.skipUnless(os.name == 'nt', 'Windows extended paths are Windows-specific')
    def test_long_path_copy_uses_extended_length_io(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            long_parent = os.path.join(temp_dir, *(('segment-' + str(i).zfill(2)) * 2 for i in range(18)))
            source = os.path.join(long_parent, 'source.cbz')
            destination = os.path.join(temp_dir, 'staged.cbz')
            api.ensure_directory(long_parent)
            with open(api.filesystem_path(source), 'wb') as source_file:
                source_file.write(b'cbz-data')
            api.copy_file_long_path(source, destination)
            self.assertEqual(Path(destination).read_bytes(), b'cbz-data')


if __name__ == '__main__':
    unittest.main()
