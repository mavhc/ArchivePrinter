from __future__ import annotations

import enum
import struct
from dataclasses import dataclass
from typing import Any, cast


class Operation(enum.IntEnum):
    PRINT_JOB = 0x0002
    PRINT_URI = 0x0003
    VALIDATE_JOB = 0x0004
    CREATE_JOB = 0x0005
    SEND_DOCUMENT = 0x0006
    SEND_URI = 0x0007
    CANCEL_JOB = 0x0008
    GET_JOB_ATTRIBUTES = 0x0009
    GET_JOBS = 0x000A
    GET_PRINTER_ATTRIBUTES = 0x000B
    HOLD_JOB = 0x000C
    RELEASE_JOB = 0x000D
    RESTART_JOB = 0x000E
    GET_PRINTER_SUPPORTED_VALUES = 0x0015
    CREATE_PRINTER_SUBSCRIPTIONS = 0x0016
    CREATE_JOB_SUBSCRIPTIONS = 0x0017
    GET_SUBSCRIPTION_ATTRIBUTES = 0x0018
    GET_SUBSCRIPTIONS = 0x0019
    RENEW_SUBSCRIPTION = 0x001A
    CANCEL_SUBSCRIPTION = 0x001B
    GET_NOTIFICATIONS = 0x001C


class Status(enum.IntEnum):
    SUCCESSFUL_OK = 0x0000
    SUCCESSFUL_OK_CONFLICTING_ATTRIBUTES = 0x0002
    CLIENT_ERROR_BAD_REQUEST = 0x0400
    CLIENT_ERROR_NOT_FOUND = 0x0406
    CLIENT_ERROR_DOCUMENT_FORMAT_NOT_SUPPORTED = 0x040A
    CLIENT_ERROR_ATTRIBUTES_OR_VALUES_NOT_SUPPORTED = 0x040B
    SERVER_ERROR_INTERNAL_ERROR = 0x0500
    SERVER_ERROR_OPERATION_NOT_SUPPORTED = 0x0501


class GroupTag(enum.IntEnum):
    OPERATION_ATTRIBUTES = 0x01
    JOB_ATTRIBUTES = 0x02
    END = 0x03
    PRINTER_ATTRIBUTES = 0x04
    UNSUPPORTED_ATTRIBUTES = 0x05
    SUBSCRIPTION_ATTRIBUTES = 0x06


class ValueTag(enum.IntEnum):
    UNSUPPORTED = 0x10
    UNKNOWN = 0x12
    NO_VALUE = 0x13
    INTEGER = 0x21
    BOOLEAN = 0x22
    ENUM = 0x23
    OCTET_STRING = 0x30
    DATE_TIME = 0x31
    RESOLUTION = 0x32
    RANGE_OF_INTEGER = 0x33
    BEG_COLLECTION = 0x34
    TEXT_WITH_LANGUAGE = 0x35
    NAME_WITH_LANGUAGE = 0x36
    END_COLLECTION = 0x37
    TEXT_WITHOUT_LANGUAGE = 0x41
    NAME_WITHOUT_LANGUAGE = 0x42
    KEYWORD = 0x44
    URI = 0x45
    URI_SCHEME = 0x46
    CHARSET = 0x47
    NATURAL_LANGUAGE = 0x48
    MIME_MEDIA_TYPE = 0x49
    MEMBER_ATTR_NAME = 0x4A


@dataclass(frozen=True)
class IppRequest:
    version_major: int
    version_minor: int
    operation_id: int
    request_id: int
    attributes: dict[str, Any]
    document: bytes | str

    @property
    def operation(self) -> Operation | None:
        try:
            return Operation(self.operation_id)
        except ValueError:
            return None


def parse_request(data: bytes) -> IppRequest:
    if len(data) < 8:
        raise ValueError("IPP request is too short")
    version_major, version_minor, operation_id, request_id = struct.unpack(">BBHI", data[:8])
    offset = 8
    attributes_dict: dict[str, Any] = {}
    current_name: str | None = None
    stack: list[list[Any]] = []

    while offset < len(data):
        tag = data[offset]
        offset += 1
        if tag == GroupTag.END:
            return IppRequest(
                version_major=version_major,
                version_minor=version_minor,
                operation_id=operation_id,
                request_id=request_id,
                attributes=attributes_dict,
                document=data[offset:],
            )
        if tag in {item.value for item in GroupTag}:
            current_name = None
            stack.clear()
            continue

        if offset + 4 > len(data):
            raise ValueError("IPP attribute is truncated")
        name_len = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
        if offset + name_len > len(data):
            raise ValueError("IPP attribute name is truncated")
        name = data[offset : offset + name_len].decode("utf-8", errors="replace")
        offset += name_len

        value_len = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
        if offset + value_len > len(data):
            raise ValueError("IPP attribute value is truncated")
        raw_value = data[offset : offset + value_len]
        offset += value_len

        if name:
            current_name = name
        elif current_name:
            name = current_name
        else:
            if not stack:
                continue

        if tag == ValueTag.BEG_COLLECTION:
            new_coll: dict[str, Any] = {}
            if not stack:
                stack.append([name, new_coll, None])
            else:
                parent_member_name = stack[-1][2]
                if parent_member_name:
                    stack.append([parent_member_name, new_coll, None])
            continue

        if tag == ValueTag.MEMBER_ATTR_NAME:
            member_name = raw_value.decode("utf-8", errors="replace")
            if stack:
                stack[-1][2] = member_name
            continue

        if tag == ValueTag.END_COLLECTION:
            if stack:
                coll_name, coll_val, _ = stack.pop()
                if not stack:
                    if coll_name in attributes_dict:
                        existing_val = attributes_dict[coll_name]
                        if isinstance(existing_val, list):
                            cast(list[Any], existing_val).append(coll_val)
                        else:
                            attributes_dict[coll_name] = [existing_val, coll_val]
                    else:
                        attributes_dict[coll_name] = coll_val
                else:
                    parent_coll = stack[-1][1]
                    parent_member_name = stack[-1][2]
                    if parent_member_name:
                        if parent_member_name in parent_coll:
                            existing_val2 = parent_coll[parent_member_name]
                            if isinstance(existing_val2, list):
                                cast(list[Any], existing_val2).append(coll_val)
                            else:
                                parent_coll[parent_member_name] = [existing_val2, coll_val]
                        else:
                            parent_coll[parent_member_name] = coll_val
            continue

        value = decode_value(tag, raw_value)

        if stack:
            current_coll = stack[-1][1]
            member_name = stack[-1][2]
            if member_name:
                if member_name in current_coll:
                    existing_val3 = current_coll[member_name]
                    if isinstance(existing_val3, list):
                        cast(list[Any], existing_val3).append(value)
                    else:
                        current_coll[member_name] = [existing_val3, value]
                else:
                    current_coll[member_name] = value
        else:
            if name in attributes_dict:
                existing_val4 = attributes_dict[name]
                if isinstance(existing_val4, list):
                    cast(list[Any], existing_val4).append(value)
                else:
                    attributes_dict[name] = [existing_val4, value]
            else:
                attributes_dict[name] = value

    raise ValueError("IPP request did not contain an end-of-attributes tag")


def parse_request_stream(stream: Any, content_length: int) -> IppRequest:
    header = stream.read(8)
    if len(header) < 8:
        raise ValueError("IPP request header is truncated")
    version_major, version_minor, operation_id, request_id = struct.unpack(">BBHI", header)
    bytes_read = 8
    
    attributes_dict: dict[str, Any] = {}
    current_name: str | None = None
    stack: list[list[Any]] = []

    while True:
        tag_byte = stream.read(1)
        if not tag_byte:
            raise ValueError("IPP request ended prematurely")
        tag = tag_byte[0]
        bytes_read += 1
        
        if tag == GroupTag.END:
            remaining_bytes = content_length - bytes_read
            document: str | bytes = b""
            if remaining_bytes > 5 * 1024 * 1024:
                import tempfile
                import os
                fd, path = tempfile.mkstemp(prefix="archive-printer-doc-")
                with os.fdopen(fd, "wb") as f:
                    remaining = remaining_bytes
                    buffer_size = 64 * 1024
                    while remaining > 0:
                        chunk_size = min(buffer_size, remaining)
                        chunk = stream.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                document = path
            else:
                document = stream.read(remaining_bytes) if remaining_bytes > 0 else b""
            
            return IppRequest(
                version_major=version_major,
                version_minor=version_minor,
                operation_id=operation_id,
                request_id=request_id,
                attributes=attributes_dict,
                document=document,
            )

        if tag in {item.value for item in GroupTag}:
            current_name = None
            stack.clear()
            continue

        len_bytes = stream.read(2)
        if len(len_bytes) < 2:
            raise ValueError("IPP attribute name length is truncated")
        name_len = struct.unpack(">H", len_bytes)[0]
        bytes_read += 2

        name_bytes = stream.read(name_len)
        if len(name_bytes) < name_len:
            raise ValueError("IPP attribute name is truncated")
        name = name_bytes.decode("utf-8", errors="replace")
        bytes_read += name_len

        val_len_bytes = stream.read(2)
        if len(val_len_bytes) < 2:
            raise ValueError("IPP attribute value length is truncated")
        value_len = struct.unpack(">H", val_len_bytes)[0]
        bytes_read += 2

        raw_value = stream.read(value_len)
        if len(raw_value) < value_len:
            raise ValueError("IPP attribute value is truncated")
        bytes_read += value_len

        if name:
            current_name = name
        elif current_name:
            name = current_name
        else:
            if not stack:
                continue

        if tag == ValueTag.BEG_COLLECTION:
            new_coll: dict[str, Any] = {}
            if not stack:
                stack.append([name, new_coll, None])
            else:
                parent_member_name = stack[-1][2]
                if parent_member_name:
                    stack.append([parent_member_name, new_coll, None])
            continue

        if tag == ValueTag.MEMBER_ATTR_NAME:
            member_name = raw_value.decode("utf-8", errors="replace")
            if stack:
                stack[-1][2] = member_name
            continue

        if tag == ValueTag.END_COLLECTION:
            if stack:
                coll_name, coll_val, _ = stack.pop()
                if not stack:
                    if coll_name in attributes_dict:
                        existing_val = attributes_dict[coll_name]
                        if isinstance(existing_val, list):
                            cast(list[Any], existing_val).append(coll_val)
                        else:
                            attributes_dict[coll_name] = [existing_val, coll_val]
                    else:
                        attributes_dict[coll_name] = coll_val
                else:
                    parent_coll = stack[-1][1]
                    parent_member_name = stack[-1][2]
                    if parent_member_name:
                        if parent_member_name in parent_coll:
                            existing_val2 = parent_coll[parent_member_name]
                            if isinstance(existing_val2, list):
                                cast(list[Any], existing_val2).append(coll_val)
                            else:
                                parent_coll[parent_member_name] = [existing_val2, coll_val]
                        else:
                            parent_coll[parent_member_name] = coll_val
            continue

        value = decode_value(tag, raw_value)

        if stack:
            current_coll = stack[-1][1]
            member_name = stack[-1][2]
            if member_name:
                if member_name in current_coll:
                    existing_val3 = current_coll[member_name]
                    if isinstance(existing_val3, list):
                        cast(list[Any], existing_val3).append(value)
                    else:
                        current_coll[member_name] = [existing_val3, value]
                else:
                    current_coll[member_name] = value
        else:
            if name in attributes_dict:
                existing_val4 = attributes_dict[name]
                if isinstance(existing_val4, list):
                    cast(list[Any], existing_val4).append(value)
                else:
                    attributes_dict[name] = [existing_val4, value]
            else:
                attributes_dict[name] = value


def decode_value(tag: int, value: bytes) -> Any:
    if tag in {ValueTag.INTEGER, ValueTag.ENUM} and len(value) == 4:
        return struct.unpack(">i", value)[0]
    if tag == ValueTag.BOOLEAN and len(value) == 1:
        return value != b"\x00"
    if tag in {ValueTag.UNSUPPORTED, ValueTag.UNKNOWN, ValueTag.NO_VALUE}:
        return f"out-of-band:{tag}"
    if tag == ValueTag.OCTET_STRING:
        return value
    if tag == ValueTag.DATE_TIME and len(value) in (8, 11):
        if len(value) == 11:
            year, month, day, hour, minute, second, decisecond, direction, offset_hour, offset_minute = struct.unpack(">HBBBBBBcBB", value)
            dir_str = direction.decode("ascii", errors="replace")
            return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.{decisecond:d}{dir_str}{offset_hour:02d}:{offset_minute:02d}"
        else:
            year, month, day, hour, minute, second, decisecond = struct.unpack(">HBBBBBB", value)
            return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.{decisecond:d}"
    if tag == ValueTag.RESOLUTION and len(value) == 9:
        h_res, v_res, unit = struct.unpack(">iiB", value)
        unit_str = "dpi" if unit == 3 else "dpcm" if unit == 4 else f"unit-{unit}"
        return f"{h_res}x{v_res} {unit_str}"
    if tag == ValueTag.RANGE_OF_INTEGER and len(value) == 8:
        return struct.unpack(">ii", value)
    if tag in {ValueTag.TEXT_WITH_LANGUAGE, ValueTag.NAME_WITH_LANGUAGE} and len(value) >= 4:
        lang_len = struct.unpack(">H", value[:2])[0]
        if len(value) >= 4 + lang_len:
            lang = value[2:2+lang_len].decode("utf-8", errors="replace")
            text_len = struct.unpack(">H", value[2+lang_len:4+lang_len])[0]
            if len(value) >= 4 + lang_len + text_len:
                text = value[4+lang_len:4+lang_len+text_len].decode("utf-8", errors="replace")
                return (lang, text)
    return value.decode("utf-8", errors="replace")


def response(request: IppRequest, status: Status, groups: list[tuple[GroupTag, list[tuple[int, str, Any]]]] | None = None) -> bytes:
    chunks = [struct.pack(">BBHI", request.version_major, request.version_minor, int(status), request.request_id)]
    chunks.extend(attributes(GroupTag.OPERATION_ATTRIBUTES, [
        (ValueTag.CHARSET, "attributes-charset", "utf-8"),
        (ValueTag.NATURAL_LANGUAGE, "attributes-natural-language", "en"),
    ]))
    for group, attrs in groups or []:
        chunks.extend(attributes(group, attrs))
    chunks.append(bytes([GroupTag.END]))
    return b"".join(chunks)


def attributes(group: GroupTag, attrs: list[tuple[int, str, Any]]) -> list[bytes]:
    chunks = [bytes([group])]
    for tag, name, value in attrs:
        chunks.extend(encode_attribute(tag, name, value))
    return chunks


def encode_attribute(tag: int, name: str, value: Any, is_first: bool = True) -> list[bytes]:
    chunks: list[bytes] = []
    if isinstance(value, list) and tag != ValueTag.BEG_COLLECTION:
        first = is_first
        for item in cast(list[Any], value):
            chunks.extend(encode_single_attribute(tag, name if first else "", item))
            first = False
    else:
        chunks.extend(encode_single_attribute(tag, name if is_first else "", value))
    return chunks


def encode_single_attribute(tag: int, name: str, value: Any) -> list[bytes]:
    chunks: list[bytes] = []
    if tag == ValueTag.BEG_COLLECTION:
        name_bytes = name.encode("utf-8")
        chunks.append(bytes([int(ValueTag.BEG_COLLECTION)]))
        chunks.append(struct.pack(">H", len(name_bytes)))
        chunks.append(name_bytes)
        chunks.append(struct.pack(">H", 0))

        coll_dict = cast(dict[str, Any], value)
        for m_name, m_val in coll_dict.items():
            chunks.append(bytes([int(ValueTag.MEMBER_ATTR_NAME)]))
            chunks.append(struct.pack(">H", 0))
            m_name_bytes = m_name.encode("utf-8")
            chunks.append(struct.pack(">H", len(m_name_bytes)))
            chunks.append(m_name_bytes)

            m_tag = infer_value_tag(m_val)
            chunks.extend(encode_single_attribute(m_tag, "", m_val))

        chunks.append(bytes([int(ValueTag.END_COLLECTION)]))
        chunks.append(struct.pack(">H", 0))
        chunks.append(struct.pack(">H", 0))
    else:
        name_bytes = name.encode("utf-8")
        encoded = encode_value(tag, value)
        chunks.append(bytes([int(tag)]))
        chunks.append(struct.pack(">H", len(name_bytes)))
        if name_bytes:
            chunks.append(name_bytes)
        chunks.append(struct.pack(">H", len(encoded)))
        if encoded:
            chunks.append(encoded)
    return chunks


def infer_value_tag(value: Any) -> int:
    if isinstance(value, bool):
        return int(ValueTag.BOOLEAN)
    if isinstance(value, int):
        return int(ValueTag.INTEGER)
    if isinstance(value, dict):
        return int(ValueTag.BEG_COLLECTION)
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "ipp://", "ipps://", "mailto:")):
            return int(ValueTag.URI)
        return int(ValueTag.KEYWORD)
    if isinstance(value, tuple) and len(cast(tuple[Any, ...], value)) == 2:
        if isinstance(value[0], str) and isinstance(value[1], str):
            return int(ValueTag.TEXT_WITH_LANGUAGE)
        return int(ValueTag.RANGE_OF_INTEGER)
    return int(ValueTag.KEYWORD)


def encode_value(tag: int, value: Any) -> bytes:
    if tag in {ValueTag.INTEGER, ValueTag.ENUM}:
        return struct.pack(">i", int(value))
    if tag == ValueTag.BOOLEAN:
        return b"\x01" if bool(value) else b"\x00"
    if tag in {ValueTag.UNSUPPORTED, ValueTag.UNKNOWN, ValueTag.NO_VALUE}:
        return b""
    if tag == ValueTag.OCTET_STRING:
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")
    if tag == ValueTag.DATE_TIME:
        from datetime import datetime
        if isinstance(value, datetime):
            year = value.year
            month = value.month
            day = value.day
            hour = value.hour
            minute = value.minute
            second = value.second
            decisecond = int(value.microsecond / 100000)
            tz = value.utcoffset()
            if tz is not None:
                seconds = tz.total_seconds()
                direction = "+" if seconds >= 0 else "-"
                seconds = abs(seconds)
                offset_hour = int(seconds // 3600)
                offset_minute = int((seconds % 3600) // 60)
                return struct.pack(
                    ">HBBBBBBcBB",
                    year, month, day, hour, minute, second, decisecond,
                    direction.encode("ascii"), offset_hour, offset_minute
                )
            else:
                return struct.pack(
                    ">HBBBBBB",
                    year, month, day, hour, minute, second, decisecond
                )
        else:
            import re
            match = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d)(\+|-)(\d{2}):(\d{2})$", str(value))
            if match:
                s_year, s_month, s_day, s_hour, s_min, s_sec, s_ds, direction, s_oh, s_om = match.groups()
                return struct.pack(
                    ">HBBBBBBcBB",
                    int(s_year), int(s_month), int(s_day), int(s_hour), int(s_min), int(s_sec), int(s_ds),
                    direction.encode("ascii"), int(s_oh), int(s_om)
                )
            match_no_tz = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d)$", str(value))
            if match_no_tz:
                s_year2, s_month2, s_day2, s_hour2, s_min2, s_sec2, s_ds2 = match_no_tz.groups()
                return struct.pack(
                    ">HBBBBBB",
                    int(s_year2), int(s_month2), int(s_day2), int(s_hour2), int(s_min2), int(s_sec2), int(s_ds2)
                )
            return str(value).encode("utf-8")
    if tag == ValueTag.RESOLUTION:
        if isinstance(value, tuple) and len(cast(tuple[Any, ...], value)) == 3:
            try:
                t3 = cast(tuple[Any, Any, Any], value)
                return struct.pack(">iiB", int(t3[0]), int(t3[1]), int(t3[2]))
            except (ValueError, TypeError):
                pass
        import re
        res_match = re.match(r"^(\d+)x(\d+)\s*(dpi|dpcm)$", str(value).strip().lower())
        if res_match:
            h_res, v_res, unit_str = res_match.groups()
            unit = 3 if unit_str == "dpi" else 4
            try:
                return struct.pack(">iiB", int(h_res), int(v_res), unit)
            except (ValueError, TypeError):
                pass
        return str(value).encode("utf-8")
    if tag == ValueTag.RANGE_OF_INTEGER:
        if isinstance(value, (list, tuple)) and len(cast(list[Any] | tuple[Any, ...], value)) == 2:
            try:
                seq = cast(list[Any] | tuple[Any, Any], value)
                return struct.pack(">ii", int(seq[0]), int(seq[1]))
            except (ValueError, TypeError):
                pass
    if tag in {ValueTag.TEXT_WITH_LANGUAGE, ValueTag.NAME_WITH_LANGUAGE}:
        if isinstance(value, (list, tuple)) and len(cast(list[Any] | tuple[Any, ...], value)) == 2:
            seq2 = cast(list[Any] | tuple[Any, Any], value)
            lang_bytes = str(seq2[0]).encode("utf-8")
            text_bytes = str(seq2[1]).encode("utf-8")
            return (
                struct.pack(">H", len(lang_bytes))
                + lang_bytes
                + struct.pack(">H", len(text_bytes))
                + text_bytes
            )
    return str(value).encode("utf-8")
