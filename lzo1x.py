"""
LZO1X decompression in pure Python.

Original MKX packages store their data in LZO1X-compressed blocks, so this decoder is required to read
any unmodified game package (both for exporting and as a conversion template).

The LZO1X format was created by Markus F.X.J. Oberhumer. This decoder was written for this project and is
covered by this project's LICENSE.
Only decompression is implemented; the tool writes ZLIB.
"""


def lzo1x_decompress(src, out_len=None):
    """Decompress one LZO1X block. `out_len`, when given, must match the decompressed size."""
    ip, op, state = 0, bytearray(), 0

    def copy_match(m_pos, t):
        dist = len(op) - m_pos
        if dist <= 0 or m_pos < 0:
            raise ValueError('LZO: bad match distance')
        if dist >= t:
            op.extend(op[m_pos:m_pos + t])
        else:
            for _ in range(t):
                op.append(op[m_pos]); m_pos += 1

    if src[0] > 17:
        t = src[0] - 17; ip = 1
        op.extend(src[ip:ip + t]); ip += t
        state = t if t < 4 else 4
    while True:
        t = src[ip]; ip += 1
        if t < 16:
            if state == 0:
                if t == 0:
                    cnt = 0
                    while src[ip] == 0: ip += 1; cnt += 1
                    t = cnt * 255 + 15 + src[ip]; ip += 1
                t += 3
                op.extend(src[ip:ip + t]); ip += t; state = 4
                continue
            elif state != 4:
                nxt = t & 3
                m_pos = len(op) - 1 - (t >> 2) - (src[ip] << 2); ip += 1
                copy_match(m_pos, 2)
                state = nxt; op.extend(src[ip:ip + nxt]); ip += nxt
                continue
            nxt = t & 3
            m_pos = len(op) - (1 + 0x800) - (t >> 2) - (src[ip] << 2); ip += 1
            t = 3
        elif t >= 64:
            nxt = t & 3
            m_pos = len(op) - 1 - ((t >> 2) & 7) - (src[ip] << 3); ip += 1
            t = (t >> 5) + 1
        elif t >= 32:
            t = (t & 31) + 2
            if t == 2:
                cnt = 0
                while src[ip] == 0: ip += 1; cnt += 1
                t += cnt * 255 + 31 + src[ip]; ip += 1
            nv = src[ip] | (src[ip + 1] << 8); ip += 2
            m_pos = len(op) - 1 - (nv >> 2); nxt = nv & 3
        else:
            m_pos = len(op) - ((t & 8) << 11)
            t = (t & 7) + 2
            if t == 2:
                cnt = 0
                while src[ip] == 0: ip += 1; cnt += 1
                t += cnt * 255 + 7 + src[ip]; ip += 1
            nv = src[ip] | (src[ip + 1] << 8); ip += 2
            m_pos -= nv >> 2; nxt = nv & 3
            if m_pos == len(op):
                break
            m_pos -= 0x4000
        copy_match(m_pos, t)
        state = nxt; op.extend(src[ip:ip + nxt]); ip += nxt
    if out_len is not None and len(op) != out_len:
        raise ValueError('LZO size mismatch %d != %d' % (len(op), out_len))
    return bytes(op)
