import io
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

import zstandard

from app import compressed_stream


class _Section:
    def __init__(self, source):
        self.offset, self.size, self.cryptoType, _ = struct.unpack('<QQQQ', source.read(32))
        self.cryptoKey = source.read(16)
        self.cryptoCounter = source.read(16)


class _Header:
    Section = _Section

    class FakeSection:
        def __init__(self, offset, size):
            self.offset = offset
            self.size = size
            self.cryptoType = 0


def _nsz_components():
    return None, _Header, None, None, zstandard.ZstdDecompressor


def _ncz(payload):
    section = struct.pack('<QQQQ', 0x4000, len(payload), 0, 0) + (b'\0' * 32)
    return (
        b'H' * 0x4000
        + b'NCZSECTN'
        + struct.pack('<Q', 1)
        + section
        + zstandard.ZstdCompressor().compress(payload)
    )


class _Entry(io.BytesIO):
    def __init__(self, name, data):
        super().__init__(data)
        self._path = name
        self.size = len(data)


class _Container:
    def __init__(self, entries):
        self.entries = entries
        self.closed = False

    def __iter__(self):
        return iter(self.entries)

    def close(self):
        self.closed = True

    def getFirstFileOffset(self):
        return 0xF000

    def getStringTableSize(self):
        return 0x40


class CompressedStreamTests(unittest.TestCase):
    def test_streams_solid_ncz_as_raw_nca(self):
        payload = b'compressed-section' * 100
        source = io.BytesIO(_ncz(payload))
        with patch('app.compressed_stream._load_nsz', side_effect=_nsz_components):
            self.assertEqual(compressed_stream.decompressed_ncz_size(source), 0x4000 + len(payload))
            self.assertEqual(b''.join(compressed_stream.iter_decompressed_ncz(source)), b'H' * 0x4000 + payload)

    def test_streams_only_requested_solid_ncz_range(self):
        payload = b'compressed-section' * 100
        source = io.BytesIO(_ncz(payload))
        with patch('app.compressed_stream._load_nsz', side_effect=_nsz_components):
            self.assertEqual(
                b''.join(compressed_stream.iter_decompressed_ncz_range(source, 0x4005, 0x4014)),
                payload[5:21],
            )

    def test_virtual_nsz_rewrites_ncz_entry_and_pfs0_size(self):
        ncz_payload = b'virtual-nca-data'
        container = _Container([
            _Entry('ticket.tik', b'ticket'),
            _Entry('content.ncz', _ncz(ncz_payload)),
        ])
        with patch('app.compressed_stream._open', return_value=container), \
                patch('app.compressed_stream._load_nsz', side_effect=_nsz_components):
            data = b''.join(compressed_stream._iter_nsz('Example.nsz'))

        self.assertTrue(container.closed)
        self.assertEqual(data[:4], b'PFS0')
        self.assertEqual(int.from_bytes(data[4:8], 'little'), 2)
        self.assertIn(b'content.nca\0', data[:0x100])
        header_size = 0x10 + 2 * 0x18 + int.from_bytes(data[8:12], 'little')
        self.assertEqual(data[header_size:0xF000], b'\0' * (0xF000 - header_size))
        self.assertEqual(data[0xF000:], b'ticket' + b'H' * 0x4000 + ncz_payload)
        self.assertEqual(int.from_bytes(data[0x10:0x18], 'little'), 0xF000 - header_size)

    def test_virtual_nsz_range_seeks_directly_to_uncompressed_entry(self):
        container = _Container([
            _Entry('first.bin', b'first'),
            _Entry('second.bin', b'second'),
        ])
        with patch('app.compressed_stream._open', return_value=container):
            data = b''.join(compressed_stream._iter_nsz_range('Example.nsz', 0xF005, 0xF00A))

        self.assertEqual(data, b'second')

    def test_virtual_names_match_uncompressed_formats(self):
        self.assertEqual(compressed_stream.virtual_filename('Example.nsz'), 'Example.nsp')
        self.assertEqual(compressed_stream.virtual_filename('Example.ncz'), 'Example.nca')
        self.assertEqual(compressed_stream.virtual_filename('Example.xcz'), 'Example.xci')

    def test_range_stream_skips_prefix_and_closes_source(self):
        source = iter([b'abc', b'def', b'ghi'])
        self.assertEqual(b''.join(compressed_stream.iter_range(source, 2, 6)), b'cdefg')
        self.assertEqual(compressed_stream.parse_single_range('bytes=2-6', 9), (2, 6))
        self.assertEqual(compressed_stream.parse_single_range('bytes=-3', 9), (6, 8))
        self.assertIsNone(compressed_stream.parse_single_range('bytes=20-', 9))

    def test_open_passes_path_object_to_nsz_factory(self):
        fake_container = unittest.mock.MagicMock()
        with patch('app.compressed_stream._load_nsz', return_value=(
            unittest.mock.MagicMock(return_value=fake_container), None, None, None, None,
        )) as loader:
            container = compressed_stream._open(r'X:\fixture-root\Example.nsz')

        factory = loader.return_value[0]
        self.assertIs(container, fake_container)
        self.assertEqual(str(factory.call_args.args[0]), r'X:\fixture-root\Example.nsz')
        self.assertIsInstance(factory.call_args.args[0], Path)

    def test_cached_shop_files_advertise_virtual_names_to_cyberfoil(self):
        from app.app import _get_cached_shop_files

        with patch('app.app._build_enriched_shop_files', return_value=[
            {'url': '/api/get_game/1#Example.nsz', 'size': 1},
            {'url': '/api/get_game/2#Example.nsp', 'size': 2},
        ]), patch('app.app.get_library_cache_state_token', return_value='stream-test'), \
                patch('app.app.shop_root_cache', {'state_token': None, 'files_enriched': None, 'encrypted': {}}):
            files = _get_cached_shop_files(virtualize_compressed=True)

        self.assertEqual(files[0]['url'], '/api/get_game/1#Example.nsp')
        self.assertEqual(files[1]['url'], '/api/get_game/2#Example.nsp')
