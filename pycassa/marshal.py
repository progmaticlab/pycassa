"""
Tools for marshalling and unmarshalling data stored
in Cassandra.
"""
import six
import uuid
import struct
import calendar
from datetime import datetime
from decimal import Decimal

import pycassa.util as util

_number_types = frozenset((int, int, float))


def make_packer(fmt_string):
    return struct.Struct(fmt_string)

_bool_packer = make_packer('>B')
_float_packer = make_packer('>f')
_double_packer = make_packer('>d')
_long_packer = make_packer('>q')
_int_packer = make_packer('>i')
_short_packer = make_packer('>H')

_BASIC_TYPES = ('BytesType', 'LongType', 'IntegerType', 'UTF8Type',
                'AsciiType', 'LexicalUUIDType', 'TimeUUIDType',
                'CounterColumnType', 'FloatType', 'DoubleType',
                'DateType', 'BooleanType', 'UUIDType', 'Int32Type',
                'DecimalType', 'TimestampType')

def extract_type_name(typestr):
    if typestr is None:
        return 'BytesType'

    if "DynamicCompositeType" in typestr:
        return _get_composite_name(typestr)

    if "CompositeType" in typestr:
        return _get_composite_name(typestr)

    if "ReversedType" in typestr:
        return _get_inner_type(typestr)

    index = typestr.rfind('.')
    if index != -1:
        typestr = typestr[index + 1:]
    if typestr not in _BASIC_TYPES:
        typestr = 'BytesType'
    return typestr

def _get_inner_type(typestr):
    """ Given a str like 'org.apache...ReversedType(LongType)',
    return just 'LongType' """
    first_paren = typestr.find('(')
    return typestr[first_paren + 1:-1]

def _get_inner_types(typestr):
    """ Given a str like 'org.apache...CompositeType(LongType, DoubleType)',
    return a tuple of the inner types, like ('LongType', 'DoubleType') """
    internal_str = _get_inner_type(typestr)
    return list(map(str.strip, internal_str.split(',')))

def _get_composite_name(typestr):
    types = list(map(extract_type_name, _get_inner_types(typestr)))
    return "CompositeType(" + ", ".join(types) + ")"

def _to_timestamp(v):
    # Expects Value to be either date or datetime
    try:
        converted = calendar.timegm(v.utctimetuple())
        converted = converted * 1e3 + getattr(v, 'microsecond', 0) / 1e3
    except AttributeError:
        # Ints and floats are valid timestamps too
        if type(v) not in _number_types:
            raise TypeError('DateType arguments must be a datetime or timestamp')

        converted = v * 1e3
    return int(converted)

def get_composite_packer(typestr=None, composite_type=None):
    assert (typestr or composite_type), "Must provide typestr or " + \
            "CompositeType instance"
    if typestr:
        packers = list(map(packer_for, _get_inner_types(typestr)))
    elif composite_type:
        packers = [c.pack for c in composite_type.components]

    len_packer = _short_packer.pack

    def pack_composite(items, slice_start=None):
        last_index = len(items) - 1
        s = ''
        for i, (item, packer) in enumerate(zip(items, packers)):
            eoc = '\x00'
            if isinstance(item, tuple):
                item, inclusive = item
                if inclusive:
                    if slice_start:
                        eoc = '\xff'
                    elif slice_start is False:
                        eoc = '\x01'
                else:
                    if slice_start:
                        eoc = '\x01'
                    elif slice_start is False:
                        eoc = '\xff'
            elif i == last_index:
                if slice_start:
                    eoc = '\xff'
                elif slice_start is False:
                    eoc = '\x01'

            packed = packer(item)
            s += ''.join((len_packer(len(packed)), packed, eoc))
        return s

    return pack_composite

def get_composite_unpacker(typestr=None, composite_type=None):
    assert (typestr or composite_type), "Must provide typestr or " + \
            "CompositeType instance"
    if typestr:
        unpackers = list(map(unpacker_for, _get_inner_types(typestr)))
    elif composite_type:
        unpackers = [c.unpack for c in composite_type.components]

    len_unpacker = lambda v: _short_packer.unpack(v)[0]

    def unpack_composite(bytestr):
        # The composite format for each component is:
        #   <len>   <value>   <eoc>
        # 2 bytes | ? bytes | 1 byte
        components = []
        i = iter(unpackers)
        while bytestr:
            unpacker = next(i)
            length = len_unpacker(bytestr[:2])
            components.append(unpacker(bytestr[2:2 + length]))
            bytestr = bytestr[3 + length:]
        return tuple(components)

    return unpack_composite

def get_dynamic_composite_packer(typestr):
    cassandra_types = {}
    for inner_type in _get_inner_types(typestr):
        alias, cassandra_type = inner_type.split('=>')
        cassandra_types[alias] = cassandra_type

    len_packer = _short_packer.pack

    def pack_dynamic_composite(items, slice_start=None):
        last_index = len(items) - 1
        s = ''
        i = 0
        for (alias, item) in items:
            eoc = '\x00'
            if isinstance(alias, tuple):
                inclusive = item
                alias, item = alias
                if inclusive:
                    if slice_start:
                        eoc = '\xff'
                    elif slice_start is False:
                        eoc = '\x01'
                else:
                    if slice_start:
                        eoc = '\x01'
                    elif slice_start is False:
                        eoc = '\xff'
            elif i == last_index:
                if slice_start:
                    eoc = '\xff'
                elif slice_start is False:
                    eoc = '\x01'
            if isinstance(alias, str) and len(alias) == 1:
                header = '\x80' + alias
                packer = packer_for(cassandra_types[alias])
            else:
                cassandra_type = str(alias).split('(')[0]
                header = len_packer(len(cassandra_type)) + cassandra_type
                packer = packer_for(cassandra_type)
            i += 1

            packed = packer(item)
            s += ''.join((header, len_packer(len(packed)), packed, eoc))
        return s

    return pack_dynamic_composite

def get_dynamic_composite_unpacker(typestr):
    cassandra_types = {}
    for inner_type in _get_inner_types(typestr):
        alias, cassandra_type = inner_type.split('=>')
        cassandra_types[alias] = cassandra_type

    len_unpacker = lambda v: _short_packer.unpack(v)[0]

    def unpack_dynamic_composite(bytestr):
        # The composite format for each component is:
        # <header>     <len>      <value>     <eoc>
        # ? bytes  |  2 bytes  |  ? bytes  |  1 byte
        types = []
        components = []
        while bytestr:
            header = len_unpacker(bytestr[:2])
            if header & 0x8000:
                alias = bytestr[1]
                types.append(alias)
                unpacker = unpacker_for(cassandra_types[alias])
                bytestr = bytestr[2:]
            else:
                cassandra_type = bytestr[2:2 + header]
                types.append(cassandra_type)
                unpacker = unpacker_for(cassandra_type)
                bytestr = bytestr[2 + header:]
            length = len_unpacker(bytestr[:2])
            components.append(unpacker(bytestr[2:2 + length]))
            bytestr = bytestr[3 + length:]
        return tuple(zip(types, components))

    return unpack_dynamic_composite

def packer_for(typestr):
    if typestr is None:
        return lambda v: v

    if "DynamicCompositeType" in typestr:
        return get_dynamic_composite_packer(typestr)

    if "CompositeType" in typestr:
        return get_composite_packer(typestr)

    if "ReversedType" in typestr:
        return packer_for(_get_inner_type(typestr))

    data_type = extract_type_name(typestr)

    if data_type in ('DateType', 'TimestampType'):
        def pack_date(v, _=None):
            return _long_packer.pack(_to_timestamp(v))
        return pack_date

    elif data_type == 'BooleanType':
        def pack_bool(v, _=None):
            return _bool_packer.pack(bool(v))
        return pack_bool

    elif data_type == 'DoubleType':
        def pack_double(v, _=None):
            return _double_packer.pack(v)
        return pack_double

    elif data_type == 'FloatType':
        def pack_float(v, _=None):
            return _float_packer.pack(v)
        return pack_float

    elif data_type == 'DecimalType':
        def pack_decimal(dec, _=None):
            sign, digits, exponent = dec.as_tuple()
            unscaled = int(''.join(map(str, digits)))
            if sign:
                unscaled *= -1
            scale = _int_packer.pack(-exponent)
            unscaled = encode_int(unscaled)
            return scale + unscaled
        return pack_decimal

    elif data_type == 'LongType':
        def pack_long(v, _=None):
            return _long_packer.pack(v)
        return pack_long

    elif data_type == 'Int32Type':
        def pack_int32(v, _=None):
            return _int_packer.pack(v)
        return pack_int32

    elif data_type == 'IntegerType':
        return encode_int

    elif data_type == 'UTF8Type':
        def pack_utf8(v, _=None):
            try:
                return v.encode('utf-8')
            except UnicodeDecodeError:
                # v is already utf-8 encoded
                return v
        return pack_utf8

    elif 'UUIDType' in data_type:
        def pack_uuid(value, slice_start=None):
            if slice_start is None:
                value = util.convert_time_to_uuid(value,
                        randomize=True)
            else:
                value = util.convert_time_to_uuid(value,
                        lowest_val=slice_start,
                        randomize=False)

            if not hasattr(value, 'bytes'):
                raise TypeError("%s is not valid for UUIDType" % value)
            return value.bytes
        return pack_uuid

    elif data_type == "CounterColumnType":
        def noop(value, slice_start=None):
            return value
        return noop

    else: # data_type == 'BytesType' or something unknown
        def pack_bytes(v, _=None):
            if not isinstance(v, six.string_types):
                raise TypeError("A str or unicode value was expected, " +
                                "but %s was received instead (%s)"
                                % (v.__class__.__name__, str(v)))
            return v
        return pack_bytes

def unpacker_for(typestr):
    if typestr is None:
        return lambda v: v

    if "DynamicCompositeType" in typestr:
        return get_dynamic_composite_unpacker(typestr)

    if "CompositeType" in typestr:
        return get_composite_unpacker(typestr)

    if "ReversedType" in typestr:
        return unpacker_for(_get_inner_type(typestr))

    data_type = extract_type_name(typestr)

    if data_type == 'BytesType':
        return lambda v: v

    elif data_type in ('DateType', 'TimestampType'):
        return lambda v: datetime.utcfromtimestamp(
                _long_packer.unpack(v)[0] / 1e3)

    elif data_type == 'BooleanType':
        return lambda v: bool(_bool_packer.unpack(v)[0])

    elif data_type == 'DoubleType':
        return lambda v: _double_packer.unpack(v)[0]

    elif data_type == 'FloatType':
        return lambda v: _float_packer.unpack(v)[0]

    elif data_type == 'DecimalType':
        def unpack_decimal(v):
            scale = _int_packer.unpack(v[:4])[0]
            unscaled = decode_int(v[4:])
            return Decimal('%de%d' % (unscaled, -scale))
        return unpack_decimal

    elif data_type == 'LongType':
        return lambda v: _long_packer.unpack(v)[0]

    elif data_type == 'Int32Type':
        return lambda v: _int_packer.unpack(v)[0]

    elif data_type == 'IntegerType':
        return decode_int

    elif data_type == 'UTF8Type':
        return lambda v: v.decode('utf-8')

    elif 'UUIDType' in data_type:
        return lambda v: uuid.UUID(bytes=v)

    else:
        return lambda v: v

def encode_int(x, *args):
    if x >= 0:
        out = []
        while x >= 256:
            out.append(struct.pack('B', 0xff & x))
            x >>= 8
        out.append(struct.pack('B', 0xff & x))
        if x > 127:
            out.append('\x00')
    else:
        x = -1 - x
        out = []
        while x >= 256:
            out.append(struct.pack('B', 0xff & ~x))
            x >>= 8
        if x <= 127:
            out.append(struct.pack('B', 0xff & ~x))
        else:
            out.append(struct.pack('>H', 0xffff & ~x))

    return ''.join(reversed(out))

def decode_int(term, *args):
    if term != "":
        val = int(term.encode('hex'), 16)
        if (ord(term[0]) & 128) != 0:
            val = val - (1 << (len(term) * 8))
        return val
