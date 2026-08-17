"""Virtual container streams and efficient range reads for NSZ-family files."""
import os
from pathlib import Path


CHUNK_SIZE = 1024 * 1024
NCZ_HEADER_SIZE = 0x4000
XCI_PREFIX_SIZE = 0xF000
HFS0_RESERVED_HEADER_SIZE = 0x8000


def supports_virtual_stream(path):
    return os.path.splitext(path)[1].lower() in {'.nsz', '.ncz', '.xcz'}


def virtual_filename(filename):
    stem, extension = os.path.splitext(filename)
    return stem + {
        '.nsz': '.nsp',
        '.ncz': '.nca',
        '.xcz': '.xci',
    }.get(extension.lower(), extension)


def parse_single_range(value, total_size):
    """Return an inclusive byte range, or ``None`` for a malformed request."""
    if not value:
        return None
    try:
        unit, spec = value.strip().split('=', 1)
        if unit.lower() != 'bytes' or ',' in spec:
            return None
        start_text, end_text = spec.split('-', 1)
        if not start_text:
            # A suffix range (``bytes=-500``).
            length = int(end_text)
            if length <= 0:
                return None
            start = max(0, total_size - length)
            return start, total_size - 1
        start = int(start_text)
        end = int(end_text) if end_text else total_size - 1
        if start < 0 or start >= total_size:
            return None
        return start, min(end, total_size - 1)
    except (TypeError, ValueError):
        return None


def iter_range(source, start, end):
    """Yield an inclusive range while promptly closing a partial source stream."""
    remaining_skip = start
    remaining_send = end - start + 1
    try:
        for chunk in source:
            if remaining_skip:
                skipped = min(remaining_skip, len(chunk))
                remaining_skip -= skipped
                chunk = chunk[skipped:]
            if not chunk:
                continue
            output = chunk[:remaining_send]
            if output:
                yield output
                remaining_send -= len(output)
            if remaining_send == 0:
                return
    finally:
        close = getattr(source, 'close', None)
        if close is not None:
            close()


def _load_nsz():
    # Kept lazy: importing nsz initializes optional crypto dependencies.
    from nsz.Fs import factory
    from nsz import Header, BlockDecompressorReader
    from nsz.nut import aes128, Print
    from zstandard import ZstdDecompressor
    # nsz logs every container entry to stdout when it opens a file.  These
    # virtual responses may involve many range probes, so retain AeroFoil's
    # own concise transfer log and suppress that library chatter.
    Print.silent = True
    return factory, Header, BlockDecompressorReader, aes128, ZstdDecompressor


def _read_int64(source):
    return int.from_bytes(source.read(8), 'little')


def _ncz_layout(source, header_module):
    source.seek(0)
    header = source.read(NCZ_HEADER_SIZE)
    if len(header) != NCZ_HEADER_SIZE or source.read(8) != b'NCZSECTN':
        raise ValueError('No NCZSECTN found in compressed file')
    section_count = _read_int64(source)
    sections = [header_module.Section(source) for _ in range(section_count)]
    if not sections:
        raise ValueError('NCZ contains no sections')
    if sections[0].offset > NCZ_HEADER_SIZE:
        sections.insert(0, header_module.FakeSection(
            NCZ_HEADER_SIZE, sections[0].offset - NCZ_HEADER_SIZE,
        ))
    size = NCZ_HEADER_SIZE + sum(section.size for section in sections)
    return header, sections, size


def decompressed_ncz_size(source):
    _, header_module, _, _, _ = _load_nsz()
    _, _, size = _ncz_layout(source, header_module)
    return size


def iter_decompressed_ncz(source, chunk_size=CHUNK_SIZE):
    """Yield a decrypted NCZ as NCA bytes from an nsz file-like object."""
    _, header_module, block_reader_module, aes128, zstd_decompressor = _load_nsz()
    header, sections, _ = _ncz_layout(source, header_module)
    yield header

    compressed_data_offset = source.tell()
    is_block = source.read(8) == b'NCZBLOCK'
    source.seek(compressed_data_offset)
    if is_block:
        block_header = header_module.Block(source)
        reader = block_reader_module.BlockDecompressorReader(source, block_header)
    else:
        reader = zstd_decompressor().stream_reader(source)

    first_section = True
    for section in sections:
        position = section.offset
        end = section.offset + section.size
        crypto = aes128.AESCTR(section.cryptoKey, section.cryptoCounter) \
            if section.cryptoType in (3, 4) else None
        if first_section:
            first_section = False
            # The bytes between the NCA header and the first NCZ section are
            # already represented in the uncompressed NCA header.
            position += max(0, NCZ_HEADER_SIZE - section.offset)
        while position < end:
            amount = min(chunk_size, end - position)
            data = reader.read(amount)
            if not data:
                raise ValueError('Unexpected end of compressed NCZ data')
            if crypto is not None:
                crypto.seek(position)
                data = crypto.encrypt(data)
            position += len(data)
            yield data


def iter_decompressed_ncz_range(source, start, end, chunk_size=CHUNK_SIZE):
    """Yield an inclusive NCA range, seeking directly for NCZBLOCK sources."""
    _, header_module, block_reader_module, aes128, zstd_decompressor = _load_nsz()
    header, sections, total_size = _ncz_layout(source, header_module)
    if start < 0 or end < start or end >= total_size:
        raise ValueError('NCZ range is outside the decompressed file')

    if start < NCZ_HEADER_SIZE:
        header_end = min(end + 1, NCZ_HEADER_SIZE)
        yield header[start:header_end]
        start = header_end
        if start > end:
            return

    compressed_data_offset = source.tell()
    is_block = source.read(8) == b'NCZBLOCK'
    source.seek(compressed_data_offset)
    logical_start = start - NCZ_HEADER_SIZE
    if is_block:
        block_header = header_module.Block(source)
        reader = block_reader_module.BlockDecompressorReader(source, block_header)
        reader.seek(logical_start)
    else:
        reader = zstd_decompressor().stream_reader(source)
        # Solid streams have no index; discard only the portion before this
        # requested NCA range, rather than the whole virtual NSP prefix.
        remaining = logical_start
        while remaining:
            discarded = reader.read(min(CHUNK_SIZE, remaining))
            if not discarded:
                raise ValueError('Unexpected end of compressed NCZ data')
            remaining -= len(discarded)

    output_position = start
    first_section = True
    for section in sections:
        section_start = section.offset
        if first_section:
            first_section = False
            section_start = max(section_start, NCZ_HEADER_SIZE)
        section_end = section.offset + section.size
        if output_position >= section_end:
            continue
        if output_position < section_start:
            # Sections are contiguous in valid NCZ files. Keep the stream
            # aligned if a malformed file has an unexpected gap.
            gap = section_start - output_position
            skipped = reader.read(gap)
            if len(skipped) != gap:
                raise ValueError('Unexpected end of compressed NCZ data')
            output_position = section_start
        if output_position > end:
            return
        wanted_end = min(end + 1, section_end)
        crypto = aes128.AESCTR(section.cryptoKey, section.cryptoCounter) \
            if section.cryptoType in (3, 4) else None
        while output_position < wanted_end:
            data = reader.read(min(chunk_size, wanted_end - output_position))
            if not data:
                raise ValueError('Unexpected end of compressed NCZ data')
            if crypto is not None:
                crypto.seek(output_position)
                data = crypto.encrypt(data)
            output_position += len(data)
            yield data
        if output_position > end:
            return


def _pfs0_header(files, data_start=None, string_table_size=None):
    return _container_header(
        b'PFS0',
        files,
        0x18,
        0,
        pad_to_0x20=True,
        data_start=data_start,
        string_table_size=string_table_size,
    )


def _hfs0_header(files):
    return _container_header(b'HFS0', files, 0x40, 0x28, pad_to_0x20=False)


def _container_header(
    magic,
    files,
    entry_size,
    extra_size,
    pad_to_0x20,
    data_start=None,
    string_table_size=None,
):
    names = b''.join(name.encode('utf-8') + b'\0' for name, _ in files)
    raw_size = 0x10 + len(files) * entry_size + len(names)
    string_size = max(len(names), int(string_table_size or 0))
    if pad_to_0x20 and string_table_size is None:
        string_size += (-raw_size) % 0x20
    header_size = 0x10 + len(files) * entry_size + string_size
    if data_start is None:
        data_start = header_size
    if data_start < header_size:
        raise ValueError('Container data offset is smaller than its header')
    output = bytearray(magic)
    output += len(files).to_bytes(4, 'little')
    output += string_size.to_bytes(4, 'little')
    output += b'\0' * 4
    offset = data_start - header_size
    name_offset = 0
    for name, size in files:
        output += offset.to_bytes(8, 'little')
        output += size.to_bytes(8, 'little')
        output += name_offset.to_bytes(4, 'little')
        output += b'\0' * (entry_size - 20)
        if extra_size:
            # HFS0's trailing hash field is deliberately zero, matching nsz.
            pass
        offset += size
        name_offset += len(name.encode('utf-8')) + 1
    output += names
    output += b'\0' * (header_size - len(output))
    return bytes(output)


def _iter_file(source):
    source.seek(0)
    while True:
        data = source.read(CHUNK_SIZE)
        if not data:
            return
        yield data


def _entry_info(entry):
    name = str(entry._path)
    if name.lower().endswith('.ncz'):
        return name[:-1] + 'a', decompressed_ncz_size(entry), True
    return name, int(entry.size), False


def virtual_stream(path):
    extension = os.path.splitext(path)[1].lower()
    if extension == '.ncz':
        return _iter_ncz_path(path), _ncz_path_size(path)
    if extension == '.nsz':
        return _iter_nsz(path), _nsz_size(path)
    if extension == '.xcz':
        return _iter_xcz(path), _xcz_size(path)
    raise ValueError('Unsupported compressed stream')


def virtual_range(path, start, end):
    """Return a direct range iterator where the virtual format permits it."""
    if os.path.splitext(path)[1].lower() == '.nsz':
        return _iter_nsz_range(path, start, end)
    return iter_range(virtual_stream(path)[0], start, end)


def _open(path):
    factory, _, _, _, _ = _load_nsz()
    source_path = Path(path)
    container = factory(source_path)
    container.open(str(source_path), 'rb')
    return container


def _ncz_path_size(path):
    source = _open(path)
    try:
        return decompressed_ncz_size(source)
    finally:
        source.close()


def _iter_ncz_path(path):
    source = _open(path)
    try:
        yield from iter_decompressed_ncz(source)
    finally:
        source.close()


def _iter_nsz(path):
    container = _open(path)
    try:
        entries = [(entry, *_entry_info(entry)) for entry in container]
        files = [(name, size) for _, name, size, _ in entries]
        header = _pfs0_header(
            files,
            data_start=container.getFirstFileOffset(),
            string_table_size=container.getStringTableSize(),
        )
        yield header
        yield b'\0' * (container.getFirstFileOffset() - len(header))
        for entry, _, _, compressed in entries:
            if compressed:
                yield from iter_decompressed_ncz(entry)
            else:
                yield from _iter_file(entry)
    finally:
        container.close()


def _iter_nsz_range(path, start, end):
    container = _open(path)
    try:
        entries = [(entry, *_entry_info(entry)) for entry in container]
        files = [(name, size) for _, name, size, _ in entries]
        data_start = container.getFirstFileOffset()
        header = _pfs0_header(
            files,
            data_start=data_start,
            string_table_size=container.getStringTableSize(),
        )
        total_size = data_start + sum(size for _, size in files)
        if start < 0 or end < start or end >= total_size:
            raise ValueError('NSP range is outside the virtual file')

        if start < len(header):
            header_end = min(end + 1, len(header))
            yield header[start:header_end]
            start = header_end
        if start <= end and start < data_start:
            padding_end = min(end + 1, data_start)
            yield b'\0' * (padding_end - start)
            start = padding_end

        entry_start = data_start
        for entry, _, size, compressed in entries:
            entry_end = entry_start + size
            if end < entry_start:
                return
            if start < entry_end and end >= entry_start:
                local_start = max(0, start - entry_start)
                local_end = min(size - 1, end - entry_start)
                if compressed:
                    yield from iter_decompressed_ncz_range(entry, local_start, local_end)
                else:
                    entry.seek(local_start)
                    remaining = local_end - local_start + 1
                    while remaining:
                        data = entry.read(min(CHUNK_SIZE, remaining))
                        if not data:
                            raise ValueError('Unexpected end of container entry')
                        remaining -= len(data)
                        yield data
            entry_start = entry_end
    finally:
        container.close()


def _nsz_size(path):
    container = _open(path)
    try:
        files = [_entry_info(entry)[:2] for entry in container]
        return container.getFirstFileOffset() + sum(size for _, size in files)
    finally:
        container.close()


def _iter_xcz(path):
    container = _open(path)
    try:
        # XCI's fixed preamble is not compressed; keep it byte-for-byte.
        with open(path, 'rb') as raw_file:
            yield raw_file.read(XCI_PREFIX_SIZE)
        partitions = []
        for partition in container.hfs0:
            entries = [(entry, *_entry_info(entry)) for entry in partition]
            files = [(name, size) for _, name, size, _ in entries]
            partitions.append((entries, HFS0_RESERVED_HEADER_SIZE + sum(size for _, size in files)))
        yield _hfs0_header([(str(partition._path), size) for partition, (_, size) in zip(container.hfs0, partitions)])
        yield b'\0' * (HFS0_RESERVED_HEADER_SIZE - len(_hfs0_header([(str(partition._path), size) for partition, (_, size) in zip(container.hfs0, partitions)])))
        for entries, _ in partitions:
            files = [(name, size) for _, name, size, _ in entries]
            header = _hfs0_header(files)
            yield header
            yield b'\0' * (HFS0_RESERVED_HEADER_SIZE - len(header))
            for entry, _, _, compressed in entries:
                yield from iter_decompressed_ncz(entry) if compressed else _iter_file(entry)
    finally:
        container.close()


def _xcz_size(path):
    container = _open(path)
    try:
        total = XCI_PREFIX_SIZE + HFS0_RESERVED_HEADER_SIZE
        for partition in container.hfs0:
            total += HFS0_RESERVED_HEADER_SIZE
            total += sum(_entry_info(entry)[1] for entry in partition)
        return total
    finally:
        container.close()
