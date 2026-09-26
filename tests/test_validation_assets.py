from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from creator_archive.validation_assets import import_verified_assets, validation_display_names
from creator_archive.workflow import ArchiveWorkflow
from creator_archive.validation import Page


class ValidationAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        self.source.mkdir()
        self.workflow = ArchiveWorkflow(Path(self.temp.name) / 'product')
        self.workflow.subscribe('xiaohongshu', 'author-a', 'Existing manual name', verified=True, evidence='test')
        with self.workflow.connect() as db:
            db.execute("INSERT INTO items(platform,item_id,author_id,published_at) VALUES('xiaohongshu','item-a','author-a','')")
        (self.source / 'samples.json').write_text(json.dumps({'XHS-A': {'author_id':'author-a','item_id':'item-a','name':'Observed name'}}))
        self.bytes = b'RIFF0000WEBPtest'
        (self.source / 'XHS-A-image-1.webp').write_bytes(self.bytes)
        self.record = {'sample':'XHS-A-image-1','file':'XHS-A-image-1.webp','sha256':sha256(self.bytes).hexdigest(),'bytes':len(self.bytes),'decoded':True,'position':0,'mime':'image/webp'}
        self.save_record()

    def save_record(self):
        (self.source / 'image-download-results.json').write_text(json.dumps([self.record]))

    def test_hash_verified_idempotent_copy_preserves_source_and_manual_name(self):
        original = (self.source / 'XHS-A-image-1.webp').read_bytes()
        for _ in range(2):
            self.assertEqual(import_verified_assets(self.workflow,self.source)['copied'],1)
        with self.workflow.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM assets').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT display_name FROM subscriptions').fetchone()[0],'Existing manual name')
        self.assertEqual((self.source / 'XHS-A-image-1.webp').read_bytes(),original)
        self.assertEqual(validation_display_names(self.source),{'author-a':'Observed name'})

    def test_bad_hash_and_path_are_not_imported(self):
        self.record['sha256']='bad'
        self.save_record()
        self.assertEqual(import_verified_assets(self.workflow,self.source)['skipped'][0]['reason'],'hash_mismatch')
        self.record['file']='../outside.webp'
        self.save_record()
        self.assertEqual(import_verified_assets(self.workflow,self.source)['skipped'][0]['reason'],'source_unavailable')
        with self.workflow.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM assets').fetchone()[0],0)

    def test_wrong_author_is_not_attached(self):
        (self.source / 'samples.json').write_text(json.dumps({'XHS-A':{'author_id':'wrong','item_id':'item-a'}}))
        self.assertEqual(import_verified_assets(self.workflow,self.source)['skipped'][0]['reason'],'identity_mismatch')


if __name__ == '__main__':
    unittest.main()
