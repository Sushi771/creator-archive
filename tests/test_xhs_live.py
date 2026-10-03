"""Synthetic records and real local subprocesses; no platform traffic."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from creator_archive.validation import AdapterFailure
from creator_archive.workflow import ArchiveWorkflow
from scripts.verify_xhs_har import atomic_json
from scripts.verify_xhs_live import BrowserInboxAdapter, validate_record


AUTHOR = 'a' * 24


def record(page=0, more=True):
    return {"authorId": AUTHOR, "requestCursor": f"{page:024x}" if page else "",
            "httpStatus": 200, "success": True, "code": 0,
            "data": {"cursor": f"{page + 1:024x}", "has_more": more,
                     "notes": [{"note_id": f"{page + 10:024x}", "user": {"user_id": AUTHOR}},
                               {"note_id": f"{10:024x}", "user": {"user_id": AUTHOR}}]}}


class LiveConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name)
        self.private = self.local / 'CreatorArchive/private-validation/G1-2026-09-26'
        self.private.mkdir(parents=True)
        (self.private / 'samples.json').write_text(json.dumps({'XHS-A': {'author_id': AUTHOR}}))
        self.root = self.private / 'consumer'
        self.env = dict(os.environ, LOCALAPPDATA=str(self.local))

    def command(self, *extra):
        return [sys.executable, '-m', 'scripts.verify_xhs_live', '--root', str(self.root),
                '--sample', 'XHS-A', *extra]

    def seed(self):
        path = self.private / 'seed.json'
        path.write_text(json.dumps([record(0), record(1)]))
        subprocess.run(self.command('--seed', str(path), '--max-pages', '2'),
                       env=self.env, capture_output=True, check=True, timeout=15)

    def test_new_process_requests_persisted_cursor_and_deduplicates(self):
        self.seed()
        workflow = ArchiveWorkflow(self.root)
        before = workflow.status()['runs'][0]
        self.assertEqual((before['pages'], before['item_count']), (2, 2))
        proc = subprocess.Popen(self.command('--max-pages', '1', '--timeout', '8'),
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'pending.json').exists():
                if time.monotonic() > deadline:
                    self.fail('Consumer did not request its persisted cursor')
                time.sleep(.05)
            ticket = json.loads((self.root / 'pending.json').read_text())
            self.assertEqual(ticket['requestCursor'], before['cursor'])
            response = record(2, False)
            response.update(ticket=ticket['ticket'], capturedAt=datetime.now(timezone.utc).isoformat())
            atomic_json(self.root / (ticket['ticket'] + '.json'), response)
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, stderr)
            after = workflow.status()['runs'][0]
            self.assertEqual((after['pages'], after['item_count'], after['state']), (3, 3, 'succeeded'))
            processes = [json.loads(p.read_text()) for p in self.root.glob('process-*.json')]
            self.assertEqual(len({r['pid'] for r in processes}), 2)
            pending_bytes = (self.root / 'pending.json').read_bytes()
            subprocess.run(self.command('--timeout', '.1'), env=self.env, capture_output=True, check=True, timeout=10)
            self.assertEqual(pending_bytes, (self.root / 'pending.json').read_bytes())
            self.assertEqual(after, workflow.status()['runs'][0])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_old_page_ticket_and_timestamp_are_rejected(self):
        ticket = {'ticket': 'new-ticket', 'requestedAt': '2026-09-26T10:00:00+00:00'}
        valid = record(2)
        valid.update(ticket='new-ticket', capturedAt='2026-09-26T10:00:01+00:00')
        validate_record(valid, AUTHOR, f'{2:024x}', ticket)
        for field, value in [('requestCursor', f'{1:024x}'), ('ticket', 'old-ticket'),
                             ('capturedAt', '2026-09-26T09:59:59+00:00'),
                             ('capturedAt', '2026-09-26T10:00:01')]:
            stale = deepcopy(valid)
            stale[field] = value
            with self.assertRaises(ValueError):
                validate_record(stale, AUTHOR, f'{2:024x}', ticket)

    def test_invalid_envelopes_and_identity_cannot_complete(self):
        for key, value in [('httpStatus', 200.0), ('httpStatus', 429), ('success', 1), ('code', False)]:
            bad = record(2, False)
            bad[key] = value
            with self.assertRaises(AdapterFailure):
                validate_record(bad, AUTHOR, f'{2:024x}')
        for user in [None, [], {'user_id': 'b' * 24}]:
            bad = record(2, False)
            bad['data']['notes'][0]['user'] = user
            with self.assertRaises(ValueError):
                validate_record(bad, AUTHOR, f'{2:024x}')

    def test_timeout_preserves_checkpoint_and_does_not_complete(self):
        self.seed()
        before = ArchiveWorkflow(self.root).status()['runs'][0]
        subprocess.run(self.command('--timeout', '.1'), env=self.env, capture_output=True, check=True, timeout=10)
        after = ArchiveWorkflow(self.root).status()['runs'][0]
        self.assertEqual((after['cursor'], after['pages'], after['item_count']),
                         (before['cursor'], before['pages'], before['item_count']))
        self.assertEqual((after['state'], after['reason'], after['terminal_evidence']), ('partial', 'timeout', None))

    def test_ssr_not_terminal_and_conflicting_seed_rejected(self):
        ssr = record(0, False)
        ssr['source'] = 'server_rendered_document'
        with self.assertRaises(ValueError):
            validate_record(ssr, AUTHOR, '')
        changed = record(0)
        changed['data']['notes'][0]['note_id'] = 'f' * 24
        with self.assertRaisesRegex(ValueError, 'conflicting_seed'):
            BrowserInboxAdapter(self.root, [record(0), changed], .1)

    def test_unrelated_private_files_preserved(self):
        self.root.mkdir()
        manual = self.root / 'pending.json'
        manual.write_text('manual note')
        result = subprocess.run(self.command('--timeout', '.1'), env=self.env, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(manual.read_text(), 'manual note')
        self.assertFalse((self.root / 'consumer.json').exists())


if __name__ == '__main__':
    unittest.main()
