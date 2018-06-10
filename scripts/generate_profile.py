#!/usr/bin/env python
#
# NOTE: this script comes from python-fitparse v1.0.1, and has been slightly
# modified and corrected for fitdecode.
#
# Horrible, dirty, ugly, awful, and terrible script to export the Profile.xls
# that comes with the FIT SDK to the Python data structures in profile.py. You
# shouldn't have to use this unless you're developing python-fitparse.
#
# You can download the SDK at http://www.thisisant.com/
#
# WARNING: This is only known to work with FIT SDK versions up to 5.10
#

from collections import namedtuple
import datetime
import os
import re
import sys
import zipfile

import xlrd  # Dev requirement for parsing Excel spreadsheet


XLS_HEADER_MAGIC = b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'


def header(header, indent=0):
    return '%s# %s' % (' ' * indent, (' %s ' % header).center(78 - indent, '*'))


PROFILE_HEADER_FIRST_PART = "%s\n%s" % (
    header('BEGIN AUTOMATICALLY GENERATED FIT PROFILE'),
    header('DO NOT EDIT THIS FILE'),
)

IMPORT_HEADER = '''from .types import (
    ComponentField,
    Field,
    FieldType,
    MessageType,
    ReferenceField,
    SubField,
    BASE_TYPES)'''

SPECIAL_FIELD_DECLARTIONS = "FIELD_TYPE_TIMESTAMP = Field(name='timestamp', type=FIELD_TYPES['date_time'], def_num=253, units='s')"

IGNORE_TYPE_VALUES = (
    # of the form 'type_name:value_name'
    'mesg_num:mfg_range_min',
    'mesg_num:mfg_range_max',
    'date_time:min')  # TODO: How to account for this? (see Profile.xls)

BASE_TYPES = {
    'enum': '0x00',
    'sint8': '0x01',
    'uint8': '0x02',
    'sint16': '0x83',
    'uint16': '0x84',
    'sint32': '0x85',
    'uint32': '0x86',
    'string': '0x07',
    'float32': '0x88',
    'float64': '0x89',
    'uint8z': '0x0a',
    'uint16z': '0x8b',
    'uint32z': '0x8c',
    'byte': '0x0d',
    'sint64': '0x8e',
    'uint64': '0x8f',
    'uint64z': '0x90'}


def render_type(name):
    if name in BASE_TYPES:
        return "BASE_TYPES[%s],  # %s" % (BASE_TYPES[name], name)
    else:
        return "FIELD_TYPES['%s']," % name


def indent(s, amount=1):
    return ('\n%s' % (' ' * (amount * 4))).join(str(s).splitlines())


class TypeList(namedtuple('TypeList', ('types'))):
    def get(self, name, raise_exception=True):
        for type in self.types:
            if type.name == name:
                return type
        if raise_exception:
            raise AssertionError("Couldn't find type by name: %s" % name)

    def num_values(self):
        return sum(len(type.enum) for type in self.types)

    def get_mesg_num(self, name):
        for mesg in self.get('mesg_num').enum:
            if mesg.name == name:
                return mesg.value
        raise AssertionError("Couldn't find message by name: %s" % name)

    def __str__(self):
        s = 'FIELD_TYPES = {\n'
        for type in sorted(self.types, key=lambda x: x.name):
            s += "    '%s': %s,\n" % (type.name, indent(type))
        s += '}'
        return s


class TypeInfo(namedtuple('TypeInfo', ('name', 'base_type', 'enum', 'comment'))):
    def get(self, value_name):
        for value in self.enum:
            if value.name == value_name:
                return value
        raise AssertionError("Invalid value name %s in type %s" % (value_name, self.name))

    def __str__(self):
        s = 'FieldType(%s\n' % render_comment(self.comment)
        s += "    name='%s',\n" % (self.name)
        s += "    base_type=BASE_TYPES[%s],  # %s\n" % (
            BASE_TYPES[self.base_type], self.base_type,
        )
        if self.enum:
            s += "    enum={\n"
            for value in sorted(self.enum, key=lambda x: x.value if isinstance(x.value, int) else int(x.value, 16)):
                s += "        %s\n" % (value,)
            s += "    },\n"
        s += ")"
        return s


class TypeValueInfo(namedtuple('TypeValueInfo', ('name', 'value', 'comment'))):
    def __str__(self):
        return "%s: '%s',%s" % (self.value, self.name, render_comment(self.comment))


class MessageList(namedtuple('MessageList', ('messages'))):
    def __str__(self):
        s = 'MESSAGE_TYPES = {\n'
        last_group_name = None
        for message in sorted(
            self.messages,
            key=lambda mi: (
                0 if mi.group_name.lower().startswith('common') else 1,
                mi.group_name.lower(), mi.num,
            ),
        ):
            # Group name comment
            if message.group_name != last_group_name:
                if last_group_name is not None:
                    s += '\n\n'
                s += "%s\n" % header(message.group_name, 4)
                last_group_name = message.group_name
            s += "    %s: %s,\n" % (message.num, indent(message))
        s += '}'
        return s


class MessageInfo(namedtuple('MessageInfo', ('name', 'num', 'group_name', 'fields', 'comment'))):
    def get(self, field_name):
        for field in self.fields:
            if field.name == field_name:
                return field
        raise AssertionError("Invalid field name %s in message %s" % (field_name, self.name))

    def __str__(self):
        s = "MessageType(%s\n" % render_comment(self.comment)
        s += "    name='%s',\n" % self.name
        s += "    mesg_num=%d,\n" % self.num
        s += "    fields={\n"
        for field in sorted(self.fields, key=lambda fi: fi.num):
            # Don't include trailing comma for fields
            s += "        %d: %s\n" % (field.num, indent(field, 2))
        s += "    },\n"
        s += ")"
        return s


class FieldInfo(namedtuple('FieldInfo', ('name', 'type', 'num', 'scale', 'offset', 'units', 'components', 'subfields', 'comment'))):
    def __str__(self):
        if self.num == 253:
            # Add trailing comma here because of comment
            assert not self.components and not self.subfields
            return 'FIELD_TYPE_TIMESTAMP,%s' % render_comment(self.comment)
        s = "Field(%s\n" % render_comment(self.comment)
        s += "    name='%s',\n" % self.name
        s += "    type=%s\n" % render_type(self.type)
        s += "    def_num=%d,\n" % self.num
        if self.scale:
            s += "    scale=%s,\n" % self.scale
        if self.offset:
            s += "    offset=%s,\n" % self.offset
        if self.units:
            s += "    units=%s,\n" % repr(self.units)
        if self.components:
            s += '    components=(\n'
            # Leave components sorted as is (order matters because of bit layout)
            for component in self.components:
                s += "        %s,\n" % indent(component, 2)
            s += "    ),\n"
        if self.subfields:
            s += "    subfields=(\n"
            for subfield in sorted(self.subfields, key=lambda si: si.name):
                s += "        %s,\n" % indent(subfield, 2)
            s += "    ),\n"
        s += "),"
        return s


class ComponentFieldInfo(namedtuple('ComponentFieldInfo', ('name', 'num', 'scale', 'offset', 'units', 'bits', 'bit_offset', 'accumulate'))):
    def __str__(self):
        s = "ComponentField(\n"
        s += "    name='%s',\n" % self.name
        s += "    def_num=%d,\n" % (self.num if self.num is not None else 0)
        if self.scale:
            s += "    scale=%s,\n" % self.scale
        if self.offset:
            s += "    offset=%s,\n" % self.offset
        if self.units:
            s += "    units=%s,\n" % repr(self.units)
        s += "    accumulate=%s,\n" % self.accumulate
        s += "    bits=%s,\n" % self.bits
        s += "    bit_offset=%s,\n" % self.bit_offset
        s += ")"
        return s


class SubFieldInfo(namedtuple('SubFieldInfo', ('name', 'num', 'type', 'scale', 'offset', 'units', 'ref_fields', 'components', 'comment'))):
    def __str__(self):
        s = "SubField(%s\n" % render_comment(self.comment)
        s += "    name='%s',\n" % self.name
        s += "    def_num=%s,\n" % self.num
        s += "    type=%s\n" % render_type(self.type)
        if self.scale:
            s += "    scale=%s,\n" % self.scale
        if self.offset:
            s += "    offset=%s,\n" % self.offset
        if self.units:
            s += "    units=%s,\n" % repr(self.units)
        s += "    ref_fields=(\n"
        for ref_field in self.ref_fields:  # sorted(self.ref_fields, key=lambda rf: (rf.name, rf.value)):
            s += "        %s,\n" % indent(ref_field, 2)
        s += "    ),\n"
        if self.components:
            s += '    components=(\n'
            # Leave components sorted as is (order matters because of bit layout)
            for component in self.components:
                s += "        %s,\n" % indent(component, 2)
            s += "    ),\n"
        s += ")"
        return s


class ReferenceFieldInfo(namedtuple('ReferenceFieldInfo', ('name', 'value', 'num', 'raw_value'))):
    def __str__(self):
        s = 'ReferenceField(\n'
        s += "    name='%s',\n" % self.name
        s += '    def_num=%d,\n' % self.num
        s += "    value='%s',\n" % self.value
        s += '    raw_value=%d,\n' % self.raw_value
        s += ')'
        return s


def render_comment(comment):
    if comment:
        return '  # %s' % comment
    return ''


def fix_scale(data):
    if data == 1:
        return None
    return data


def fix_units(data):
    if isinstance(data, str):
        data = data.replace(' / ', '/')
        data = data.replace(' * ', '*')
        data = data.replace('(steps)', 'or steps')
        data = data.strip()
    return data


def parse_csv_fields(data, num_expected):
    if data is None or data == '':
        return [None] * num_expected
    elif isinstance(data, str):
        ret = [(int(x.strip()) if x.strip().isdigit() else x.strip()) for x in data.strip().split(',')]
    else:
        ret = [data]

    # Only len 1 but more were expected, extend it for all values
    if len(ret) == 1 and num_expected:
        return ret * num_expected
    return ret


def parse_spreadsheet(xls_file, *sheet_names):
    if isinstance(xls_file, str):
        workbook = xlrd.open_workbook(xls_file)
    else:
        workbook = xlrd.open_workbook(file_contents=xls_file.read())

    for sheet_name in sheet_names:
        sheet = workbook.sheet_by_name(sheet_name)

        parsed_values = []

        # Strip sheet header
        for n in range(1, sheet.nrows):
            values = []

            row_values = sheet.row_values(n)
            if sheet_name.lower() == 'messages':
                # Only care about the first 14 rows for Messages
                row_values = row_values[:14]

            for value in row_values:
                if isinstance(value, str):
                    # Use strings for now. Unicode is wonky
                    value = value.strip().encode('ascii', 'ignore')
                    if value == '':
                        value = None
                if isinstance(value, float):
                    if value.is_integer():
                        value = int(value)

                values.append(value)

            if all(v is None for v in values):
                continue

            parsed_values.append(values)

        yield parsed_values


def parse_types(types_rows):
    type_list = TypeList([])

    for row in types_rows:
        if row[0]:
            # First column means new type
            type = TypeInfo(
                name=row[0].decode(), base_type=row[1].decode(), enum=[], comment=row[4].decode(),
            )
            type_list.types.append(type)
            assert type.name
            assert type.base_type

        else:
            # No first column means a value for this type
            value = TypeValueInfo(name=row[2].decode(), value=maybe_decode(row[3]), comment=row[4].decode())

            if value.name and value.value is not None:
                # Don't add ignore keyed types
                if "%s:%s" % (type.name, value.name) not in IGNORE_TYPE_VALUES:
                    type.enum.append(value)

    # Add missing boolean type if it's not there
    if not type_list.get('bool', raise_exception=False):
        type_list.types.append(TypeInfo('bool', 'enum', [], None))

    return type_list


def maybe_decode(o):
    if isinstance(o, bytes):
        return o.decode()
    return o


def parse_messages(messages_rows, type_list):
    message_list = MessageList([])

    group_name = ""
    for row in messages_rows:
        if (row[3] is not None) and all(r == b'' for n, r in enumerate(row[:14]) if n != 3):
            # Only row 3 means it's a group name
            group_name = row[3].decode().title()
        elif row[0] is not None and row[0] != b'':
            # First row means a new message
            name = row[0].decode()
            message = MessageInfo(
                name=name, num=type_list.get_mesg_num(name),
                group_name=group_name, fields=[], comment=row[13].decode(),
            )
            message_list.messages.append(message)
        else:
            # Get components if they exist
            components = []
            component_names = parse_csv_fields(row[5].decode(), 0)
            if component_names and (len(component_names) != 1 or component_names[0] != ''):
                num_components = len(component_names)
                components = [
                    ComponentFieldInfo(
                        name=cmp_name, num=None, scale=fix_scale(cmp_scale),
                        offset=cmp_offset, units=fix_units(cmp_units),
                        bits=cmp_bits, bit_offset=None, accumulate=bool(cmp_accumulate),
                    )
                    for cmp_name, cmp_scale, cmp_offset, cmp_units, cmp_bits, cmp_accumulate in zip(
                        component_names,  # name
                        parse_csv_fields(maybe_decode(row[6]), num_components),   # scale
                        parse_csv_fields(maybe_decode(row[7]), num_components),   # offset
                        parse_csv_fields(maybe_decode(row[8]), num_components),   # units
                        parse_csv_fields(maybe_decode(row[9]), num_components),   # bits
                        parse_csv_fields(maybe_decode(row[10]), num_components),  # accumulate
                    )
                ]

                assert len(components) == num_components
                for component in components:
                    assert component.name
                    assert component.bits

            # Otherwise a field
            # Not a subfield if first row has definition num
            if row[1] is not None and row[1] != b'':
                field = FieldInfo(
                    name=row[2].decode(), type=row[3].decode(), num=maybe_decode(row[1]), scale=fix_scale(row[6]),
                    offset=maybe_decode(row[7]), units=fix_units(row[8].decode()), components=[],
                    subfields=[], comment=row[13].decode(),
                )

                assert field.name
                assert field.type

                # Add components if they exist
                if components:
                    field.components.extend(components)
                    # Wipe out scale, units, offset from field since it's a component
                    field = field._replace(scale=None, offset=None, units=None)

                message.fields.append(field)
            elif row[2] != b'':
                # Sub fields
                subfield = SubFieldInfo(
                    name=row[2].decode(), num=field.num, type=row[3].decode(), scale=fix_scale(row[6]),
                    offset=maybe_decode(row[7]), units=fix_units(row[8].decode()), ref_fields=[],
                    components=[], comment=row[13].decode(),
                )

                ref_field_names = parse_csv_fields(row[11].decode(), 0)
                assert ref_field_names

                if components:
                    subfield.components.extend(components)
                    # Wipe out scale, units, offset from field since it's a component
                    subfield = subfield._replace(scale=None, offset=None, units=None)

                subfield.ref_fields.extend(
                    ReferenceFieldInfo(
                        name=ref_field_name, value=ref_field_value,
                        num=None, raw_value=None,
                    )
                    for ref_field_name, ref_field_value
                    in zip(ref_field_names, parse_csv_fields(row[12].decode(), 0))
                )

                assert len(subfield.ref_fields) == len(ref_field_names)
                if "alert_type" not in ref_field_names:
                    field.subfields.append(subfield)

    # Resolve reference fields for subfields and components
    for message in message_list.messages:
        for field in message.fields:
            for sub_field in field.subfields:
                for n, ref_field_info in enumerate(sub_field.ref_fields[:]):
                    ref_field = message.get(ref_field_info.name)
                    sub_field.ref_fields[n] = ref_field_info._replace(
                        num=ref_field.num,
                        # Get the type of the reference field, then get its numeric value
                        raw_value=type_list.get(ref_field.type).get(ref_field_info.value).value,
                    )
                bit_offset = 0
                for n, component in enumerate(sub_field.components[:]):
                    sub_field.components[n] = component._replace(
                        num=message.get(component.name).num, bit_offset=bit_offset
                    )
                    bit_offset += component.bits
            bit_offset = 0
            for n, component in enumerate(field.components[:]):
                field.components[n] = component._replace(
                    num=message.get(component.name).num, bit_offset=bit_offset
                )
                bit_offset += component.bits

    return message_list


def get_xls_and_version_from_zip(path):
    archive = zipfile.ZipFile(path, 'r')
    profile_version = None

    version_match = re.search(
        r'Profile Version.+?(\d+\.?\d*).*',
        archive.open('c/fit.h').read().decode(),
    )
    if version_match:
        profile_version = ("%f" % float(version_match.group(1))).rstrip('0').ljust(4, '0')

    try:
        return archive.open('Profile.xls'), profile_version
    except KeyError:
        return archive.open('Profile.xlsx'), profile_version


def main(input_xls_or_zip, output_py_path=None):
    if output_py_path and os.path.exists(output_py_path):
        if not open(output_py_path, 'r').read().strip().startswith(PROFILE_HEADER_FIRST_PART):
            print("Python file doesn't begin with appropriate header. Exiting.")
            sys.exit(1)

    if open(input_xls_or_zip, 'rb').read().startswith(XLS_HEADER_MAGIC):
        xls_file, profile_version = input_xls_or_zip, None
    else:
        xls_file, profile_version = get_xls_and_version_from_zip(input_xls_or_zip)

    types_rows, messages_rows = parse_spreadsheet(xls_file, 'Types', 'Messages')
    type_list = parse_types(types_rows)
    message_list = parse_messages(messages_rows, type_list)

    output = '\n'.join([
        "\n%s" % PROFILE_HEADER_FIRST_PART,
        header('EXPORTED PROFILE FROM %s ON %s' % (
            ('SDK VERSION %s' % profile_version) if profile_version else 'SPREADSHEET',
            datetime.datetime.now().strftime('%Y-%m-%d'),
        )),
        header('PARSED %d TYPES (%d VALUES), %d MESSAGES (%d FIELDS)' % (
            len(type_list.types), sum(len(ti.enum) for ti in type_list.types),
            len(message_list.messages), sum(len(mi.fields) for mi in message_list.messages),
        )),
        '', IMPORT_HEADER, '\n',
        str(type_list), '\n',
        SPECIAL_FIELD_DECLARTIONS, '\n',
        str(message_list), ''
    ])

    # TODO: Apply an additional layer of monkey patching to match reference/component
    # fields to actual field objects? Would clean up accesses to these

    if output_py_path:
        open(output_py_path, 'w').write(output)
        print('Profile version %s written to %s' % (
            profile_version if profile_version else '<unknown>',
            output_py_path))
    else:
        print(output.strip())


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: %s <FitSDK.zip | Profile.xls> [profile.py]", os.path.basename(__file__))
        sys.exit(0)

    xls = sys.argv[1]
    profile = sys.argv[2] if len(sys.argv) >= 3 else None
    main(xls, profile)
