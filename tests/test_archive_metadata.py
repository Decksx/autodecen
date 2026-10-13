"""ZIP rebuild regressions using synthetic, non-image archive contents."""
import ast
import copy
import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path


def archive_helpers():
    # Load the pure archive helpers without initializing the live queue/database.
    tree = ast.parse((Path(__file__).parents[1] / 'api.py').read_text(encoding='utf-8'))
    names = {'_zip_extra_signature', '_zip_info_signature', '_copy_archive_with_processed_pages', '_verify_rebuilt_archive'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'copy': copy, 'zipfile': zipfile}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'archive_helpers', 'exec'), namespace)
    return namespace


class ArchiveMetadataTests(unittest.TestCase):
    def setUp(self):
        self.helpers = archive_helpers()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source.cbz'
        self.rebuilt = Path(self.temp.name) / 'rebuilt.cbz'

    def make_source(self, *, utf8=False, attributes=0):
        info = zipfile.ZipInfo('ComicInfo.xml', (2026, 2, 23, 16, 47, 24))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 0
        info.external_attr = attributes
        info.comment = b'metadata comment'
        info.extra = b'\xfe\xca\x02\x00ok'
        with zipfile.ZipFile(self.source, 'w') as archive:
            archive.comment = b'archive comment'
            archive.writestr(info, b'<ComicInfo><Title>Test Book</Title></ComicInfo>')
            info.external_attr = attributes
        if utf8:
            raw = bytearray(self.source.read_bytes())
            for signature, offset in [(b'PK\x03\x04', 6), (b'PK\x01\x02', 8)]:
                position = raw.index(signature) + offset
                flags = struct.unpack_from('<H', raw, position)[0]
                struct.pack_into('<H', raw, position, flags | 0x800)
            self.source.write_bytes(raw)

    def rebuild(self):
        self.helpers['_copy_archive_with_processed_pages'](self.source, self.rebuilt, [], [])

    def verify(self):
        self.helpers['_verify_rebuilt_archive'](self.source, self.rebuilt, [], [])

    def test_preserves_explicit_zero_external_attributes(self):
        self.make_source()
        self.rebuild()
        self.verify()
        with zipfile.ZipFile(self.rebuilt) as archive:
            self.assertEqual(archive.getinfo('ComicInfo.xml').external_attr, 0)

    def test_ascii_filename_may_normalize_utf8_encoding_flag(self):
        self.make_source(utf8=True, attributes=32)
        self.rebuild()
        self.verify()

    def test_changed_non_image_payload_is_still_rejected(self):
        self.make_source(attributes=32)
        with zipfile.ZipFile(self.source) as source, zipfile.ZipFile(self.rebuilt, 'w') as rebuilt:
            rebuilt.comment = source.comment
            rebuilt.writestr(copy.copy(source.getinfo('ComicInfo.xml')), b'<ComicInfo/>')
        with self.assertRaisesRegex(RuntimeError, 'Non-image archive member changed'):
            self.verify()

    def test_changed_member_timestamp_is_still_rejected(self):
        self.make_source(attributes=32)
        with zipfile.ZipFile(self.source) as source, zipfile.ZipFile(self.rebuilt, 'w') as rebuilt:
            rebuilt.comment = source.comment
            member = copy.copy(source.getinfo('ComicInfo.xml'))
            member.date_time = (2025, 1, 1, 0, 0, 0)
            rebuilt.writestr(member, source.read(member.filename))
        with self.assertRaisesRegex(RuntimeError, 'ZIP metadata changed'):
            self.verify()

    def test_changed_nonzero_file_attributes_are_still_rejected(self):
        self.make_source(attributes=32)
        with zipfile.ZipFile(self.source) as source, zipfile.ZipFile(self.rebuilt, 'w') as rebuilt:
            rebuilt.comment = source.comment
            member = copy.copy(source.getinfo('ComicInfo.xml'))
            member.external_attr = 16
            rebuilt.writestr(member, source.read(member.filename))
        with self.assertRaisesRegex(RuntimeError, 'ZIP metadata changed'):
            self.verify()

    def test_zip64_storage_offsets_are_regenerated(self):
        self.make_source(attributes=32)
        raw = bytearray(self.source.read_bytes())
        central = raw.index(b'PK\x01\x02')
        end = raw.index(b'PK\x05\x06')
        name_size, extra_size = struct.unpack_from('<HH', raw, central + 28)
        insertion = central + 46 + name_size + extra_size
        zip64 = struct.pack('<HHQ', 1, 8, 0)
        raw[insertion:insertion] = zip64
        struct.pack_into('<H', raw, central + 30, extra_size + len(zip64))
        struct.pack_into('<I', raw, central + 42, 0xffffffff)
        struct.pack_into('<H', raw, central + 4, 45)
        struct.pack_into('<H', raw, central + 6, 45)
        old_size = struct.unpack_from('<I', raw, end + len(zip64) + 12)[0]
        struct.pack_into('<I', raw, end + len(zip64) + 12, old_size + len(zip64))
        self.source.write_bytes(raw)
        self.rebuild()
        self.verify()
        with zipfile.ZipFile(self.rebuilt) as archive:
            self.assertEqual(archive.getinfo('ComicInfo.xml').extra, b'\xfe\xca\x02\x00ok')


if __name__ == '__main__':
    unittest.main()
