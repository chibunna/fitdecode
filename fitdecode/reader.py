# fitdecode
#
# Copyright (c) 2018 Jean-Charles Lefebvre
# All rights reserved.
#
# This code is licensed under the MIT License.
# See the LICENSE.txt file at the root of this project.

import io
import os
import struct

from .exceptions import FitHeaderError, FitCrcError, FitEOFError, FitParseError
from . import records
from . import types
from . import utils
from . import processors
from . import profile

__all__ = ['FitReader']


class RecordHeader:
    __slots__ = (
        'is_definition', 'is_developer_data', 'local_mesg_num', 'time_offset')

    def __init__(self, is_definition, is_developer_data, local_mesg_num,
                 time_offset):
        self.is_definition = is_definition
        self.is_developer_data = is_developer_data
        self.local_mesg_num = local_mesg_num
        self.time_offset = time_offset


class FitReader:
    """
    Parse the content of a FIT stream or storage.

    Transparently supports "chained FIT Files" as per SDK's definition. A
    :class:`fitdecode.FitHeader` object is yielded during iteration to mark the
    beginning of each new "FIT File".

    Usage::

        import fitdecode

        with fitdecode.FitReader(src_file) as fit:
            for record in fit:
                # The yielded *record* object has one of the following types:
                # * fitdecode.FitHeader
                # * fitdecode.FitDefinitionMessage
                # * fitdecode.FitDataMessage
                # * fitdecode.FitCrc
                #
                # A fitdecode.FitDataMessage object contains decoded values that
                # are directly usable in your script logic.
                pass

    .. todo::
        * Make a ``read_all()`` method to parse then interpret the file and
          consolidate some data. Typically the post-activity ``hr`` records
          coming from a Garmin HRM strap that are appended to the FIT file and
          that need to be merge in activity's ``record`` messages.
        * Since we support thread-safety, ``read_all()`` should accept
          "progress" argument, a callable to be called each time the percentage
          of the progression changes.
    """

    def __init__(self, fileish, *,
                 processor=None, check_crc=True, keep_raw_chunks=False):
        # modifiable options (public)
        self.check_crc = check_crc

        # immutable options (private)
        self._processor = processor or processors.get_default_processor()
        self._keep_raw = keep_raw_chunks

        # state (private)
        self._fd = None        # the file object to read from
        self._fd_view = None   # memoryview of self._fd, if possible
        self._read_offset = 0  # read cursor position in the file
        self._read_size = 0    # count bytes read from this file so far in total

        # per-chunk state (private)
        self._chunk_offset = 0  # the offset of the chunk that is currently being read (relative to `read_offset`)
        self._chunk_size = 0    # the size of the chunk that is currently being read

        # per-FIT-file state (private)
        self._crc = utils.CRC_START  # current CRC value, updated upon every read, reset on each new "FIT file"
        self._header = None          # `FitHeader` of the **current** "FIT file"
        self._body_bytes_left = 0    # the number of bytes that are still to read before reaching the CRC footer of the current "FIT file"
        self._local_mesg_defs = {}   # registry of every `FitDefinitionMessage` in this file so far
        self._dev_types = {}         # registry of developer types
        self._accumulators = {}
        self._compressed_ts_accumulator = 0

        if hasattr(fileish, '__fspath__'):
            fileish = os.fspath(fileish)

        if hasattr(fileish, 'read'):
            self._fd = fileish

            try:
                self._fd_view = self._fd.getbuffer()

                if (not isinstance(self._fd_view, memoryview) or
                        not len(self._fd_view) or
                        type(self._fd_view[0]) is not int):
                    self._fd_view = None
            except AttributeError:
                pass
        elif hasattr(fileish, '__fspath__') or isinstance(fileish, str):
            self._fd = open(os.fspath(fileish), mode='rb')
        else:
            self._fd = io.BytesIO(fileish)
            self._fd_view = self._fd.getbuffer()

        try:
            self._read_offset = self._fd.tell()
            self._chunk_offset = self._read_offset
        except (AttributeError, OSError):
            pass

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return self.close()

    def __iter__(self):
        yield from self._read_next()

    @property
    def local_mesg_defs(self):
        """
        Read-only access to the `dict` of local message types of the current
        "FIT file".

        It is cleared by `close()` (or ``__exit__()``), and also each time a FIT
        file header is reached (i.e. at the beginning of a file, or after a
        :class:`fitdecode.FitCrc`).
        """
        return self._local_mesg_defs

    @property
    def dev_types(self):
        """
        Read-only access to the `dict` of developer types of the current
        "FIT file".

        It is cleared by `close()` (or ``__exit__()``), and also each time a FIT
        file header is reached (i.e. at the beginning of a file, or after a
        :class:`fitdecode.FitCrc`).
        """
        return self._dev_types

    def close(self):
        """
        Close the file handle (constructor's *fileish*) and clear the internal
        state.
        """
        if self._fd and hasattr(self._fd, 'close'):
            self._fd.close()

        self._fd = None
        self._fd_view = None
        self._read_offset = 0
        self._read_size = 0
        self._chunk_offset = 0
        self._chunk_size = 0
        self._crc = utils.CRC_START
        self._header = None
        self._body_bytes_left = 0
        self._local_mesg_defs = {}
        self._dev_types = {}
        self._accumulators = {}
        self._compressed_ts_accumulator = 0

    # ONLY PRIVATE METHODS BELOW

    def _read_next(self):

        def _update_state():
            self._chunk_offset += self._chunk_size
            self._chunk_size = 0

        while True:
            assert self._chunk_size == 0

            if not self._header:
                assert self._body_bytes_left == 0

                self._read_header()
                if not self._header:
                    break

                yield self._header
                _update_state()

            elif self._body_bytes_left > 0:
                assert self._header

                record = self._read_record()
                if not record:
                    break

                assert self._chunk_size <= self._body_bytes_left
                self._body_bytes_left -= self._chunk_size

                yield record
                _update_state()

            else:
                assert self._header
                assert self._body_bytes_left == 0

                try:
                    crc_obj = self._read_crc()
                except FitEOFError:
                    # if not self.check_crc:
                    #     # There is no CRC footer in this file (or it is
                    #     # incomplete) but caller does not mind about CRC so
                    #     # we will just ignore this
                    #     break
                    raise

                yield crc_obj
                _update_state()

                # we reached the end of this FIT "file"
                self._header = None

    def _read_header(self):
        # reset state
        self._crc = utils.CRC_START
        self._header = None
        self._body_bytes_left = 0
        self._local_mesg_defs = {}
        self._dev_types = {}
        self._accumulators = {}
        self._compressed_ts_accumulator = 0

        try:
            chunk, header_size, proto_ver, profile_ver, body_size, \
                header_magic = self._read_struct('<2BHI4s')
        except FitEOFError as exc:
            if not exc.got:
                return  # regular EOF
            raise FitHeaderError('file too small (' + str(exc) + ')')

        # check header size
        if header_size < len(chunk) or header_magic != b'.FIT':
            raise FitHeaderError('not a FIT file')

        # read the extended part of the header (i.e. byte 12-...)
        extra_header_size = header_size - len(chunk)
        read_crc = None
        if extra_header_size:
            # at least 2 bytes expected for the CRC
            if extra_header_size < 2:
                raise FitHeaderError('unsupported FIT header')

            extra_chunk = self._read_bytes(extra_header_size)
            if len(extra_chunk) != extra_header_size:
                raise FitHeaderError('truncated FIT header')

            if self.check_crc:
                (read_crc, ) = struct.unpack('<H', extra_chunk)
                if not read_crc:  # can be null according to SDK
                    read_crc = None
                else:
                    computed_crc = utils.compute_crc(chunk)
                    if computed_crc != read_crc:
                        raise FitCrcError('invalid FIT header CRC')

            chunk += extra_chunk

        proto_ver = (proto_ver >> 4, proto_ver & ((1 << 4) - 1))
        profile_ver = (int(profile_ver / 100), int(profile_ver % 100))

        # update state
        self._header = records.FitHeader(
            header_size=header_size,
            proto_ver=proto_ver,
            profile_ver=profile_ver,
            body_size=body_size,
            crc=read_crc,
            chunk=self._keep_chunk_or_view(chunk))
        self._body_bytes_left = body_size

    def _read_crc(self):
        computed_crc = self._crc
        chunk, read_crc = self._read_struct('<H')

        if self.check_crc and computed_crc != read_crc:
            # print(f'{computed_crc:#x} {read_crc:#x}')
            raise FitCrcError()

        return records.FitCrc(read_crc, self._keep_chunk_or_view(chunk))

    def _read_record(self):
        # read header
        chunk = self._read_bytes(1)
        if chunk[0] & 0x80:  # bit 7: compressed timestamp?
            record_header = RecordHeader(
                is_definition=False,
                is_developer_data=False,
                local_mesg_num=(chunk[0] >> 5) & 0x3,  # bits 5-6
                time_offset=chunk[0] & 0x1f)           # bits 0-4
        else:
            record_header = RecordHeader(
                is_definition=bool(chunk[0] & 0x40),      # bit 6
                is_developer_data=bool(chunk[0] & 0x20),  # bit 5
                local_mesg_num=chunk[0] & 0xf,            # bits 0-3
                time_offset=None)

        # read record payload
        if record_header.is_definition:
            message = self._read_definition_message(chunk, record_header)
        else:
            message = self._read_data_message(chunk, record_header)

            if message.mesg_type is not None:
                if message.mesg_type.name == 'developer_data_id':
                    self._add_dev_data_id(message)
                elif message.mesg_type.name == 'field_description':
                    self._add_dev_field_description(message)

        return message

    def _read_definition_message(self, header_chunk, record_header):
        record_chunks = [header_chunk]

        # read the "fixed content" part
        extra_chunk = self._read_bytes(5)
        record_chunks.append(extra_chunk)
        endian = '<' if not extra_chunk[1] else '>'
        global_mesg_num, num_fields = struct.unpack(endian + '2xHB',
                                                    extra_chunk)

        # get global message's declaration from our profile if any
        mesg_type = profile.MESSAGE_TYPES.get(global_mesg_num)

        field_unpacker = struct.Struct(endian + '3B')
        field_defs = []
        dev_field_defs = []

        # read field definitions
        for idx in range(num_fields):
            extra_chunk = self._read_bytes(field_unpacker.size)
            record_chunks.append(extra_chunk)

            field_def_num, field_size, base_type_num = \
                field_unpacker.unpack(extra_chunk)

            field = mesg_type.fields.get(field_def_num) if mesg_type else None
            base_type = types.BASE_TYPES.get(
                base_type_num, types.BASE_TYPE_BYTE)

            if (field_size % base_type.size) != 0:
                # should we fall back to byte encoding instead?
                raise FitParseError(
                    self._chunk_offset,
                    f'invalid field size {field_size} for type ' +
                    f'{base_type.name} (expected a multiple of ' +
                    f'{base_type.size})')

            # if the field has components that are accumulators,
            # start recording their accumulation at 0
            if field and field.components:
                for component in field.components:
                    if component.accumulate:
                        accumulators = \
                            self._accumulators.setdefault(global_mesg_num, {})
                        accumulators[component.def_num] = 0

            field_defs.append(types.FieldDefinition(
                field, field_def_num, base_type, field_size))

        # read developer field definitions if any
        if record_header.is_developer_data:
            # read the number of developer fields definitions that follow
            extra_chunk = self._read_bytes(1)
            record_chunks.append(extra_chunk)
            num_dev_fields = extra_chunk[0]

            # read field definitions
            for idx in range(num_dev_fields):
                extra_chunk = self._read_bytes(field_unpacker.size)
                record_chunks.append(extra_chunk)

                field_def_num, field_size, dev_data_index = \
                    field_unpacker.unpack(extra_chunk)

                field = self._get_dev_type(dev_data_index, field_def_num)

                dev_field_defs.append(types.DevFieldDefinition(
                    field, dev_data_index, field_def_num, field_size))

        def_mesg = records.FitDefinitionMessage(
            record_header.is_developer_data,
            record_header.local_mesg_num,
            record_header.time_offset,
            mesg_type,
            global_mesg_num,
            endian,
            field_defs,
            dev_field_defs,
            self._keep_chunk_or_view(record_chunks))

        # According to FIT protocol's specification (section 4.8.3), it is ok to
        # redefine message types
        self._local_mesg_defs[record_header.local_mesg_num] = def_mesg

        return def_mesg

    def _read_data_message(self, header_chunk, record_header):
        record_chunks = [header_chunk]

        try:
            def_mesg = self._local_mesg_defs[record_header.local_mesg_num]
        except KeyError:
            raise FitParseError(
                self._chunk_offset,
                f'local message {record_header.local_mesg_num} not defined')

        extra_chunks, raw_values = self._read_data_message_raw_values(def_mesg)
        record_chunks.extend(extra_chunks)
        message_fields = []

        for field_def, raw_value in zip(
                def_mesg.field_defs + def_mesg.dev_field_defs, raw_values):

            field, parent_field = field_def.field, None
            if field:
                field, parent_field = self._resolve_subfield(
                    field, def_mesg, raw_values)

                # resolve component fields
                if field.components:
                    for component in field.components:
                        # render its raw value
                        cmp_raw_value = component.render(raw_value)

                        # apply accumulated value
                        if component.accumulate and cmp_raw_value is not None:
                            accumulator = self._accumulators[
                                def_mesg.global_mesg_num]
                            cmp_raw_value = self._apply_compressed_accumulation(
                                cmp_raw_value,
                                accumulator[component.def_num],
                                component.bits)
                            accumulator[component.def_num] = cmp_raw_value

                        # apply scale and offset from component, not from the
                        # dynamic field as they may differ
                        cmp_raw_value = self._apply_scale_offset(
                            component, cmp_raw_value)

                        # extract the component's dynamic field from def_mesg
                        cmp_field = def_mesg.mesg_type.fields[component.def_num]

                        # resolve a possible subfield
                        cmp_field, cmp_parent_field = self._resolve_subfield(
                            cmp_field, def_mesg, raw_values)
                        cmp_value = cmp_field.render(cmp_raw_value)

                        message_fields.append(types.FieldData(
                            None,              # field_def
                            cmp_field,         # field
                            cmp_parent_field,  # parent_field
                            cmp_value,         # value
                            cmp_raw_value))    # raw_value

                decoded_value = self._apply_scale_offset(
                    field, field.render(raw_value))
            else:
                decoded_value = raw_value

            # update compressed timestamp field
            if (field_def.def_num == profile.FIELD_TYPE_TIMESTAMP.def_num and
                    raw_value is not None):
                self._compressed_ts_accumulator = raw_value

            message_fields.append(types.FieldData(
                field_def,      # field_def
                field,          # field
                parent_field,   # parent_field
                decoded_value,  # value
                raw_value))     # raw_value

        # apply timestamp field if we got a header
        if record_header.time_offset is not None:
            ts_value = self._apply_compressed_accumulation(
                record_header.time_offset, self._compressed_ts_accumulator, 5)

            self._compressed_ts_accumulator = ts_value

            message_fields.append(types.FieldData(
                None,                                           # field_def
                profile.FIELD_TYPE_TIMESTAMP,                   # field
                None,                                           # parent_field
                profile.FIELD_TYPE_TIMESTAMP.render(ts_value),  # value
                ts_value))                                      # raw_value

        # apply data processors
        for field_data in message_fields:
            self._processor.run_type_processor(field_data)
            self._processor.run_field_processor(field_data)
            self._processor.run_unit_processor(field_data)

        data_message = records.FitDataMessage(
            record_header.is_developer_data,
            record_header.local_mesg_num,
            record_header.time_offset,
            def_mesg,
            message_fields,
            self._keep_chunk_or_view(record_chunks))

        self._processor.run_message_processor(data_message)

        return data_message

    def _read_data_message_raw_values(self, def_mesg):
        raw_values = []
        record_chunks = []

        for field_def in def_mesg.field_defs + def_mesg.dev_field_defs:
            base_type = field_def.base_type

            # struct format to read "[N]" base types
            unpacker = struct.Struct(
                def_mesg.endian +
                str(int(field_def.size / base_type.size)) +
                base_type.fmt)

            # read the chunk
            chunk = self._read_bytes(unpacker.size)
            record_chunks.append(chunk)

            # extract the raw value
            raw_value = unpacker.unpack(chunk)

            if base_type.name == 'byte':
                raw_value = base_type.parse(raw_value)
            elif len(raw_value) > 1:
                # If the field returns with a tuple of values it's definitely an
                # oddball, but we'll parse it on a per-value basis. If it's a
                # byte type, treat the tuple as a single value
                raw_value = tuple(base_type.parse(v) for v in raw_value)
            else:
                # Otherwise, just scrub the singular value
                raw_value = base_type.parse(raw_value[0])

            raw_values.append(raw_value)

        return record_chunks, raw_values

    def _read_struct(self, fmt, *, endian=None):
        assert fmt
        if endian:
            fmt = endian + fmt

        unpacker = struct.Struct(fmt)
        if unpacker.size <= 0:
            raise ValueError(f'invalid struct format "{fmt}"')

        chunk = self._read_bytes(unpacker.size)

        return (chunk, ) + unpacker.unpack(chunk)

    def _read_bytes(self, size):
        if size <= 0:
            raise ValueError('size')

        chunk = utils.blocking_read(self._fd, size)
        chunk_size = 0 if not chunk else len(chunk)
        if chunk_size != size:
            raise FitEOFError(size, chunk_size, self._read_offset)

        if chunk:
            self._crc = utils.compute_crc(chunk, crc=self._crc)
            self._chunk_size += chunk_size
            self._read_offset += chunk_size
            self._read_size += chunk_size

        return chunk

    def _keep_chunk_or_view(self, chunk):
        if not self._keep_raw:
            return None

        assert chunk
        assert type(chunk) in (list, tuple, bytes)

        if self._fd_view:
            if __debug__:
                if type(chunk) in (list, tuple):
                    assert sum(map(lambda x: len(x), chunk)) == self._chunk_size
                else:
                    assert len(chunk) == self._chunk_size

            end = self._chunk_offset + self._chunk_size
            return records.FitChunk(
                self._chunk_offset,
                self._fd_view[self._chunk_offset:end])

        elif type(chunk) in (list, tuple):
            # *chunk* is a list of chunks
            assert sum(map(lambda x: len(x), chunk)) == self._chunk_size
            return records.FitChunk(self._chunk_offset, b''.join(chunk))

        else:
            assert len(chunk) == self._chunk_size
            return records.FitChunk(self._chunk_offset, chunk)

    def _add_dev_data_id(self, message):
        dev_data_index = message.get_field('developer_data_index').raw_value

        try:
            application_id = message.get_field('application_id').raw_value
        except AttributeError:
            application_id = None

        # declare/overwrite type
        self._dev_types[dev_data_index] = {
            'dev_data_index': dev_data_index,
            'application_id': application_id,
            'fields': {}}

    def _add_dev_field_description(self, message):
        dev_data_index = message.get_field('developer_data_index').raw_value
        if dev_data_index not in self._dev_types:
            raise FitParseError(
                self._chunk_offset,
                'dev_data_index {dev_data_index} not defined')

        field_def_num = message.get_field('field_definition_number').raw_value
        base_type_id = message.get_field('fit_base_type_id').raw_value
        field_name = message.get_field('field_name').raw_value
        units = message.get_field('units').raw_value

        native_field_num = message.get_field('native_field_num')
        if native_field_num is not None:
            native_field_num = native_field_num.raw_value

        fields = self._dev_types[int(dev_data_index)]['fields']

        # declare/overwrite type
        fields[field_def_num] = types.DevField(
            dev_data_index, field_name, field_def_num,
            types.BASE_TYPES[base_type_id], units, native_field_num)

    def _get_dev_type(self, dev_data_index, field_def_num):
        if dev_data_index not in self._dev_types:
            raise FitParseError(
                self._chunk_offset,
                f'dev_data_index {dev_data_index} not defined ' +
                f'(looking up for field {field_def_num})')

        try:
            return self._dev_types[dev_data_index]['fields'][field_def_num]
        except AttributeError:
            raise FitParseError(
                self._chunk_offset,
                f'no such field {field_def_num} for dev_data_index ' +
                f'{dev_data_index}')

    @staticmethod
    def _resolve_subfield(field, def_mesg, raw_values):
        # resolve into (field, parent) ie (subfield, field) or (field, none)
        if field.subfields:
            for sub_field in field.subfields:
                # go through reference fields for this sub field
                for ref_field in sub_field.ref_fields:
                    # go through field defs AND their raw values
                    for field_def, raw_value in zip(
                            def_mesg.field_defs, raw_values):
                        # if there's a definition number AND raw value match on
                        # the reference field, then we return this subfield
                        if (field_def.def_num == ref_field.def_num and
                                ref_field.raw_value == raw_value):
                            return sub_field, field

        return field, None

    @staticmethod
    def _apply_compressed_accumulation(raw_value, accumulation, num_bits):
        max_value = 1 << num_bits
        max_mask = max_value - 1
        base_value = raw_value + (accumulation & ~max_mask)

        if raw_value < (accumulation & max_mask):
            base_value += max_value

        return base_value

    @classmethod
    def _apply_scale_offset(cls, field, raw_value):
        # apply numeric transformations (scale + offset)
        if isinstance(raw_value, tuple):
            # contains multiple values, apply transformations to all of them
            return tuple(cls._apply_scale_offset(field, x) for x in raw_value)
        elif isinstance(raw_value, (int, float)):
            if field.scale:
                raw_value = float(raw_value) / field.scale
            if field.offset:
                raw_value = raw_value - field.offset

        return raw_value