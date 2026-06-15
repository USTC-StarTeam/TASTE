from __future__ import annotations

import base64
from html import escape


_ECC_LOW_BLOCKS: dict[int, tuple[int, list[int]]] = {
    1: (7, [19]),
    2: (10, [34]),
    3: (15, [55]),
    4: (20, [80]),
    5: (26, [108]),
    6: (18, [68, 68]),
    7: (20, [78, 78]),
    8: (24, [97, 97]),
    9: (30, [116, 116]),
    10: (18, [68, 68, 69, 69]),
}

_ALIGNMENT_POSITIONS: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

_FORMAT_LOW_MASK0 = 0b111011111000100


def qr_svg_data_url(text: str, *, border: int = 4) -> str:
    svg = qr_svg(text, border=border)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def qr_svg(text: str, *, border: int = 4) -> str:
    matrix = qr_matrix(text)
    size = len(matrix)
    view_size = size + border * 2
    path_parts: list[str] = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                path_parts.append(f"M{x + border},{y + border}h1v1h-1z")
    path_data = " ".join(path_parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view_size} {view_size}" '
        f'role="img" aria-label="{escape("TASTE connection QR code", quote=True)}">'
        f'<rect width="{view_size}" height="{view_size}" fill="#fff"/>'
        f'<path d="{path_data}" fill="#111827"/>'
        "</svg>"
    )


def qr_matrix(text: str) -> list[list[bool]]:
    data = str(text or "").encode("utf-8")
    version, data_codewords = _encode_data_codewords(data)
    codewords = _add_error_correction(version, data_codewords)
    size = 21 + (version - 1) * 4
    modules: list[list[bool | None]] = [[None] * size for _ in range(size)]
    is_function = [[False] * size for _ in range(size)]

    def set_function(x: int, y: int, dark: bool) -> None:
        if 0 <= x < size and 0 <= y < size:
            modules[y][x] = dark
            is_function[y][x] = True

    _draw_function_patterns(version, modules, is_function, set_function)
    _draw_codewords(codewords, modules, is_function)
    _draw_format_bits(modules, set_function)
    if version >= 7:
        _draw_version_bits(version, set_function, size)

    return [[bool(value) for value in row] for row in modules]


def _encode_data_codewords(data: bytes) -> tuple[int, list[int]]:
    for version, (_ecc_per_block, block_sizes) in _ECC_LOW_BLOCKS.items():
        capacity_bits = sum(block_sizes) * 8
        char_count_bits = 8 if version <= 9 else 16
        bits = _int_bits(0b0100, 4) + _int_bits(len(data), char_count_bits)
        for byte in data:
            bits.extend(_int_bits(byte, 8))
        if len(bits) > capacity_bits:
            continue
        bits.extend([0] * min(4, capacity_bits - len(bits)))
        while len(bits) % 8:
            bits.append(0)
        codewords = [_bits_to_int(bits[index:index + 8]) for index in range(0, len(bits), 8)]
        pad = 0xEC
        while len(codewords) < sum(block_sizes):
            codewords.append(pad)
            pad = 0x11 if pad == 0xEC else 0xEC
        return version, codewords
    raise ValueError("connection link is too long for the built-in mobile QR generator")


def _add_error_correction(version: int, data_codewords: list[int]) -> list[int]:
    ecc_per_block, block_sizes = _ECC_LOW_BLOCKS[version]
    blocks: list[tuple[list[int], list[int]]] = []
    offset = 0
    for size in block_sizes:
        block = data_codewords[offset:offset + size]
        offset += size
        blocks.append((block, _reed_solomon_remainder(block, ecc_per_block)))

    result: list[int] = []
    for index in range(max(len(block[0]) for block in blocks)):
        for data_block, _ecc in blocks:
            if index < len(data_block):
                result.append(data_block[index])
    for index in range(ecc_per_block):
        for _data_block, ecc in blocks:
            result.append(ecc[index])
    return result


def _draw_function_patterns(version: int, modules, is_function, set_function) -> None:
    size = len(modules)
    _draw_finder(0, 0, set_function)
    _draw_finder(size - 7, 0, set_function)
    _draw_finder(0, size - 7, set_function)

    for index in range(8, size - 8):
        set_function(6, index, index % 2 == 0)
        set_function(index, 6, index % 2 == 0)

    for y in _ALIGNMENT_POSITIONS[version]:
        for x in _ALIGNMENT_POSITIONS[version]:
            if not is_function[y][x]:
                _draw_alignment(x, y, set_function)

    for index in range(9):
        if index != 6:
            set_function(8, index, False)
            set_function(index, 8, False)
    for index in range(8):
        set_function(size - 1 - index, 8, False)
        set_function(8, size - 1 - index, False)
    set_function(8, size - 8, True)


def _draw_finder(x: int, y: int, set_function) -> None:
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            xx = x + dx
            yy = y + dy
            dark = (
                0 <= dx <= 6
                and 0 <= dy <= 6
                and (dx in {0, 6} or dy in {0, 6} or (2 <= dx <= 4 and 2 <= dy <= 4))
            )
            set_function(xx, yy, dark)


def _draw_alignment(cx: int, cy: int, set_function) -> None:
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            set_function(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)


def _draw_codewords(codewords: list[int], modules, is_function) -> None:
    bits: list[bool] = []
    for codeword in codewords:
        bits.extend(bool((codeword >> bit) & 1) for bit in range(7, -1, -1))

    size = len(modules)
    bit_index = 0
    upward = True
    x = size - 1
    while x > 0:
        if x == 6:
            x -= 1
        for offset in range(size):
            y = size - 1 - offset if upward else offset
            for dx in range(2):
                xx = x - dx
                if is_function[y][xx]:
                    continue
                dark = bits[bit_index] if bit_index < len(bits) else False
                if (xx + y) % 2 == 0:
                    dark = not dark
                modules[y][xx] = dark
                bit_index += 1
        upward = not upward
        x -= 2


def _draw_format_bits(modules, set_function) -> None:
    size = len(modules)
    bits = _FORMAT_LOW_MASK0
    for index in range(6):
        set_function(8, index, bool((bits >> index) & 1))
    set_function(8, 7, bool((bits >> 6) & 1))
    set_function(8, 8, bool((bits >> 7) & 1))
    set_function(7, 8, bool((bits >> 8) & 1))
    for index in range(9, 15):
        set_function(14 - index, 8, bool((bits >> index) & 1))
    for index in range(8):
        set_function(size - 1 - index, 8, bool((bits >> index) & 1))
    for index in range(8, 15):
        set_function(8, size - 15 + index, bool((bits >> index) & 1))
    set_function(8, size - 8, True)


def _draw_version_bits(version: int, set_function, size: int) -> None:
    bits = _version_bits(version)
    for index in range(18):
        bit = bool((bits >> index) & 1)
        set_function(size - 11 + index % 3, index // 3, bit)
        set_function(index // 3, size - 11 + index % 3, bit)


def _version_bits(version: int) -> int:
    remainder = version
    for _ in range(12):
        remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
    return (version << 12) | (remainder & 0xFFF)


def _reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    generator = _reed_solomon_generator(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index in range(degree):
            result[index] ^= _gf_multiply(generator[index + 1], factor)
    return result


def _reed_solomon_generator(degree: int) -> list[int]:
    result = [1]
    for index in range(degree):
        result = _polynomial_multiply(result, [1, _gf_power(2, index)])
    return result


def _polynomial_multiply(left: list[int], right: list[int]) -> list[int]:
    result = [0] * (len(left) + len(right) - 1)
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            result[i + j] ^= _gf_multiply(a, b)
    return result


def _gf_power(value: int, power: int) -> int:
    result = 1
    for _ in range(power):
        result = _gf_multiply(result, value)
    return result


def _gf_multiply(left: int, right: int) -> int:
    result = 0
    while right:
        if right & 1:
            result ^= left
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
        right >>= 1
    return result


def _int_bits(value: int, width: int) -> list[int]:
    return [(value >> bit) & 1 for bit in range(width - 1, -1, -1)]


def _bits_to_int(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value
