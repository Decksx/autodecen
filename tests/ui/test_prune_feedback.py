"""Built UI behavior with held API responses; never connects to live Camelia.

Build camelia-ui first. CI installs Playwright Chromium; local runs may set
CAMELIA_TEST_BROWSER=chrome to use an installed browser in a fresh profile.
"""
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

from playwright.sync_api import sync_playwright, expect


class PruneFeedbackTests(unittest.TestCase):
    def test_pending_success_failure_and_background_refresh(self):
        root = Path(__file__).resolve().parents[2] / 'camelia-ui'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with tempfile.TemporaryFile(mode='w+') as log:
            server = subprocess.Popen(['node', '.output/server/index.mjs'], cwd=root,
                env=dict(os.environ, NITRO_HOST='127.0.0.1', NITRO_PORT=str(port)), stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        log.seek(0)
                        self.fail(log.read())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.1)
                self.check_page(port)
            finally:
                server.terminate()
                server.wait(timeout=10)

    def check_page(self, port):
        held, calls, page_errors = [], [], []
        counts = dict.fromkeys(['discovered','eligible','queued','held','processing','completed','failed','quarantined','skipped'], 0)
        preview = dict(missing_count=2, removed_count=0, by_state={'queued':1,'skipped':1},
            kept_count=3, unverified_count=0, unavailable_roots=[], snapshot_digest='a'*64,
            samples=[{'source_path':'C:\\fixture\\Missing.cbz','state':'queued'}])
        headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'GET,POST,OPTIONS',
                   'Access-Control-Allow-Headers':'content-type'}
        def respond(route, data, status=200):
            route.fulfill(status=status, content_type='application/json', body=json.dumps(data), headers=headers)
        def api(route):
            request = route.request
            if request.method == 'OPTIONS':
                respond(route, {})
            elif request.url.endswith('/books/prune-missing'):
                calls.append(request.post_data_json)
                held.append(route)  # keep fetch pending until the test releases it
            elif '/books?' in request.url:
                respond(route, dict(items=[],total=0,counts=counts,limit=50,offset=0))
            else:
                respond(route, dict(allowed_roots=['C:\\fixture'],scan=None,queue_status='paused',known_types_only=False,
                    counts=counts,current_file=None,current_stage=None,recent_books=[],events=[],
                    batch_schedule={'enabled':False,'auto_resume':False,'in_window':False},
                    backup_offload_configured=False,comic_automation_handoff_configured=False,source_registration_configured=False))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get('CAMELIA_TEST_BROWSER') or None, headless=True)
            try:
                page = browser.new_page(viewport={'width':1100,'height':800})
                page.on('pageerror', lambda error: page_errors.append(str(error)))
                page.route('**/api/**', api)
                page.goto(f'http://127.0.0.1:{port}', wait_until='domcontentloaded')
                button = page.get_by_role('button', name='Prune missing files', exact=True)
                button.click()
                expect(page.get_by_role('button', name='Checking missing files…')).to_be_disabled()
                expect(page.get_by_role('status').filter(has_text='Checking every record')).to_be_visible()
                expect(page.get_by_role('status').filter(has_text='Checking every record')).to_contain_text(re.compile(r'[1-9]\d*s elapsed'))
                screenshot = os.environ.get('CAMELIA_UI_FEEDBACK_SCREENSHOT')
                if screenshot:
                    page.get_by_role('button', name='Checking missing files…').locator('..').locator('..').screenshot(path=screenshot)
                expect(page.get_by_text('1s elapsed.')).to_be_visible()
                self.assertEqual(len(held), 1)
                respond(held.pop(), preview)
                panel = page.get_by_role('dialog', name='Prune missing files preview')
                expect(panel).to_be_visible()
                expect(panel).to_be_in_viewport()
                expect(panel).to_be_focused()
                expect(page.get_by_text('Checking every record', exact=False)).to_have_count(0)
                panel.get_by_role('button',name='Cancel',exact=True).click()
                self.assertEqual(calls, [{'dry_run':True}])

                button.click()
                expect(page.get_by_role('status').filter(has_text='Checking every record')).to_be_visible()
                respond(held.pop(), preview)
                expect(panel).to_be_visible()
                panel.get_by_role('button',name='Confirm prune',exact=True).click()
                expect(page.get_by_role('status').filter(has_text='Rechecking and pruning')).to_be_visible()
                expect(panel.get_by_role('button',name='Pruning…')).to_be_disabled()
                expect(page.get_by_role('status').filter(has_text='Rechecking and pruning')).to_contain_text(re.compile(r'[1-9]\d*s elapsed'))
                self.assertEqual(calls[-1], {'dry_run':False,'expected_count':2,'snapshot_digest':'a'*64})
                respond(held.pop(), dict(preview, removed_count=2))
                expect(page.get_by_role('status').filter(has_text='2 missing-file records removed')).to_be_visible()
                expect(panel).to_have_count(0)
                expect(page.get_by_text('Rechecking and pruning',exact=False)).to_have_count(0)

                button.click()
                expect(page.get_by_role('status').filter(has_text='Checking every record')).to_be_visible()
                respond(held.pop(), {'error':'Pause the queue before pruning'}, status=409)
                alert = page.get_by_role('alert').filter(has_text='Pause the queue before pruning')
                expect(alert).to_be_visible()
                # Successful background refresh used to erase the action error.
                page.wait_for_timeout(5500)
                expect(alert).to_be_visible()
                expect(button).to_be_enabled()
                self.assertEqual(page_errors, [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
