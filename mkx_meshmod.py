#!/usr/bin/env python3
"""
mkx_meshmod.py - backend of MKX Character Studio (unofficial community tool).

Reads and writes Mortal Kombat X (PC) character packages (Unreal Engine 3 package version 677, licensee 157):
  * package summary, name/import/export tables and chunked compression (ZLIB, LZO via lzo1x.py, stored)
  * skeletal mesh LOD0: sections, chunks, packed skin weights, vertex streams, tessellation adjacency
  * skeleton reference pose and hierarchy, inline BC7 texture mips
  * glTF 2.0 import/export and a BC7 (mode 6) encoder

Use launcher.py (desktop app) or studio_cli.py. Python 3.10+; numpy and Pillow for texture work.
"""
import argparse, json, math, os, re, struct, sys, zlib
from collections import defaultdict

from lzo1x import lzo1x_decompress

PKG_TAG = 0x9E2A83C1
PKG_StoreCompressed = 0x02000000
COMPRESS_ZLIB, COMPRESS_LZO = 1, 2
CHUNK_SIZE, BLOCK_SIZE = 0x80000, 0x20000


class MKXError(Exception):
    """A user-facing problem (bad input model, unsupported data). The message explains how to fix it."""


# ----------------------------------------------------------------------------------------------- binary helpers
class Reader:
    def __init__(self, d, o=0):
        self.d, self.o = d, o

    def _u(self, fmt, n):
        v = struct.unpack_from(fmt, self.d, self.o)[0]; self.o += n; return v

    def u8(self): return self._u('<B', 1)
    def u16(self): return self._u('<H', 2)
    def u32(self): return self._u('<I', 4)
    def i32(self): return self._u('<i', 4)
    def u64(self): return self._u('<Q', 8)
    def f32(self): return self._u('<f', 4)

    def raw(self, n):
        v = self.d[self.o:self.o + n]
        if len(v) != n:
            raise ValueError('read past end of buffer')
        self.o += n; return v

    def fstr(self):
        n = self.i32()
        if n == 0: return ''
        if n > 0: return bytes(self.raw(n)).rstrip(b'\0').decode('latin1')
        return bytes(self.raw(-2 * n)).decode('utf-16le').rstrip('\0')


class Writer:
    def __init__(self):
        self.b = bytearray()

    def u8(self, v): self.b += struct.pack('<B', v)
    def u16(self, v): self.b += struct.pack('<H', v)
    def u32(self, v): self.b += struct.pack('<I', v)
    def i32(self, v): self.b += struct.pack('<i', v)
    def u64(self, v): self.b += struct.pack('<Q', v)
    def f32(self, v): self.b += struct.pack('<f', v)
    def raw(self, v): self.b += v
    def __len__(self): return len(self.b)


def uncompress_block(flags, data, size):
    if flags & COMPRESS_ZLIB:
        out = zlib.decompress(data)
    elif flags & COMPRESS_LZO:
        out = lzo1x_decompress(data, size)
    elif flags == 0 or flags & 8:  # PFS = stored
        out = bytes(data)
    else:
        raise ValueError('unsupported compression flags %d' % flags)
    if len(out) != size:
        raise ValueError('block size mismatch')
    return out


# ----------------------------------------------------------------------------------------------- package
class Package:
    """MKX .xxx package. All offsets in the name/import/export tables and in bulk data are offsets into the
    *uncompressed* image (self.image); the on-disk file holds the summary followed by compressed chunks."""

    def __init__(self, path):
        self.path = path
        self.file = open(path, 'rb').read()
        r = Reader(self.file)
        s = self.s = {}
        if r.u32() != PKG_TAG:
            raise ValueError('%s: not an Unreal package' % path)
        v = r.u32(); s['FileVersion'], s['Licensee'] = v & 0xffff, v >> 16
        if (s['FileVersion'], s['Licensee']) != (677, 157):
            raise ValueError('unexpected package version %d/%d (MKX is 677/157)' % (s['FileVersion'], s['Licensee']))
        s['TotalHeaderSize'] = r.u32(); s['TeamFourCC'] = bytes(r.raw(4)); s['TeamVersion'] = r.u32()
        s['ShaderVersion'] = r.u32(); s['MetadataVersion'] = bytes(r.raw(16)).hex()
        s['FolderName'] = r.fstr()
        self.pf_off = r.o; s['PackageFlags'] = r.u32()
        s['NameCount'] = r.u32(); s['NameOffset'] = r.u64()
        s['ExportCount'] = r.u32(); s['ExportOffset'] = r.u64()
        s['ImportCount'] = r.u32(); s['ImportOffset'] = r.u64()
        s['GameThreadExportCount'] = r.u32(); s['DependsOffset'] = r.u64()
        s['BulkDataOffset'] = r.u64(); s['ImportExportGuidsOffset'] = r.u64()
        s['ImportGuidsCount'] = r.u32(); s['ExportGuidsCount'] = r.u32(); s['ThumbnailTableOffset'] = r.u64()
        s['Guid'] = bytes(r.raw(16)).hex(); s['EngineVersion'] = r.u32(); s['CookedContentVersion'] = r.u32()
        self.cf_off = r.o; s['CompressionFlags'] = r.u32()
        self.chunks = [(r.u64(), r.u32(), r.u64(), r.u32()) for _ in range(r.u32())]
        self.after_chunks = r.o
        n = r.u32(); [r.fstr() for _ in range(n)]           # AdditionalPackagesToCook
        n = r.u32(); r.o += n                                 # MD5 set (skipped by the engine at load)
        n = r.u32(); cv_start = r.o; r.o += n                 # class version table
        rr = Reader(self.file, cv_start); self.class_versions = {}
        for _ in range(rr.u32()):
            nm = rr.fstr(); self.class_versions[nm] = rr.u32()
        s['LinkerRoot'] = r.fstr()
        self.summary_end = r.o
        self.image = None

    # -- uncompressed image
    def summary_bytes(self, chunks, compression_flags, package_flags):
        w = Writer()
        head = bytearray(self.file[:self.cf_off])
        struct.pack_into('<I', head, self.pf_off, package_flags)
        w.raw(head); w.u32(compression_flags); w.u32(len(chunks))
        for c in chunks:
            w.u64(c[0]); w.u32(c[1]); w.u64(c[2]); w.u32(c[3])
        w.raw(self.file[self.after_chunks:self.summary_end])
        return w.b

    def decompress(self):
        if self.image is not None:
            return self.image
        if not self.chunks:
            self.image = bytearray(self.file)
            return self.image
        total = max(c[0] + c[1] for c in self.chunks)
        img = bytearray(total)
        summ = self.summary_bytes([], 0, self.s['PackageFlags'] & ~PKG_StoreCompressed)
        img[:min(len(summ), self.s['NameOffset'])] = summ[:self.s['NameOffset']]
        for (uo, us, co, cs) in self.chunks:
            r = Reader(self.file, co)
            if r.u64() != PKG_TAG:
                raise ValueError('bad chunk tag at %x' % co)
            r.u64(); r.u64(); usz = r.u64()
            blocks, left = [], usz
            while left > 0:
                bc, bu = r.u64(), r.u64(); blocks.append((bc, bu)); left -= bu
            pos = uo
            for bc, bu in blocks:
                img[pos:pos + bu] = uncompress_block(self.s['CompressionFlags'], self.file[r.o:r.o + bc], bu)
                r.o += bc; pos += bu
        self.image = img
        return img

    # -- tables
    def read_tables(self):
        d = self.decompress(); s = self.s
        r = Reader(d, s['NameOffset']); self.names = [r.fstr() for _ in range(s['NameCount'])]
        r = Reader(d, s['ImportOffset']); self.imports = []
        for _ in range(s['ImportCount']):
            cp, cpn, cn, cnn, outer, on, onn = r.u32(), r.u32(), r.u32(), r.u32(), r.i32(), r.u32(), r.u32()
            r.o += 16
            self.imports.append(dict(ClassName=self.name(cn, cnn), Outer=outer, ObjectName=self.name(on, onn)))
        r = Reader(d, s['ExportOffset']); self.exports = []
        for i in range(s['ExportCount']):
            pos = r.o
            e = dict(Class=r.i32(), Super=r.i32(), Outer=r.i32()); on, onn = r.u32(), r.u32()
            e['ObjectName'] = self.name(on, onn)
            r.o = pos + 52; e['SerialSize'] = r.u32(); e['SerialOffset'] = r.u64(); e['table_pos'] = pos
            r.o = pos + 88
            self.exports.append(e)
        return self

    def name(self, i, n=0):
        nm = self.names[i] if 0 <= i < len(self.names) else '<bad name %d>' % i
        return nm if n == 0 else '%s_%d' % (nm, n - 1)

    def objref(self, i):
        if i == 0: return 'None'
        if i < 0:
            im = self.imports[-i - 1]
            return (self.objref(im['Outer']) + '.' if im['Outer'] else '') + im['ObjectName']
        e = self.exports[i - 1]
        return (self.objref(e['Outer']) + '.' if e['Outer'] else '') + e['ObjectName']

    def classname(self, i):
        if i == 0: return 'Class'
        return self.imports[-i - 1]['ObjectName'] if i < 0 else self.exports[i - 1]['ObjectName']

    def find_export(self, path, cls=None):
        hits = [i + 1 for i in range(len(self.exports))
                if self.objref(i + 1).lower() == path.lower() and (cls is None or self.classname(self.exports[i]['Class']) == cls)]
        if not hits:
            raise KeyError('export %r (%s) not found' % (path, cls or 'any class'))
        return hits[0]

    def export_blob(self, idx):
        e = self.exports[idx - 1]
        return bytes(self.image[e['SerialOffset']:e['SerialOffset'] + e['SerialSize']])

    # -- modification
    def set_export_location(self, idx, offset, size):
        e = self.exports[idx - 1]
        struct.pack_into('<IQ', self.image, e['table_pos'] + 52, size, offset)
        e['SerialSize'], e['SerialOffset'] = size, offset

    def append_export_data(self, idx, blob):
        off = len(self.image)
        self.image += blob
        self.set_export_location(idx, off, len(blob))
        return off

    def save(self, path, mode='zlib'):
        img = self.image
        nameoff, hdr_end = self.s['NameOffset'], self.s['TotalHeaderSize']
        pflags = self.s['PackageFlags']
        if mode == 'none':
            summ = self.summary_bytes([], 0, pflags & ~PKG_StoreCompressed)
            if len(summ) > nameoff:
                raise ValueError('summary does not fit before NameOffset')
            out = bytearray(img); out[:len(summ)] = summ
        else:
            segs = [(nameoff, hdr_end)]
            p = hdr_end
            while p < len(img):
                segs.append((p, min(len(img), p + CHUNK_SIZE))); p = segs[-1][1]
            blobs = []
            for (a, b) in segs:
                blocks, w = [], Writer()
                for q in range(a, b, BLOCK_SIZE):
                    raw = bytes(img[q:min(b, q + BLOCK_SIZE)])
                    blocks.append((zlib.compress(raw, 6), len(raw)))
                w.u64(PKG_TAG); w.u64(BLOCK_SIZE); w.u64(sum(len(c) for c, _ in blocks)); w.u64(b - a)
                for c, u in blocks:
                    w.u64(len(c)); w.u64(u)
                for c, _ in blocks:
                    w.raw(c)
                blobs.append(bytes(w.b))
            summ_len = len(self.summary_bytes([(0, 0, 0, 0)] * len(segs), COMPRESS_ZLIB, pflags | PKG_StoreCompressed))
            chunks, off = [], summ_len
            for (a, b), bl in zip(segs, blobs):
                chunks.append((a, b - a, off, len(bl))); off += len(bl)
            out = bytearray(self.summary_bytes(chunks, COMPRESS_ZLIB, pflags | PKG_StoreCompressed))
            for bl in blobs:
                out += bl
            out += b'\0' * ((-len(out)) % 0x8000)
        with open(path, 'wb') as f:
            f.write(out)
        return len(out)


# ----------------------------------------------------------------------------------------------- tagged properties
def parse_props(pkg, d, o, end=None):
    """Returns ([(name, type, extra, size, arrayindex, value_offset)], end_offset). MKX: bool value is 4 bytes."""
    r = Reader(d, o); out = []
    while True:
        name = pkg.name(r.u32(), r.u32())
        if name == 'None':
            return out, r.o
        typ = pkg.name(r.u32(), r.u32())
        size, aidx = r.i32(), r.i32()
        extra = None
        if typ in ('StructProperty', 'ByteProperty'):
            extra = pkg.name(r.u32(), r.u32())
        elif typ == 'BoolProperty':
            extra = r.u32()
        out.append((name, typ, extra, size, aidx, r.o))
        r.o += size
        if end is not None and r.o > end:
            raise ValueError('property list overruns export')


def prop_map(props):
    return {p[0]: p for p in props}


# ----------------------------------------------------------------------------------------------- skeleton
class Skeleton:
    def __init__(self, pkg, idx):
        d = pkg.image; e = pkg.exports[idx - 1]
        self.props, end = parse_props(pkg, d, e['SerialOffset'])
        r = Reader(d, end)
        self.pose = []
        for _ in range(r.u32()):
            q = struct.unpack_from('<4f', d, r.o); t = struct.unpack_from('<3f', d, r.o + 16); r.o += 28
            self.pose.append((q, t))
        self.parents = [r.u16() for _ in range(r.u32())]
        self.names = [pkg.name(r.u32(), r.u32()) for _ in range(r.u32())]
        self.index = {n: i for i, n in enumerate(self.names)}
        self.globals = []
        for i, (q, t) in enumerate(self.pose):
            m = mat_from_qt(q, t)
            self.globals.append(m if i == 0 else mat_mul(self.globals[self.parents[i]], m))


# ----------------------------------------------------------------------------------------------- math
def mat_from_qt(q, t, s=(1, 1, 1)):
    x, y, z, w = q
    return [[(1 - 2 * (y * y + z * z)) * s[0], 2 * (x * y - z * w) * s[1], 2 * (x * z + y * w) * s[2], t[0]],
            [2 * (x * y + z * w) * s[0], (1 - 2 * (x * x + z * z)) * s[1], 2 * (y * z - x * w) * s[2], t[1]],
            [2 * (x * z - y * w) * s[0], 2 * (y * z + x * w) * s[1], (1 - 2 * (x * x + y * y)) * s[2], t[2]],
            [0, 0, 0, 1]]


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def mat_inv_affine(m):
    a = [row[:3] for row in m[:3]]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    inv = [[(a[(j + 1) % 3][(i + 1) % 3] * a[(j + 2) % 3][(i + 2) % 3] - a[(j + 1) % 3][(i + 2) % 3] * a[(j + 2) % 3][(i + 1) % 3]) / det
            for j in range(3)] for i in range(3)]
    t = [m[0][3], m[1][3], m[2][3]]
    nt = [-sum(inv[i][k] * t[k] for k in range(3)) for i in range(3)]
    return [inv[0] + [nt[0]], inv[1] + [nt[1]], inv[2] + [nt[2]], [0, 0, 0, 1]]


def xform_point(m, p):
    return tuple(m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3))


def xform_dir(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def normalize(v):
    l = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / l, v[1] / l, v[2] / l) if l > 1e-12 else (0.0, 0.0, 1.0)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


S_SWAP = [[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]]  # UE (x,y,z) <-> glTF (x,z,y); its own inverse


def ue2g(v): return (v[0], v[2], v[1])
def ue2g_q(q): return (-q[0], -q[2], -q[1], q[3])


def half(h): return struct.unpack('<e', struct.pack('<H', h))[0]
def to_half(f): return struct.unpack('<H', struct.pack('<e', max(-65504.0, min(65504.0, f))))[0]


def pack_normal(v, w=0):
    return bytes(max(0, min(255, int(round((c + 1.0) * 127.5)))) for c in v[:3]) + bytes([w])


def unpack_normal(b):
    return tuple(x / 127.5 - 1.0 for x in b)


def unpack_influence(e):
    """MKX FSkelWeightVertex (8 bytes): four 10-bit chunk-local bone indices in the low 40 bits, then three weight
    bytes; the 4th weight is implied (255 - w0 - w1 - w2). Verified against UModel and against posed renders."""
    v = int.from_bytes(e[0:5], 'little')
    w = [e[5], e[6], e[7]]
    return [(v >> (10 * k)) & 0x3ff for k in range(4)], w + [255 - sum(w)]


def pack_influence(idx, w):
    v = 0
    for k in range(4):
        v |= (idx[k] & 0x3ff) << (10 * k)
    return v.to_bytes(5, 'little') + bytes(w[:3])


# ----------------------------------------------------------------------------------------------- skeletal mesh
TRISORT_CUSTOM_LEFT_RIGHT = 5   # section sort mode whose triangles are stored twice (one order per view side)


class LODModel:
    """Cooked skeletal-mesh LOD: sections, index buffers, chunks, vertex streams and tessellation adjacency."""

    def num_tris(self):
        return sum(s['tris'] for s in self.sections)

    @staticmethod
    def parse(d, o, has_vertex_colors=False):
        r = Reader(d, o); L = LODModel(); L.start = o
        L.sections = [dict(mat=r.u16(), chunk=r.u16(), base=r.u32(), tris=r.u32(), sort=r.u8()) for _ in range(r.u32())]
        L.index_ranges = []                                          # (offset, bytes) of every triangle index array
        n = r.u32(); L.indices = list(struct.unpack_from('<%dH' % n, d, r.o)); L.index_ranges.append((r.o, 2 * n)); r.o += 2 * n
        n = r.u32(); L.indices32 = list(struct.unpack_from('<%dI' % n, d, r.o)); L.index_ranges.append((r.o, 4 * n)); r.o += 4 * n
        n = r.u32(); L.index_ranges.append((r.o, 2 * n)); r.o += 2 * n  # ShadowIndices
        n = r.u32(); L.active_bones = list(struct.unpack_from('<%dH' % n, d, r.o)); r.o += 2 * n
        n = r.u32(); r.o += n                                        # ShadowTriangleDoubleSided
        L.chunks = []
        for _ in range(r.u32()):
            c = dict(base=r.u32())
            for arr in ('rigid_verts', 'soft_verts', 'rigid_morph', 'soft_morph'):
                if r.u32() != 0:
                    raise ValueError('chunk has CPU-side %s (not a cooked mesh?)' % arr)
            c['bonemap'] = [r.u16() for _ in range(r.u32())]
            c['rigid'], c['soft'], c['maxinf'] = r.u32(), r.u32(), r.u32()
            L.chunks.append(c)
        L.size_field, L.num_verts = r.u32(), r.u32()
        if r.u32() != 0:
            raise ValueError('LOD has edges (unsupported)')
        n = r.u32(); L.required_bones = bytes(r.raw(n))
        L.bulk = []
        for _ in range(2):                                           # RawPointIndices, RawPointIndices32
            fl, cnt, sz, off = r.u32(), r.u32(), r.u64(), r.u64()
            L.bulk.append((fl, cnt, sz, off, r.o))
            if not fl & 1:
                r.o += sz
        L.pos_stride, nv, cnt = r.u32(), r.u32(), r.u32()
        L.positions = [struct.unpack_from('<3f', d, r.o + 12 * i) for i in range(cnt)]; r.o += L.pos_stride * cnt
        L.tan_stride, nv, cnt = r.u32(), r.u32(), r.u32()
        L.tangents = [bytes(d[r.o + 8 * i:r.o + 8 * i + 8]) for i in range(cnt)]; r.o += L.tan_stride * cnt
        L.wgt_stride, nv, cnt = r.u32(), r.u32(), r.u32()
        L.influences = [bytes(d[r.o + 8 * i:r.o + 8 * i + 8]) for i in range(cnt)]; r.o += L.wgt_stride * cnt
        L.num_uvs, L.uv_stride, L.uv_numverts, cnt = r.u32(), r.u32(), r.u32(), r.u32()
        L.uv_raw = bytes(d[r.o:r.o + L.uv_stride * cnt]); r.o += L.uv_stride * cnt
        if has_vertex_colors:
            raise ValueError('meshes with vertex colors are not supported yet')
        L.dq_stride, dqn = r.u32(), r.u32()        # data array is only serialized when NumVertices != 0
        if dqn:
            cnt = r.u32(); L.dq = list(struct.unpack_from('<%df' % cnt, d, r.o)); r.o += L.dq_stride * cnt
        else:
            L.dq = None
        n = r.u32(); r.o += n                                        # VertexBufferGPUMorphSkin
        n = r.u32(); L.adjacency = list(struct.unpack_from('<%dH' % n, d, r.o)); L.index_ranges.append((r.o, 2 * n)); r.o += 2 * n
        n = r.u32(); L.adjacency32 = list(struct.unpack_from('<%dI' % n, d, r.o)); L.index_ranges.append((r.o, 4 * n)); r.o += 4 * n
        # TriangleProbabilityDistribution: area-weighted triangle sampling as an alias table (empty on most meshes)
        n = r.u32(); L.prob = list(struct.unpack_from('<%df' % n, d, r.o)); r.o += 4 * n
        n = r.u32(); L.alias = list(struct.unpack_from('<%di' % n, d, r.o)); r.o += 4 * n
        L.end = r.o
        return L

    def uv(self, v, ch=0):
        a, b = struct.unpack_from('<HH', self.uv_raw, v * self.uv_stride + 4 * ch)
        return half(a), half(b)


class SkeletalMesh:
    def __init__(self, pkg, idx):
        self.pkg, self.idx = pkg, idx
        d = pkg.image; e = pkg.exports[idx - 1]
        self.base, self.size = e['SerialOffset'], e['SerialSize']
        self.props, pend = parse_props(pkg, d, self.base, self.base + self.size)
        self.pm = prop_map(self.props)
        self.props_len = pend - self.base
        r = Reader(d, pend)
        nl = r.u32()
        if nl != 1:
            raise ValueError('expected exactly 1 cooked LOD, found %d' % nl)
        self.lod_index = r.u32()
        hvc = 'bHasVertexColors' in self.pm and self.pm['bHasVertexColors'][2]
        self.lod = LODModel.parse(d, r.o, hvc)
        self.tail = bytes(d[self.lod.end:self.base + self.size])
        if self.lod.end > self.base + self.size:
            raise ValueError('LOD overruns export')

    def materials(self):
        p = self.pm.get('Materials')
        if not p: return []
        d = self.pkg.image; cnt = struct.unpack_from('<i', d, p[5])[0]
        return [self.pkg.objref(x) for x in struct.unpack_from('<%di' % cnt, d, p[5] + 4)]

    def skeleton_export(self):
        p = self.pm.get('Skeleton')
        return struct.unpack_from('<i', self.pkg.image, p[5])[0] if p else None


def build_lod_bytes(geo, abs_start):
    """Serialize an FStaticLODModel. abs_start = absolute uncompressed offset where these bytes will live
    (needed because bulk data stores absolute offsets that the lazy loader seeks to)."""
    w = Writer(); nv = len(geo['positions'])
    w.u32(len(geo['sections']))
    for s in geo['sections']:
        w.u16(s['mat']); w.u16(s['chunk']); w.u32(s['base']); w.u32(s['tris']); w.u8(0)
    w.u32(len(geo['indices'])); w.raw(struct.pack('<%dH' % len(geo['indices']), *geo['indices']))
    w.u32(0)                                  # IndexBuffer32
    w.u32(0)                                  # ShadowIndices
    w.u32(len(geo['active_bones'])); w.raw(struct.pack('<%dH' % len(geo['active_bones']), *geo['active_bones']))
    w.u32(0)                                  # ShadowTriangleDoubleSided
    w.u32(len(geo['chunks']))
    for c in geo['chunks']:
        w.u32(c['base']); w.u32(0); w.u32(0); w.u32(0); w.u32(0)
        w.u32(len(c['bonemap'])); w.raw(struct.pack('<%dH' % len(c['bonemap']), *c['bonemap']))
        w.u32(c['rigid']); w.u32(c['soft']); w.u32(c['maxinf'])
    w.u32(geo['size_field']); w.u32(nv)
    w.u32(0)                                  # Edges
    w.u32(len(geo['required_bones'])); w.raw(geo['required_bones'])
    raw = struct.pack('<%dH' % nv, *range(nv))  # RawPointIndices: identity mapping (unused at runtime)
    w.u32(0); w.u32(nv); w.u64(len(raw)); w.u64(abs_start + len(w) + 8); w.raw(raw)
    w.u32(0); w.u32(0); w.u64(0); w.u64(abs_start + len(w) + 8)
    w.u32(12); w.u32(nv); w.u32(nv)
    for p in geo['positions']:
        w.raw(struct.pack('<3f', *p))
    w.u32(8); w.u32(nv); w.u32(nv)
    for t in geo['tangents']:
        w.raw(t)
    w.u32(8); w.u32(nv); w.u32(nv)
    for e in geo['influences']:
        w.raw(e)
    # UV buffer: MKX stores NumTexCoords=2, stride 8, NumVertices=nv*2 and the per-vertex (uv0,uv1) array twice
    uvblock = b''.join(struct.pack('<4H', *(to_half(x) for x in (a[0], a[1], b[0], b[1]))) for a, b in zip(geo['uv0'], geo['uv1']))
    w.u32(2); w.u32(8); w.u32(nv * 2); w.u32(nv * 2); w.raw(uvblock); w.raw(uvblock)
    if geo['dq'] is None:                     # mesh without bHasDQBlendWeights: empty buffer, no data array
        w.u32(4); w.u32(0)
    else:
        w.u32(4); w.u32(nv); w.u32(nv); w.raw(struct.pack('<%df' % nv, *geo['dq']))
    w.u32(0)                                  # VertexBufferGPUMorphSkin
    w.u32(len(geo['adjacency'])); w.raw(struct.pack('<%dH' % len(geo['adjacency']), *geo['adjacency']))
    w.u32(0)                                  # AdjacencyIndexBuffer32
    w.u32(0); w.u32(0)                        # TriangleProbabilityDistribution
    return bytes(w.b)


# ----------------------------------------------------------------------------------------------- PN-AEN adjacency
def build_adjacency(indices, positions, uv0):
    """12 indices per triangle: corners, 3 dominant edges (neighbour's vertices across each edge, oriented to this
    edge), 3 dominant corners (vertex with smallest UV0 among those sharing the position). Matches shipped character
    data for 99.9% of indices."""
    key = lambda v: tuple(round(c, 3) for c in positions[v])
    dom = {}
    for v in range(len(positions)):
        k = key(v); rk = (uv0[v][0], uv0[v][1], v)
        if k not in dom or rk < dom[k][0]:
            dom[k] = (rk, v)
    edges = defaultdict(list)
    ntri = len(indices) // 3
    for t in range(ntri):
        tri = indices[3 * t:3 * t + 3]
        for e in range(3):
            a, b = tri[e], tri[(e + 1) % 3]
            edges[frozenset((key(a), key(b)))].append((t, key(a), a, b))
    out = []
    for t in range(ntri):
        tri = indices[3 * t:3 * t + 3]
        out.extend(tri)
        for e in range(3):
            a, b = tri[e], tri[(e + 1) % 3]
            cand = [x for x in edges[frozenset((key(a), key(b)))] if x[0] != t]
            if cand:
                _, oka, oa, ob = cand[0]
                out.extend((oa, ob) if oka == key(a) else (ob, oa))
            else:
                out.extend((a, b))
        out.extend(dom[key(v)][1] for v in tri)
    return out


# ----------------------------------------------------------------------------------------------- glTF
class GLTF:
    def __init__(self, path):
        data = open(path, 'rb').read()
        self.bins = []
        if data[:4] == b'glTF':
            off = 12; self.j = None
            while off < len(data):
                ln, typ = struct.unpack_from('<II', data, off); chunk = data[off + 8:off + 8 + ln]
                if typ == 0x4E4F534A: self.j = json.loads(chunk.decode('utf-8'))
                elif typ == 0x004E4942: self.bins.append(chunk)
                off += 8 + ln
        else:
            self.j = json.loads(data.decode('utf-8'))
        self.buffers = []
        import base64
        for i, b in enumerate(self.j.get('buffers', [])):
            uri = b.get('uri')
            if uri is None: self.buffers.append(self.bins[0])
            elif uri.startswith('data:'): self.buffers.append(base64.b64decode(uri.split(',', 1)[1]))
            else: self.buffers.append(open(os.path.join(os.path.dirname(path), uri), 'rb').read())

    def accessor(self, i):
        a = self.j['accessors'][i]
        if 'sparse' in a:
            raise ValueError('sparse accessors are not supported (disable shape keys / morph export)')
        n = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}[a['type']]
        fmt = {5126: 'f', 5125: 'I', 5123: 'H', 5121: 'B', 5122: 'h', 5120: 'b'}[a['componentType']]
        csz = struct.calcsize(fmt)
        v = self.j['bufferViews'][a['bufferView']]
        buf = self.buffers[v['buffer']]
        base = v.get('byteOffset', 0) + a.get('byteOffset', 0)
        stride = v.get('byteStride', csz * n)
        norm = a.get('normalized', False)
        div = {5121: 255.0, 5123: 65535.0, 5120: 127.0, 5122: 32767.0}.get(a['componentType'], 1.0)
        out = []
        for k in range(a['count']):
            t = struct.unpack_from('<%d%s' % (n, fmt), buf, base + stride * k)
            if norm: t = tuple(x / div for x in t)
            out.append(t if n > 1 else t[0])
        return out

    def node_globals(self):
        nodes = self.j['nodes']; G = [None] * len(nodes)

        def local(nd):
            if 'matrix' in nd:
                m = nd['matrix']; return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
            return mat_from_qt(nd.get('rotation', [0, 0, 0, 1]), nd.get('translation', [0, 0, 0]), nd.get('scale', [1, 1, 1]))

        def walk(i, parent):
            G[i] = mat_mul(parent, local(nodes[i])) if parent else local(nodes[i])
            for c in nodes[i].get('children', []):
                walk(c, G[i])
        children = {c for nd in nodes for c in nd.get('children', [])}
        for i in range(len(nodes)):
            if i not in children:
                walk(i, None)
        return G


def write_glb(path, j, binbuf):
    js = json.dumps(j, separators=(',', ':')).encode()
    js += b' ' * ((-len(js)) % 4); binbuf = bytes(binbuf) + b'\0' * ((-len(binbuf)) % 4)
    with open(path, 'wb') as f:
        f.write(struct.pack('<III', 0x46546C67, 2, 28 + len(js) + len(binbuf)))
        f.write(struct.pack('<II', len(js), 0x4E4F534A)); f.write(js)
        f.write(struct.pack('<II', len(binbuf), 0x004E4942)); f.write(binbuf)


class GLBBuilder:
    def __init__(self):
        self.bin = bytearray()
        self.j = {"asset": {"version": "2.0", "generator": "mkx_meshmod"}, "buffers": [{}], "bufferViews": [], "accessors": [],
                  "nodes": [], "meshes": [], "skins": [], "materials": [], "scenes": [{"nodes": []}], "scene": 0}

    def acc(self, values, ctype, typ, target=None, minmax=False):
        fmt = {5126: 'f', 5125: 'I', 5123: 'H'}[ctype]
        n = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}[typ]
        flat = values if n == 1 else [x for v in values for x in v]
        self.bin += b'\0' * ((-len(self.bin)) % 4)
        bv = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": 4 * 0}
        data = struct.pack('<%d%s' % (len(flat), fmt), *flat); bv['byteLength'] = len(data); self.bin += data
        if target: bv['target'] = target
        self.j['bufferViews'].append(bv)
        a = {"bufferView": len(self.j['bufferViews']) - 1, "componentType": ctype, "count": len(values), "type": typ}
        if minmax:
            a['min'] = [min(v[i] for v in values) for i in range(n)]; a['max'] = [max(v[i] for v in values) for i in range(n)]
        self.j['accessors'].append(a)
        return len(self.j['accessors']) - 1

    def save(self, path):
        self.j['buffers'][0] = {"byteLength": len(self.bin)}
        write_glb(path, self.j, self.bin)


# ----------------------------------------------------------------------------------------------- commands
def load(pkgpath):
    p = Package(pkgpath); p.decompress(); p.read_tables(); return p


def cmd_info(a):
    p = load(a.package)
    print('%s: version %d/%d, %d names, %d imports, %d exports, compression %d, %d chunks' % (
        a.package, p.s['FileVersion'], p.s['Licensee'], len(p.names), len(p.imports), len(p.exports), p.s['CompressionFlags'], len(p.chunks)))
    print('class versions: ' + ', '.join('%s=%d' % kv for kv in p.class_versions.items() if kv[1]))
    for i, e in enumerate(p.exports):
        cn = p.classname(e['Class'])
        if a.all or cn in ('SkeletalMesh', 'Skeleton', 'Texture2D', 'MaterialInstanceConstant', 'Material', 'CharacterAsset'):
            print('%5d %-26s %-70s %9d @ %x' % (i + 1, cn, p.objref(i + 1), e['SerialSize'], e['SerialOffset']))


def list_skeletal_meshes(p):
    """Object paths of every SkeletalMesh export in the package."""
    return [p.objref(i + 1) for i, e in enumerate(p.exports) if p.classname(e['Class']) == 'SkeletalMesh']


def hide_skeletal_mesh(p, mesh):
    """Make SkeletalMesh `mesh` draw nothing, in place: every triangle index (draw, shadow and adjacency arrays) becomes
    vertex 0, so all triangles have zero area. Sizes and offsets stay the same, so nothing else in the package moves.
    Returns the number of triangles hidden."""
    m = SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
    for off, size in m.lod.index_ranges:
        p.image[off:off + size] = bytes(size)
    return m.lod.num_tris()


def mic_texture_params(p, idx):
    """{parameter name: texture object index} from a MaterialInstanceConstant export's TextureParameterValues."""
    d = p.image; e = p.exports[idx - 1]
    try:
        props, _ = parse_props(p, d, e['SerialOffset'], e['SerialOffset'] + e['SerialSize'])
    except Exception:
        return {}
    out = {}
    for name, typ, extra, size, aidx, vo in props:
        if name != 'TextureParameterValues' or typ != 'ArrayProperty':
            continue
        o = vo + 4
        for _ in range(struct.unpack_from('<i', d, vo)[0]):
            el, o = parse_props(p, d, o, vo + size)
            em = prop_map(el)
            if 'ParameterName' in em and 'ParameterValue' in em:
                pn = p.name(*struct.unpack_from('<II', d, em['ParameterName'][5]))
                out[pn] = struct.unpack_from('<i', d, em['ParameterValue'][5])[0]
    return out


def mesh_material_ids(p, m):
    pm = m.pm.get('Materials')
    if not pm:
        return []
    d = p.image
    return list(struct.unpack_from('<%di' % struct.unpack_from('<i', d, pm[5])[0], d, pm[5] + 4))


PARAM_ORDER = {'DiffuseMap': 0, 'NormalMap': 1, 'Pmsk': 2}


def mesh_texture_slots(p, mesh):
    """Textures stored inside this package that the mesh's materials use: list of dicts
    {texture, param (DiffuseMap/NormalMap/Pmsk/...), slots, size (w, h), srgb, replaceable}."""
    m = SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
    found = {}
    for slot, mid in enumerate(mesh_material_ids(p, m)):
        if mid <= 0:
            continue
        for param, tid in mic_texture_params(p, mid).items():
            if tid <= 0 or p.classname(p.exports[tid - 1]['Class']) != 'Texture2D':
                continue
            f = found.setdefault(p.objref(tid), dict(params=set(), slots=set()))
            f['params'].add(param); f['slots'].add(slot)
    out = []
    for path, f in found.items():
        t = read_texture(p, p.find_export(path, 'Texture2D'))
        ok = t['fmt'] == 22 and all(x['data_pos'] is not None for x in t['mips'])
        param = sorted(f['params'], key=lambda x: (PARAM_ORDER.get(x, 9), x))[0]
        out.append(dict(texture=path, param=param, slots=sorted(f['slots']), size=(t['mips'][0]['w'], t['mips'][0]['h']),
                        srgb=bool(t['srgb']), replaceable=ok))
    out.sort(key=lambda x: (PARAM_ORDER.get(x['param'], 9), x['texture']))
    return out


def texture_png_bytes(p, path):
    """Decode an inline BC7 texture's top mip to PNG bytes (needs Pillow); None if not possible."""
    try:
        import io
        from PIL import Image
        t = read_texture(p, p.find_export(path, 'Texture2D'))
        if t['fmt'] != 22 or any(m['data_pos'] is None for m in t['mips']):
            return None
        top = bytes(p.image[t['mips'][0]['data_pos']:t['mips'][0]['data_pos'] + t['mips'][0]['size']])
        im = Image.open(io.BytesIO(dds_bytes(t['mips'][0]['w'], t['mips'][0]['h'], [top], t['srgb'])))
        im.load()
        buf = io.BytesIO(); im.convert('RGB').save(buf, 'PNG')
        return buf.getvalue()
    except Exception:
        return None


def export_ref(p, mesh, out, embed_textures=False, log=print):
    """SkeletalMesh (LOD0) + Skeleton -> .glb. Materials are named slotNN_<MaterialInstance>; with embed_textures the
    diffuse (DiffuseMap) texture of each material is embedded so the model shows textured in Blender."""
    mi = p.find_export(mesh, 'SkeletalMesh'); m = SkeletalMesh(p, mi)
    ski = m.skeleton_export()
    if not ski or ski < 0:
        raise MKXError('%s has no Skeleton inside this package, so it cannot be exported' % mesh)
    sk = Skeleton(p, ski); L = m.lod; mats = m.materials()
    g = GLBBuilder(); n = len(sk.names)
    for i in range(n):
        q, t = sk.pose[i]
        g.j['nodes'].append({"name": sk.names[i], "translation": list(ue2g(t)), "rotation": list(ue2g_q(q))})
    kids = defaultdict(list)
    for i in range(1, n): kids[sk.parents[i]].append(i)
    for pi, c in kids.items(): g.j['nodes'][pi]['children'] = c
    ibm = []
    for i in range(n):
        gm = mat_mul(mat_mul(S_SWAP, sk.globals[i]), S_SWAP); inv = mat_inv_affine(gm)
        ibm.append([inv[r][c] for c in range(4) for r in range(4)])
    g.j['skins'].append({"name": "Skeleton", "joints": list(range(n)), "skeleton": 0, "inverseBindMatrices": g.acc(ibm, 5126, 'MAT4')})
    nv = len(L.positions)
    pos = [ue2g(x) for x in L.positions]
    nrm = [normalize(ue2g(unpack_normal(t[4:8]))) for t in L.tangents]
    joints, weights = [None] * nv, [None] * nv
    for c in L.chunks:
        for v in range(c['base'], c['base'] + c['rigid'] + c['soft']):
            idx, w = unpack_influence(L.influences[v])
            joints[v] = [c['bonemap'][idx[k]] if w[k] else 0 for k in range(4)]
            weights[v] = [x / 255.0 for x in w]
    # TangentX -> glTF TANGENT; the UE->glTF reflection flips handedness, so w = -1 when TangentZ.W says +1
    tan = [tuple(normalize(ue2g(unpack_normal(t[0:4])))) + ((-1.0,) if t[7] >= 128 else (1.0,)) for t in L.tangents]
    attrs = {"POSITION": g.acc(pos, 5126, 'VEC3', 34962, True), "NORMAL": g.acc(nrm, 5126, 'VEC3', 34962),
             "TANGENT": g.acc(tan, 5126, 'VEC4', 34962),
             "TEXCOORD_0": g.acc([L.uv(v, 0) for v in range(nv)], 5126, 'VEC2', 34962),
             "TEXCOORD_1": g.acc([L.uv(v, 1) for v in range(nv)], 5126, 'VEC2', 34962),
             "JOINTS_0": g.acc(joints, 5123, 'VEC4', 34962), "WEIGHTS_0": g.acc(weights, 5126, 'VEC4', 34962)}
    slot_tex = {}
    if embed_textures:
        img_index = {}
        for slot, mid in enumerate(mesh_material_ids(p, m)):
            if mid <= 0: continue
            tid = mic_texture_params(p, mid).get('DiffuseMap')
            if not tid or tid <= 0: continue
            path = p.objref(tid)
            if path not in img_index:
                png = texture_png_bytes(p, path)
                if png is None:
                    log('WARNING: cannot embed %s (requires an inline BC7 texture and Pillow PNG support)' % path)
                    continue
                g.bin += b'\0' * ((-len(g.bin)) % 4)
                g.j['bufferViews'].append({"buffer": 0, "byteOffset": len(g.bin), "byteLength": len(png)}); g.bin += png
                g.j.setdefault('images', []).append({"name": path.split('.')[-1], "mimeType": "image/png", "bufferView": len(g.j['bufferViews']) - 1})
                g.j.setdefault('textures', []).append({"source": len(g.j['images']) - 1})
                img_index[path] = len(g.j['textures']) - 1
            slot_tex[slot] = img_index[path]
    prims = []
    for s in L.sections:
        mname = mats[s['mat']].split('.')[-1] if s['mat'] < len(mats) else 'mat'
        mat = {"name": 'slot%02d_%s' % (s['mat'], mname)}
        if s['mat'] in slot_tex:
            mat['pbrMetallicRoughness'] = {"baseColorTexture": {"index": slot_tex[s['mat']]}, "metallicFactor": 0.0, "roughnessFactor": 0.8}
        g.j['materials'].append(mat)
        prims.append({"attributes": attrs, "material": len(g.j['materials']) - 1,
                      "indices": g.acc(L.indices[s['base']:s['base'] + 3 * s['tris']], 5125, 'SCALAR', 34963)})
    g.j['meshes'].append({"name": mesh.split('.')[-1], "primitives": prims})
    g.j['nodes'].append({"name": mesh.split('.')[-1], "mesh": 0, "skin": 0})
    g.j['scenes'][0]['nodes'] = [0, len(g.j['nodes']) - 1]
    g.save(out)
    log('wrote %s: %d bones, %d verts, %d tris, %d material slots%s' % (out, n, nv, L.num_tris(), len(mats),
        (', %d embedded texture(s)' % len(set(slot_tex.values()))) if embed_textures else ''))
    log('  slots: ' + ', '.join('%d=%s' % (i, x.split('.')[-1]) for i, x in enumerate(mats)))
    return out


def cmd_export_ref(a):
    export_ref(load(a.package), a.mesh, a.out, embed_textures=a.embed_textures)


class NearestGrid:
    def __init__(self, pts, cell=2.0):
        self.pts, self.cell, self.g = pts, cell, defaultdict(list)
        for i, p in enumerate(pts):
            self.g[(int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell)), int(math.floor(p[2] / cell)))].append(i)

    def nearest(self, p):
        c = tuple(int(math.floor(p[k] / self.cell)) for k in range(3))
        best, bd = None, 1e30
        for r in range(0, 64):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for dz in range(-r, r + 1):
                        if max(abs(dx), abs(dy), abs(dz)) != r: continue
                        for i in self.g.get((c[0] + dx, c[1] + dy, c[2] + dz), ()):
                            q = self.pts[i]; dd = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2
                            if dd < bd: best, bd = i, dd
            if best is not None and math.sqrt(bd) <= r * self.cell:
                break
        return best, math.sqrt(bd)


def material_slot(name, mats):
    m = re.search(r'slot\s*(\d+)', name or '', re.I)
    if m:
        return int(m.group(1))
    short = [x.split('.')[-1].lower() for x in mats]
    base = (name or '').split('.')[0].lower()
    if base in short:
        return short.index(base)
    return None


def pack_uv2(prims, pad=0.004):
    """A new second UV map like MKX's own: every UV island of every primitive gets its own spot, sized by its real
    surface area, packed into the unit square without overlaps (the game paints blood and damage through it).
    prims: [(positions, uv0, triangle indices)]; returns one [(u, v)] list per primitive."""
    islands = []                                       # (prim, vertex list, 2D points, width, height)
    for pi, (P, UV, idx) in enumerate(prims):
        parent = list(range(len(P)))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for k in range(0, len(idx), 3):
            a, b, c = find(idx[k]), find(idx[k + 1]), find(idx[k + 2])
            parent[b] = a; parent[find(c)] = a
        groups = defaultdict(list)
        for k in range(0, len(idx), 3):
            groups[find(idx[k])].append(idx[k:k + 3])
        for tris in groups.values():
            area3 = area2 = 0.0
            for t in tris:
                p0, p1, p2 = (P[v] for v in t)
                area3 += 0.5 * math.sqrt(sum(x * x for x in cross([p1[i] - p0[i] for i in range(3)], [p2[i] - p0[i] for i in range(3)])))
                (u0, v0), (u1, v1), (u2, v2) = (UV[v][:2] for v in t)
                area2 += 0.5 * abs((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0))
            verts = sorted({v for t in tris for v in t})
            if area2 > 1e-12:
                s = math.sqrt(area3 / area2) if area3 > 0 else 1.0
                pts = {v: (UV[v][0] * s, UV[v][1] * s) for v in verts}
            else:                                       # flat or missing UVs: project onto the island's main plane
                ext = [max(P[v][i] for v in verts) - min(P[v][i] for v in verts) for i in range(3)]
                ax = sorted(range(3), key=lambda i: -ext[i])[:2]
                pts = {v: (P[v][ax[0]], P[v][ax[1]]) for v in verts}
            us, vs = [q[0] for q in pts.values()], [q[1] for q in pts.values()]
            pts = {v: (q[0] - min(us), q[1] - min(vs)) for v, q in pts.items()}
            islands.append((pi, pts, max(max(us) - min(us), 1e-6), max(max(vs) - min(vs), 1e-6)))
    order = sorted(range(len(islands)), key=lambda i: -islands[i][3])

    def place(scale):                                  # shelf packing; None if it does not fit
        x = y = row = 0.0; spots = {}
        for i in order:
            w, h = islands[i][2] * scale + 2 * pad, islands[i][3] * scale + 2 * pad
            if w > 1:
                return None
            if x + w > 1:
                x, y, row = 0.0, y + row, 0.0
            if y + h > 1:
                return None
            spots[i] = (x + pad, y + pad); x += w; row = max(row, h)
        return spots
    lo, hi = 0.0, 1.0 / max(max(w, h) for _, _, w, h in islands)
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if place(mid) else (lo, mid)
    spots = place(lo)
    out = [[(0.0, 0.0)] * len(P) for P, _, _ in prims]
    for i, (pi, pts, _, _) in enumerate(islands):
        ox, oy = spots[i]
        for v, (u, w) in pts.items():
            out[pi][v] = (ox + u * lo, oy + w * lo)
    return out


def surface_normals(P, idx, merge=False):
    """Per-vertex normal of the surface itself (area-weighted triangle normals, glTF counter-clockwise winding).
    merge=True shares one normal between vertices at the same position, so UV seams stay smooth."""
    key = (lambda v: tuple(round(c, 5) for c in P[v])) if merge else (lambda v: v)
    acc = defaultdict(lambda: [0.0, 0.0, 0.0])
    for f in range(0, len(idx), 3):
        i0, i1, i2 = idx[f:f + 3]
        fn = cross([P[i1][k] - P[i0][k] for k in range(3)], [P[i2][k] - P[i0][k] for k in range(3)])
        for v in (i0, i1, i2):
            s = acc[key(v)]
            for k in range(3): s[k] += fn[k]
    out = [None] * len(P)
    for v in set(idx):
        s = acc[key(v)]
        out[v] = normalize(s) if dot(s, s) > 1e-24 else None
    return out


def check_normals(prims, good=0.5, fits=0.8):
    """prims: [(P, N, idx)] of one mesh in glTF space. Shipped MKX meshes have vertex normals that follow the surface
    (average agreement 0.96 on Erron Black), and the character shaders light, shade and reflect with them. A model whose
    normals point elsewhere (for example written in a different axis frame than the positions) looks blotchy and shiny
    in game. Returns (fixed N lists, message or None): normals that already follow the surface are kept; normals that
    one axis turn or flip brings onto the surface are turned back (keeps hard edges); anything else is rebuilt from the
    surface."""
    import itertools
    S = [surface_normals(P, idx) for P, N, idx in prims]
    pairs = [(n, s) for (P, N, idx), Sp in zip(prims, S) for v, (n, s) in enumerate(zip(N, Sp)) if s is not None]
    if not pairs:
        return [N for _, N, _ in prims], None

    def turn(R, n):
        return tuple(R[r][0] * n[0] + R[r][1] * n[1] + R[r][2] * n[2] for r in range(3))

    def score(R):
        return sum(dot(normalize(turn(R, n)), s) for n, s in pairs) / len(pairs)
    ident = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    before = score(ident)
    if before >= good:
        return [N for _, N, _ in prims], None
    turns = [tuple(tuple(sg[r] if c == perm[r] else 0 for c in range(3)) for r in range(3))
             for perm in itertools.permutations(range(3)) for sg in itertools.product((1, -1), repeat=3)]
    best = max(turns, key=score)
    after = score(best)
    if after >= fits:
        return ([[turn(best, n) for n in N] for _, N, _ in prims],
                'the model\'s vertex normals point the wrong way compared to its surface (%d%% match); turned them back '
                '(%d%% match) so lighting and shine look right' % (round(max(before, 0) * 100), round(after * 100)))
    rebuilt = [surface_normals(P, idx, merge=True) for P, N, idx in prims]
    return ([[r if r is not None else n for r, n in zip(Rp, N)] for Rp, (_, N, _) in zip(rebuilt, prims)],
            'the model\'s vertex normals do not follow its surface (%d%% match); rebuilt smooth normals from the surface'
            % round(max(before, 0) * 100))


IMPORT_DEFAULTS =dict(uv1='transfer', dq='transfer', default_slot=None, joint_tolerance=0.5, force=False, keep_bounds=False)


def apply_mesh(p, mesh, model, log=print, **opts):
    """Replace LOD0 of SkeletalMesh `mesh` in the loaded package `p` with the rigged glTF `model` (in memory; call
    p.save() afterwards). Options: see IMPORT_DEFAULTS. Returns a one-line summary."""
    a = argparse.Namespace(**dict(IMPORT_DEFAULTS, **opts))
    a.mesh, a.model = mesh, model
    mi = p.find_export(a.mesh, 'SkeletalMesh'); orig = SkeletalMesh(p, mi)
    sk = Skeleton(p, orig.skeleton_export()); mats = orig.materials(); OL = orig.lod
    log('target %s: %d material slots, skeleton %d bones, original %d verts / %d tris' % (
        a.mesh, len(mats), len(sk.names), len(OL.positions), len(OL.indices) // 3))
    gl = GLTF(a.model); j = gl.j; G = gl.node_globals()
    skinned = [i for i, nd in enumerate(j['nodes']) if 'mesh' in nd and 'skin' in nd]
    if not skinned:
        raise MKXError('no skinned mesh in %s (parent the mesh to the armature with an Armature modifier and export skinning)' % a.model)
    # ---- second UV map (blood and damage): the model's own, a new packed one, or copied from the original
    prim_list = [(ni, k, pr) for ni in skinned for k, pr in enumerate(j['meshes'][j['nodes'][ni]['mesh']]['primitives'])]
    if a.uv1 == 'auto':
        a.uv1 = 'model' if all('TEXCOORD_1' in pr['attributes'] for _, _, pr in prim_list) else 'generate'
    generated = {}
    if a.uv1 == 'generate':
        packs = []
        for _, _, pr in prim_list:
            at = pr['attributes']; P = gl.accessor(at['POSITION'])
            packs.append((P, gl.accessor(at['TEXCOORD_0']) if 'TEXCOORD_0' in at else [(0.0, 0.0)] * len(P),
                          gl.accessor(pr['indices']) if 'indices' in pr else list(range(len(P)))))
        generated = {(ni, k): uv for (ni, k, _), uv in zip(prim_list, pack_uv2(packs))}
        log('made a new second UV map for blood and damage (every UV island packed without overlaps)')
    # ---- joint mapping + bind consistency
    gm_ref = [mat_mul(mat_mul(S_SWAP, sk.globals[b]), S_SWAP) for b in range(len(sk.names))]
    sections = defaultdict(lambda: dict(verts=[], tris=[]))
    worst = []
    for ni in skinned:
        nd = j['nodes'][ni]; skin = j['skins'][nd['skin']]
        jn = [j['nodes'][x].get('name', '') for x in skin['joints']]
        missing = [x for x in jn if x not in sk.index]
        if missing:
            raise MKXError('joints not in the target skeleton: %s' % ', '.join(missing[:20]))
        jb = [sk.index[x] for x in jn]
        ibm = gl.accessor(skin['inverseBindMatrices']) if 'inverseBindMatrices' in skin else [[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]] * len(jn)
        ibm = [[[m[c * 4 + r] for c in range(4)] for r in range(4)] for m in ibm]
        bindm = [mat_mul(G[x], ibm[k]) for k, x in enumerate(skin['joints'])]
        for k, x in enumerate(skin['joints']):
            ug = G[x]; rg = gm_ref[jb[k]]
            dist = math.sqrt(sum((ug[i][3] - rg[i][3]) ** 2 for i in range(3)))
            worst.append((dist, jn[k]))
        node_prims = j['meshes'][nd['mesh']]['primitives']
        fixed_normals, msg = [None] * len(node_prims), None
        if all('NORMAL' in pr['attributes'] for pr in node_prims):
            raw = []
            for pr in node_prims:
                Pn = gl.accessor(pr['attributes']['POSITION'])
                raw.append((Pn, gl.accessor(pr['attributes']['NORMAL']),
                            gl.accessor(pr['indices']) if 'indices' in pr else list(range(len(Pn)))))
            fixed_normals, msg = check_normals(raw)
            if msg:      # the exporter built the model's tangents around the wrong normals: rebuild them from the UVs
                log(msg + '; rebuilt the tangents (normal map direction) from the UV map')
        for pk, prim in enumerate(node_prims):
            if prim.get('mode', 4) != 4:
                raise MKXError('only triangle primitives are supported')
            at = prim['attributes']
            for need in ('POSITION', 'JOINTS_0', 'WEIGHTS_0'):
                if need not in at:
                    raise MKXError('primitive lacks %s' % need)
            mname = j['materials'][prim['material']].get('name', '') if 'material' in prim else ''
            slot = material_slot(mname, mats)
            if slot is None:
                slot = a.default_slot
            if slot is None or not (0 <= slot < len(mats)):
                raise MKXError('material %r does not map to a slot. Name materials "slotNN_..." (NN = 0..%d) or use --default-slot.\n  slots: %s'
                                 % (mname, len(mats) - 1, ', '.join('%d=%s' % (i, x.split('.')[-1]) for i, x in enumerate(mats))))
            P = gl.accessor(at['POSITION']); N = fixed_normals[pk]
            T = gl.accessor(at['TANGENT']) if 'TANGENT' in at and not msg else None
            UV0 = gl.accessor(at['TEXCOORD_0']) if 'TEXCOORD_0' in at else [(0.0, 0.0)] * len(P)
            UV1 = generated[(ni, pk)] if generated else (gl.accessor(at['TEXCOORD_1']) if 'TEXCOORD_1' in at else None)
            infl = defaultdict(float)
            J0, W0 = gl.accessor(at['JOINTS_0']), gl.accessor(at['WEIGHTS_0'])
            J1 = gl.accessor(at['JOINTS_1']) if 'JOINTS_1' in at else None
            W1 = gl.accessor(at['WEIGHTS_1']) if 'WEIGHTS_1' in at else None
            idx = gl.accessor(prim['indices']) if 'indices' in prim else list(range(len(P)))
            if not idx or len(idx) % 3 or min(idx) < 0 or max(idx) >= len(P):
                raise MKXError('primitive has empty or invalid triangle indices')
            if UV1 is None and a.uv1 == 'model':
                raise MKXError('Use model UV1 was selected, but this primitive has no second UV map')
            for values in (P, N, T, UV0, UV1, J0, W0, J1, W1):
                if values is not None and len(values) != len(P):
                    raise MKXError('vertex attribute counts do not match POSITION')
            if any(not math.isfinite(x) for values in (P, N, T, UV0, UV1, W0, W1) if values is not None for row in values for x in row):
                raise MKXError('model contains NaN or infinite vertex data')
            if any(x < 0 for values in (W0, W1) if values is not None for row in values for x in row):
                raise MKXError('skin weights cannot be negative')
            if any(x < 0 or x >= len(jn) for values in (J0, J1) if values is not None for row in values for x in row):
                raise MKXError('vertex joint index is outside the skin joint list')
            sec = sections[slot]; base = len(sec['verts'])
            used = sorted(set(idx))                    # compact: primitives may share one big vertex array
            local = {v: k for k, v in enumerate(used)}
            for v in used:
                inf = defaultdict(float)
                for js, ws in ((J0, W0), (J1, W1)):
                    if js is None: continue
                    for k in range(4):
                        if ws[v][k] > 0: inf[js[v][k]] += ws[v][k]
                if not inf:
                    raise MKXError('vertex %d of material %r has no skin weights' % (v, mname))
                tot = sum(inf.values())
                M = [[0.0] * 4 for _ in range(4)]
                for jj, ww in inf.items():
                    bm = bindm[jj]
                    for r in range(3):
                        for c in range(4): M[r][c] += bm[r][c] * ww / tot
                pg = xform_point(M, P[v])
                ng = normalize(xform_dir(M, N[v])) if N else None
                tg = (normalize(xform_dir(M, T[v][:3])), T[v][3]) if T else None
                sec['verts'].append(dict(p=ue2g(pg), n=ue2g(ng) if ng else None, t=(ue2g(tg[0]), tg[1], ng) if tg else None,
                                         uv0=tuple(UV0[v][:2]), uv1=tuple(UV1[v][:2]) if UV1 else None,
                                         inf=sorted(((ww / tot, jb[jj]) for jj, ww in inf.items()), reverse=True)))
            sec['tris'].extend(base + local[x] for x in idx)
    worst.sort(reverse=True)
    if worst and worst[0][0] > a.joint_tolerance:
        msg = 'joint head positions differ from the target skeleton (worst: %s)' % ', '.join('%s %.2f' % (nm, dd) for dd, nm in worst[:6])
        if not a.force:
            raise MKXError('%s\n  Keep the reference armature unmodified (rest pose, transforms, scale). Use --force to ignore.' % msg)
        log('WARNING: ' + msg)
    # ---- build LOD
    allv = sum(len(s['verts']) for s in sections.values())
    if not allv:
        raise MKXError('the rigged model contains no triangle geometry')
    if allv > 65535:
        raise MKXError('%d vertices; MKX skeletal meshes use 16-bit indices here (max 65535). Decimate or split UV seams less.' % allv)
    ref = NearestGrid(OL.positions)
    geo = dict(positions=[], tangents=[], influences=[], uv0=[], uv1=[], dq=[], indices=[], sections=[], chunks=[])
    actives = []
    far = 0
    for ci, slot in enumerate(sorted(sections)):
        sec = sections[slot]; V = sec['verts']
        # quantize influences (max 4, bytes summing to 255, descending)
        q = []
        for vv in V:
            inf = vv['inf'][:4]; tot = sum(w for w, _ in inf)
            ws = [int(round(w / tot * 255)) for w, _ in inf]
            ws[0] += 255 - sum(ws)
            pairs = [(w, b) for w, b in zip(ws, (b for _, b in inf)) if w > 0]
            pairs.sort(reverse=True)
            q.append(pairs)
        order = sorted(range(len(V)), key=lambda i: (len(q[i]) > 1, i))   # rigid first, then soft
        remap = {old: new for new, old in enumerate(order)}
        bonemap = []
        for i in order:
            for _, b in q[i]:
                if b not in bonemap: bonemap.append(b)
        for b in bonemap:
            if b not in actives: actives.append(b)
        base_v = len(geo['positions'])
        nrigid = sum(1 for i in order if len(q[i]) == 1)
        # UV-space tangent frame per vertex: used for TangentX when the model has no TANGENT attribute, and always for
        # the bitangent sign (MKX convention measured on shipped character data: TangentZ.W=255 when dP/dv agrees with N x T in UE space)
        acc_t = [[0.0, 0.0, 0.0] for _ in V]; acc_b = [[0.0, 0.0, 0.0] for _ in V]
        if True:
            for f in range(0, len(sec['tris']), 3):
                i0, i1, i2 = sec['tris'][f:f + 3]
                p0, p1, p2 = V[i0]['p'], V[i1]['p'], V[i2]['p']
                u0, u1, u2 = V[i0]['uv0'], V[i1]['uv0'], V[i2]['uv0']
                e1 = [p1[k] - p0[k] for k in range(3)]; e2 = [p2[k] - p0[k] for k in range(3)]
                du1, dv1, du2, dv2 = u1[0] - u0[0], u1[1] - u0[1], u2[0] - u0[0], u2[1] - u0[1]
                det = du1 * dv2 - du2 * dv1
                if abs(det) < 1e-12: continue
                tt = [(e1[k] * dv2 - e2[k] * dv1) / det for k in range(3)]
                bb = [(e2[k] * du1 - e1[k] * du2) / det for k in range(3)]
                for i in (i0, i1, i2):
                    for k in range(3): acc_t[i][k] += tt[k]; acc_b[i][k] += bb[k]
        for i in order:
            vv = V[i]; pos = vv['p']
            nrm = vv['n']
            if nrm is None:
                raise MKXError('model has no normals; enable Normals in the glTF exporter')
            if vv['t'] is not None:
                tx = vv['t'][0]
            else:
                tx = acc_t[i]; tx = [tx[k] - dot(nrm, tx) * nrm[k] for k in range(3)]
                tx = normalize(tx) if dot(tx, tx) > 1e-12 else normalize(cross(nrm, (0, 0, 1)) if abs(nrm[2]) < 0.9 else cross(nrm, (1, 0, 0)))
            tx = normalize([tx[k] - dot(nrm, tx) * nrm[k] for k in range(3)])
            sb = dot(acc_b[i], cross(nrm, tx))
            if abs(sb) > 1e-12 or vv['t'] is None:
                sign = sb > 0
            else:  # degenerate UVs: fall back to the glTF handedness (B = w * N x T in glTF space, mapped to UE)
                bg = tuple(vv['t'][1] * c for c in cross(ue2g(nrm), ue2g(tx)))
                sign = dot(ue2g(bg), cross(nrm, tx)) > 0
            geo['tangents'].append(pack_normal(tx, 0) + pack_normal(nrm, 255 if sign else 0))
            geo['positions'].append(pos)
            pairs = q[i]
            li = [bonemap.index(b) for _, b in pairs] + [0] * (4 - len(pairs))
            ws = [w for w, _ in pairs] + [0] * (4 - len(pairs))
            geo['influences'].append(pack_influence(li, ws))
            geo['uv0'].append(vv['uv0'])
            k, dist = ref.nearest(pos)
            far = max(far, dist)
            geo['uv1'].append(vv['uv1'] if (vv['uv1'] is not None and a.uv1 in ('model', 'generate')) else (OL.uv(k, 1) if a.uv1 == 'transfer' else vv['uv0']))
            if OL.dq is not None:
                geo['dq'].append({'transfer': OL.dq[k], 'zero': 0.0, 'half': 0.5}[a.dq])
        geo['chunks'].append(dict(base=base_v, bonemap=bonemap, rigid=nrigid, soft=len(V) - nrigid,
                                  maxinf=max(len(x) for x in q)))
        tri_base = len(geo['indices'])
        geo['indices'].extend(base_v + remap[x] for x in sec['tris'])
        geo['sections'].append(dict(mat=slot, chunk=ci, base=tri_base, tris=len(sec['tris']) // 3))
    geo['active_bones'] = actives
    if OL.dq is None:
        geo['dq'] = None
    geo['required_bones'] = OL.required_bones
    geo['size_field'] = OL.size_field
    geo['adjacency'] = build_adjacency(geo['indices'], geo['positions'], geo['uv0'])
    # ---- serialize export and append it to the package
    d = p.image
    props = bytearray(d[orig.base:orig.base + orig.props_len])
    if 'Bounds' in orig.pm and not a.keep_bounds:
        P = geo['positions']
        mn = [min(x[k] for x in P) for k in range(3)]; mx = [max(x[k] for x in P) for k in range(3)]
        org = [(mn[k] + mx[k]) / 2 for k in range(3)]; ext = [(mx[k] - mn[k]) / 2 for k in range(3)]
        rad = max(math.sqrt(sum((x[k] - org[k]) ** 2 for k in range(3))) for x in P)
        struct.pack_into('<7f', props, orig.pm['Bounds'][5] - orig.base, *(org + ext + [rad]))
    new_off = len(d)
    head = bytes(props) + struct.pack('<II', 1, orig.lod_index)
    lod = build_lod_bytes(geo, new_off + len(head))
    blob = head + lod + orig.tail
    p.append_export_data(mi, blob)
    new = SkeletalMesh(p, mi)                      # self-check: re-parse what we just wrote
    assert len(new.lod.positions) == len(geo['positions']) and new.tail == orig.tail
    summary = ('new LOD0: %d verts (%d rigid), %d tris, %d sections (slots %s), %d active bones; max distance to nearest original vertex %.1f'
               % (len(geo['positions']), sum(c['rigid'] for c in geo['chunks']), len(geo['indices']) // 3, len(geo['sections']),
                  ','.join(str(s['mat']) for s in geo['sections']), len(actives), far))
    log(summary)
    if a.uv1 == 'transfer' and far > 2.0:
        log('WARNING: the second UV map (blood and damage) was copied from original vertices up to %.1f units away, so blood '
            'will land in the wrong places. Let the converter make a new one instead.' % far)
    return summary


def cmd_import_mesh(a):
    p = load(a.package)
    apply_mesh(p, a.mesh, a.model, uv1=a.uv1, dq=a.dq, default_slot=a.default_slot, joint_tolerance=a.joint_tolerance,
               force=a.force, keep_bounds=a.keep_bounds)
    size = p.save(a.out, a.compression)
    print('wrote %s (%d bytes, %s)' % (a.out, size, a.compression))


def read_texture(p, idx):
    d = p.image; e = p.exports[idx - 1]
    props, end = parse_props(p, d, e['SerialOffset']); pm = prop_map(props)
    info = dict(srgb=pm['SRGB'][2] if 'SRGB' in pm else 0, fmt=d[pm['Format'][5]] if 'Format' in pm else None, mips=[])
    r = Reader(d, end)
    for _ in range(2):
        fl, cnt, sz, off = r.u32(), r.u32(), r.u64(), r.u64()
        if not fl & 1: r.o += sz
    r.u32(); r.u32(); n = r.u32(); r.o += 4 * n
    for _ in range(r.u32()):
        fl, cnt, sz, off = r.u32(), r.u32(), r.u64(), r.u64()
        dp = None
        if not fl & 1:
            dp = r.o; r.o += sz
        info['mips'].append(dict(flags=fl, size=sz, data_pos=dp, w=r.u32(), h=r.u32()))
    return info


def dds_bytes(w, h, mips, srgb):
    hdr = struct.pack('<4sIIIIIII', b'DDS ', 124, 0xA1007, h, w, len(mips[0]), 0, len(mips)) + b'\0' * 44
    hdr += struct.pack('<II4sIIIII', 32, 4, b'DX10', 0, 0, 0, 0, 0) + struct.pack('<IIIII', 0x401008, 0, 0, 0, 0)
    hdr += struct.pack('<IIIII', 99 if srgb else 98, 3, 0, 1, 0)
    return hdr + b''.join(mips)


def export_textures(p, outdir, only=None, log=print):
    """Write every inline BC7 texture (or only those in `only`) as .dds (+ .png when Pillow is available)."""
    os.makedirs(outdir, exist_ok=True)
    written = []
    for i, e in enumerate(p.exports):
        if p.classname(e['Class']) != 'Texture2D': continue
        if only is not None and p.objref(i + 1) not in only: continue
        t = read_texture(p, i + 1)
        if t['fmt'] != 22 or any(m['data_pos'] is None for m in t['mips']):
            log('skip %s (streamed from .tfc or not BC7)' % p.objref(i + 1)); continue
        mips = [bytes(p.image[m['data_pos']:m['data_pos'] + m['size']]) for m in t['mips']]
        out = os.path.join(outdir, p.objref(i + 1).split('.')[-1] + '.dds')
        open(out, 'wb').write(dds_bytes(t['mips'][0]['w'], t['mips'][0]['h'], mips, t['srgb']))
        msg = ''
        try:
            from PIL import Image
            im = Image.open(out); im.load(); im.save(out[:-4] + '.png'); msg = ' (+png)'
        except Exception as exc:
            log('WARNING: PNG preview unavailable for %s: %s (DDS was exported)' % (out, exc))
        written.append(out)
        log('wrote %s %dx%d, %d mips, %s%s' % (out, t['mips'][0]['w'], t['mips'][0]['h'], len(mips), 'sRGB' if t['srgb'] else 'linear', msg))
    return written


def cmd_export_textures(a):
    export_textures(load(a.package), a.outdir)


def cmd_import_texture(a):
    p = load(a.package); ti = p.find_export(a.texture, 'Texture2D'); t = read_texture(p, ti)
    d = open(a.dds, 'rb').read()
    if d[:4] != b'DDS ' or d[84:88] != b'DX10':
        raise MKXError('DDS must be BC7 with a DX10 header (texconv -f BC7_UNORM_SRGB / BC7_UNORM)')
    h, w = struct.unpack_from('<II', d, 12); fmt = struct.unpack_from('<I', d, 128)[0]
    if fmt not in (98, 99):
        raise MKXError('DDS format %d is not BC7' % fmt)
    if (w, h) != (t['mips'][0]['w'], t['mips'][0]['h']):
        raise MKXError('DDS is %dx%d; %s is %dx%d (same size required for in-place replacement)'
                         % (w, h, a.texture, t['mips'][0]['w'], t['mips'][0]['h']))
    off = 148
    for k, m in enumerate(t['mips']):
        if m['data_pos'] is None:
            raise MKXError('mip %d is streamed from a .tfc (not supported)' % k)
        if off + m['size'] > len(d):
            raise MKXError('DDS has too few mip levels (need %d)' % len(t['mips']))
        p.image[m['data_pos']:m['data_pos'] + m['size']] = d[off:off + m['size']]; off += m['size']
    if (fmt == 99) != bool(t['srgb']):
        print('WARNING: DDS %s but texture is %s; BC7 blocks are identical, colour interpretation follows the game (%s)'
              % ('sRGB' if fmt == 99 else 'linear', 'sRGB' if t['srgb'] else 'linear', 'sRGB' if t['srgb'] else 'linear'))
    size = p.save(a.out, a.compression)
    print('replaced %d mips of %s; wrote %s (%d bytes)' % (len(t['mips']), a.texture, a.out, size))


# ----------------------------------------------------------------------------------------------- BC7 encoder (mode 6)
BC7_W4 = (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64)


def bc7_encode(rgba, _dedupe=True):
    """Encode an (H, W, 4) uint8 numpy array (H, W multiples of 4) to BC7 using mode 6 (one subset, RGBA 7.7.7.7 +
    p-bit endpoints, 4-bit indices). PCA endpoints, best p-bit pair per block. Needs numpy."""
    import numpy as np
    h, w = rgba.shape[:2]
    blocks = rgba.reshape(h // 4, 4, w // 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 4)
    if _dedupe and blocks.shape[0] > 1 and np.all(blocks == blocks[0, 0]):   # constant image: encode one block, repeat
        return bc7_encode(np.ascontiguousarray(rgba[:4, :4]), False) * blocks.shape[0]
    Wt = np.array(BC7_W4, np.int64)
    out = []
    for s in range(0, blocks.shape[0], 4096):
        px = blocks[s:s + 4096].astype(np.float64); n = px.shape[0]
        mean = px.mean(1)
        dlt = px - mean[:, None, :]
        cov = np.einsum('npi,npj->nij', dlt, dlt)
        v = np.full((n, 4), 0.5)
        for _ in range(8):
            v = np.einsum('nij,nj->ni', cov, v)
            nv = np.linalg.norm(v, axis=1, keepdims=True)
            v = np.where(nv > 1e-9, v / np.maximum(nv, 1e-12), 0.5)
        t = np.einsum('npi,ni->np', dlt, v)
        e = [mean + t.min(1)[:, None] * v, mean + t.max(1)[:, None] * v]
        best_err = None
        for p0 in (0, 1):
            for p1 in (0, 1):
                q0 = np.clip(np.rint((e[0] - p0) / 2), 0, 127).astype(np.int64)
                q1 = np.clip(np.rint((e[1] - p1) / 2), 0, 127).astype(np.int64)
                c0, c1 = q0 * 2 + p0, q1 * 2 + p1
                pal = ((64 - Wt)[None, :, None] * c0[:, None, :] + Wt[None, :, None] * c1[:, None, :] + 32) >> 6
                dist = ((px[:, :, None, :] - pal[:, None, :, :]) ** 2).sum(-1)
                idx = dist.argmin(-1); err = dist.min(-1).sum(-1)
                if best_err is None:
                    best = [q0, q1, np.full(n, p0), np.full(n, p1), idx]; best_err = err
                else:
                    m = err < best_err; best_err = np.where(m, err, best_err)
                    for k, val in enumerate((q0, q1, np.full(n, p0), np.full(n, p1), idx)):
                        best[k] = np.where(m[:, None] if val.ndim == 2 else m, val, best[k])
        q0, q1, p0, p1, idx = best
        sw = idx[:, 0] >= 8                                  # anchor index must have its MSB clear
        q0, q1 = np.where(sw[:, None], q1, q0), np.where(sw[:, None], q0, q1)
        p0, p1 = np.where(sw, p1, p0), np.where(sw, p0, p1)
        idx = np.where(sw[:, None], 15 - idx, idx)
        lo = np.zeros(n, np.uint64); hi = np.zeros(n, np.uint64)

        def put(val, pos, width):
            nonlocal lo, hi
            val = val.astype(np.uint64) & np.uint64((1 << width) - 1)
            if pos + width <= 64:
                lo |= val << np.uint64(pos)
            elif pos >= 64:
                hi |= val << np.uint64(pos - 64)
            else:
                lo |= (val << np.uint64(pos)) & np.uint64(0xFFFFFFFFFFFFFFFF)
                hi |= val >> np.uint64(64 - pos)
        put(np.full(n, 64), 0, 7)                            # mode 6 marker: 0000001
        pos = 7
        for ch in range(4):
            put(q0[:, ch], pos, 7); put(q1[:, ch], pos + 7, 7); pos += 14
        put(p0, 63, 1); put(p1, 64, 1)
        put(idx[:, 0], 65, 3)
        for k in range(1, 16):
            put(idx[:, k], 68 + 4 * (k - 1), 4)
        out.append(np.stack([lo, hi], 1).astype('<u8').tobytes())
    return b''.join(out)


def resize_channels(im, size, method):
    """Resize each RGBA channel independently. Pillow premultiplies RGBA by alpha when resampling, which zeroes the
    colour wherever alpha is 0; mask textures (Pmsk) store unrelated data in each channel, so that would destroy it."""
    from PIL import Image
    return Image.merge('RGBA', [band.resize(size, method) for band in im.split()])


def image_mips(src, w, h, count):
    """Build `count` RGBA mip levels (numpy arrays) from a PIL image or solid colour, top level resized to w x h."""
    import numpy as np
    from PIL import Image
    if isinstance(src, tuple):
        base = Image.new('RGBA', (w, h), src)
    else:
        base = src.convert('RGBA')
        if base.size != (w, h):
            base = resize_channels(base, (w, h), Image.LANCZOS)
    mips = []
    for k in range(count):
        mw, mh = max(1, w >> k), max(1, h >> k)
        im = base if k == 0 else resize_channels(base, (mw, mh), Image.BOX)
        arr = np.asarray(im, dtype=np.uint8)
        if mw < 4 or mh < 4:                                 # pad tiny mips to one block
            arr = np.pad(arr, ((0, max(0, 4 - mh)), (0, max(0, 4 - mw)), (0, 0)), mode='edge')
        mips.append(np.ascontiguousarray(arr))
    return mips


def pow2_size(w, h, lo=4, hi=8192):
    """Nearest power-of-two size (each side 4..8192), as game textures use for their mip chains."""
    near = lambda x: min(hi, max(lo, 2 ** round(math.log2(max(x, 1)))))
    return near(w), near(h)


def rewrite_texture(p, ti, w, h, datas):
    """Store new BC7 mips (largest first, w x h) for Texture2D export `ti`, changing its size: the export is rebuilt
    with SizeX/SizeY/MipTailBaseIdx updated and every mip stored inline, then appended to the package."""
    d = p.image; e = p.exports[ti - 1]; base, end = e['SerialOffset'], e['SerialOffset'] + e['SerialSize']
    props, pend = parse_props(p, d, base, end)
    blob = bytearray(d[base:pend]); pm = prop_map(props)
    for name, value in (('SizeX', w), ('SizeY', h), ('MipTailBaseIdx', len(datas) - 1)):
        if name in pm:
            struct.pack_into('<i', blob, pm[name][5] - base, value)
    r = Reader(d, pend); pre = []
    for _ in range(2):
        fl, cnt, sz, off = r.u32(), r.u32(), r.u64(), r.u64()
        payload = None
        if not fl & 1:
            payload = bytes(d[r.o:r.o + sz]); r.o += sz
        pre.append((fl, cnt, sz, off, payload))
    mid = [r.u32(), r.u32()]; n = r.u32(); arr = bytes(d[r.o:r.o + 4 * n]); r.o += 4 * n
    for _ in range(r.u32()):
        fl, cnt, sz, off = r.u32(), r.u32(), r.u64(), r.u64()
        if not fl & 1: r.o += sz
        r.u32(); r.u32()
    tail = bytes(d[r.o:end])
    new_off = len(d)
    out = Writer(); out.raw(bytes(blob))
    for fl, cnt, sz, off, payload in pre:
        out.u32(fl); out.u32(cnt)
        if payload is None:
            out.u64(sz); out.u64(off)
        else:
            out.u64(len(payload)); out.u64(new_off + len(out) + 8); out.raw(payload)
    out.u32(mid[0]); out.u32(mid[1]); out.u32(n); out.raw(arr)
    out.u32(len(datas))
    for k, data in enumerate(datas):
        out.u32(0); out.u32(len(data)); out.u64(len(data)); out.u64(new_off + len(out) + 8); out.raw(data)
        out.u32(max(1, w >> k)); out.u32(max(1, h >> k))
    out.raw(tail)
    p.append_export_data(ti, bytes(out.b))
    p.image[base:end] = bytes(end - base)          # the old data is no longer referenced; zeros compress to almost nothing


def apply_image(p, texture, image, opaque=False, dds_out=None, log=print, keep_size=False):
    """Resize `image` (path to PNG/TGA/JPG/..., a PIL image, or 'solid:R,G,B,A') to the texture's size, build its mip
    chain, BC7-encode it and write it over the inline Texture2D `texture` in the loaded package (in memory)."""
    ti = p.find_export(texture, 'Texture2D'); t = read_texture(p, ti)
    if t['fmt'] != 22:
        raise MKXError('%s is not BC7' % texture)
    if any(m['data_pos'] is None for m in t['mips']):
        raise MKXError('%s streams mips from a .tfc (not supported)' % texture)
    try:
        import numpy  # noqa: F401
        from PIL import Image
    except ImportError:
        raise MKXError('texture conversion needs numpy and Pillow:  python -m pip install numpy pillow')
    w, h = t['mips'][0]['w'], t['mips'][0]['h']
    label = image if isinstance(image, str) else 'image'
    if isinstance(image, str) and image.lower().startswith('solid:'):
        src = tuple(int(x) for x in image[6:].split(','))
        if len(src) != 4:
            raise MKXError('use solid:R,G,B,A')
    else:
        src = Image.open(image) if isinstance(image, str) else image
        if keep_size and src.size != (w, h):                    # experimental: the texture takes the image's size
            nw, nh = pow2_size(*src.size)
            count = int(math.log2(min(nw, nh))) - 1             # down to a 4-pixel side, as the game's textures do
            if (nw, nh) != src.size:
                log('keeping %s near its own size: %dx%d -> %dx%d (textures need power-of-two sides)'
                    % (os.path.basename(label), src.size[0], src.size[1], nw, nh))
            if opaque:
                src = src.convert('RGBA'); src.putalpha(255)
            datas = [bc7_encode(arr) for arr in image_mips(src, nw, nh, count)]
            rewrite_texture(p, ti, nw, nh, datas)
            if dds_out:
                open(dds_out, 'wb').write(dds_bytes(nw, nh, datas, t['srgb']))
            log('encoded %s -> %s at its own size (%dx%d, %d mips, BC7; original was %dx%d) [experimental]'
                % (os.path.basename(label), texture, nw, nh, count, w, h))
            return
        if src.size != (w, h):
            log('resizing %s %dx%d -> %dx%d' % (os.path.basename(label), src.size[0], src.size[1], w, h))
        if opaque:
            src = src.convert('RGBA'); src.putalpha(255)
    mips = image_mips(src, w, h, len(t['mips']))
    datas = []
    for k, (m, arr) in enumerate(zip(t['mips'], mips)):
        enc = bc7_encode(arr)
        if len(enc) != m['size']:
            raise MKXError('mip %d encoded to %d bytes, expected %d' % (k, len(enc), m['size']))
        datas.append(enc)
    for m, enc in zip(t['mips'], datas):
        p.image[m['data_pos']:m['data_pos'] + m['size']] = enc
    if dds_out:
        open(dds_out, 'wb').write(dds_bytes(w, h, datas, t['srgb']))
    log('encoded %s -> %s (%dx%d, %d mips, BC7)' % (os.path.basename(label), texture, w, h, len(datas)))


def cmd_import_image(a):
    p = load(a.package)
    apply_image(p, a.texture, a.image, opaque=a.opaque, dds_out=a.dds_out)
    size = p.save(a.out, a.compression)
    print('wrote %s (%d bytes)' % (a.out, size))


def cmd_verify(a):
    return 0 if verify_package(load(a.package)) else 1


def verify_package(p, log=print):
    """Re-parse every SkeletalMesh and inline texture and check internal consistency. Returns True when all OK."""
    ok = True
    print = log  # noqa: A001  (route the messages below through the caller's logger)
    for i, e in enumerate(p.exports):
        cn = p.classname(e['Class'])
        try:
            if cn == 'SkeletalMesh':
                m = SkeletalMesh(p, i + 1); L = m.lod; nv = len(L.positions)
                assert len(L.tangents) == len(L.influences) == nv == L.num_verts and (L.dq is None or len(L.dq) == nv)
                assert max(L.indices) < nv and len(L.adjacency) == 4 * len(L.indices)
                copies = [2 if s['sort'] == TRISORT_CUSTOM_LEFT_RIGHT else 1 for s in L.sections]
                assert sum(s['tris'] * k for s, k in zip(L.sections, copies)) * 3 == len(L.indices), 'section triangle count'
                assert len(L.prob) == len(L.alias) in (0, L.num_tris()), 'triangle sampling table size'
                assert all(0 <= a < len(L.alias) for a in L.alias), 'triangle sampling alias index'
                for c in L.chunks:
                    for v in range(c['base'], c['base'] + c['rigid'] + c['soft']):
                        idx, w = unpack_influence(L.influences[v])
                        assert min(w) >= 0 and all(idx[k] < len(c['bonemap']) for k in range(4) if w[k])
                for fl, cnt, sz, off, pos in L.bulk:
                    assert off == pos, 'bulk offset %x != %x' % (off, pos)
                mats = m.materials()
                assert all(s['mat'] < len(mats) for s in L.sections)
                print('OK   SkeletalMesh %-60s %6d verts %6d tris %2d sections' % (p.objref(i + 1), nv, L.num_tris(), len(L.sections)))
            elif cn == 'Texture2D':
                t = read_texture(p, i + 1)
                for m in t['mips']:
                    if m['data_pos'] is not None:
                        assert struct.unpack_from('<Q', p.image, m['data_pos'] - 8)[0] == m['data_pos']
        except Exception as ex:
            ok = False; print('FAIL %s %s: %s' % (cn, p.objref(i + 1), ex))
    print('verify: ' + ('all OK' if ok else 'FAILURES'))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('info'); s.add_argument('package'); s.add_argument('--all', action='store_true'); s.set_defaults(f=cmd_info)
    s = sp.add_parser('export-ref'); s.add_argument('package'); s.add_argument('mesh'); s.add_argument('out')
    s.add_argument('--embed-textures', action='store_true', help='embed each material\'s diffuse texture in the .glb')
    s.set_defaults(f=cmd_export_ref)
    s = sp.add_parser('export-textures'); s.add_argument('package'); s.add_argument('outdir'); s.set_defaults(f=cmd_export_textures)
    s = sp.add_parser('import-mesh'); s.add_argument('package'); s.add_argument('mesh'); s.add_argument('model'); s.add_argument('out')
    s.add_argument('--uv1', choices=['transfer', 'model', 'uv0', 'generate', 'auto'], default='transfer',
                   help='second UV set: copy from nearest original vertex (default), use the model\'s TEXCOORD_1, or duplicate UV0')
    s.add_argument('--dq', choices=['transfer', 'zero', 'half'], default='transfer',
                   help='dual-quaternion blend weight per vertex: nearest original vertex (default), 0 (linear) or 0.5')
    s.add_argument('--default-slot', type=int, default=None, help='material slot for materials without a slotNN name')
    s.add_argument('--joint-tolerance', type=float, default=0.5, help='max joint position difference vs target skeleton (units)')
    s.add_argument('--force', action='store_true'); s.add_argument('--keep-bounds', action='store_true')
    s.add_argument('--compression', choices=['zlib', 'none'], default='zlib'); s.set_defaults(f=cmd_import_mesh)
    s = sp.add_parser('import-texture'); s.add_argument('package'); s.add_argument('texture'); s.add_argument('dds'); s.add_argument('out')
    s.add_argument('--compression', choices=['zlib', 'none'], default='zlib'); s.set_defaults(f=cmd_import_texture)
    s = sp.add_parser('import-image', help='PNG/TGA/... or solid:R,G,B,A -> resized, mipped, BC7-encoded, injected')
    s.add_argument('package'); s.add_argument('texture'); s.add_argument('image'); s.add_argument('out')
    s.add_argument('--opaque', action='store_true', help='force alpha to 255 (DIFF alpha is ambient occlusion in MKX)')
    s.add_argument('--dds-out', default=None, help='also save the encoded texture as .dds')
    s.add_argument('--compression', choices=['zlib', 'none'], default='zlib'); s.set_defaults(f=cmd_import_image)
    s = sp.add_parser('verify'); s.add_argument('package'); s.set_defaults(f=cmd_verify)
    a = ap.parse_args()
    try:
        sys.exit(a.f(a) or 0)
    except MKXError as ex:
        print('ERROR: %s' % ex)
        sys.exit(1)


if __name__ == '__main__':
    raise SystemExit('mkx_meshmod.py is the MKX Character Studio backend. Run launcher.py or studio_cli.py.')
