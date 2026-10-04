import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import studio_core as core


class WorkspaceTests(unittest.TestCase):
    def test_game_and_original_tool_are_protected(self):
        # Synthetic folders allow contributors to run this test without owning game files.
        with tempfile.TemporaryDirectory() as temp:
            game = Path(temp) / 'MK10'
            (game / 'Asset').mkdir(parents=True)
            (game / 'Binaries').mkdir()
            with patch.object(core, 'detect_game_dir', return_value=str(game)):
                for path in [game, game / 'Asset' / 'new', game / 'Binaries', game / 'ModTool']:
                    with self.subTest(path=path), self.assertRaises(core.mk.MKXError):
                        core.setup_workspace(str(path), str(game))
                core.guard_output(core.HERE / 'projects' / 'test', str(game))

    def test_traversal_and_reserved_names(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ('..', '../escape', str(Path(root).parent)):
                with self.subTest(name=name), self.assertRaises(core.mk.MKXError):
                    core.child(root, name)
        for name in ('..', 'CON', 'NUL.txt'):
            with self.assertRaises(core.mk.MKXError): core.safe_name(name)
        for name in ('../CHAR_A.xxx', 'C:\\CHAR_A.xxx', 'a.xxx:stream'):
            with self.assertRaises(core.mk.MKXError): core.package_name(name)

    def test_transaction_failure_and_repeat_preserve_results(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(RuntimeError):
                with core.staged_output(ws, '', 'converted', 'build') as (stage, final):
                    (stage / 'test').write_text('partial')
                    raise RuntimeError('conversion failed')
            self.assertEqual(list((Path(ws) / 'converted').glob('build*')), [])
            with core.staged_output(ws, '', 'converted', 'build') as (stage, first):
                (stage / 'test').write_text('original')
            with core.staged_output(ws, '', 'converted', 'build') as (stage, second):
                (stage / 'test').write_text('new')
            self.assertNotEqual(first, second)
            self.assertEqual((first / 'test').read_text(), 'original')

    def test_cache_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            core.setup_workspace(ws)
            cache = Path(ws) / 'vanilla_cache' / 'CHAR_Test.xxx'
            cache.write_bytes(b'original')
            cache.with_suffix('.xxx.json').write_text(json.dumps({'sha256': core.sha256(cache)}))
            self.assertEqual(core.template_path(ws, '', cache.name), str(cache))
            cache.write_bytes(b'modified')
            with self.assertRaises(core.mk.MKXError): core.template_path(ws, '', cache.name)

    def test_shared_texture_ambiguity_keeps_original(self):
        slots = [{'texture': 'Textures.Diff', 'param': 'DiffuseMap', 'slots': [0, 1], 'replaceable': True}]
        result = core.guess_textures(slots, ['MyChar/diff_1.png', 'MyChar/diff_2.png', 'Other/diff.png'], 'MyChar.glb')
        self.assertEqual(result['Textures.Diff'], core.KEEP)
        result = core.guess_textures(slots, ['MyChar/diff.png', 'Other/diff.png'], 'MyChar.glb')
        self.assertEqual(result['Textures.Diff'], 'MyChar/diff.png')

    def test_hardlink_output_is_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            a, b = Path(root) / 'a', Path(root) / 'b'
            a.write_text('original'); os.link(a, b)
            with self.assertRaises(core.mk.MKXError): core.guard_output(b)

    def test_body_texture_is_not_guessed_for_eyes(self):
        slots = [{'texture': 'body', 'param': 'DiffuseMap', 'slots': [0, 1, 2], 'replaceable': True},
                 {'texture': 'eyes', 'param': 'DiffuseMap', 'slots': [3], 'replaceable': True}]
        result = core.guess_textures(slots, ['MyChar/diff.png'], 'MyChar.glb')
        self.assertEqual(result, {'body': 'MyChar/diff.png', 'eyes': core.KEEP})


class LzoTests(unittest.TestCase):
    """Hand-built LZO1X streams (no game data)."""

    def test_literals_only(self):
        from lzo1x import lzo1x_decompress
        stream = bytes([17 + 5]) + b'hello' + b'\x11\x00\x00'
        self.assertEqual(lzo1x_decompress(stream, 5), b'hello')

    def test_overlapping_match(self):
        from lzo1x import lzo1x_decompress
        # 3 literals, then a match 3 bytes back of length 6, then the end marker.
        stream = bytes([17 + 3]) + b'abc' + bytes([168, 0]) + b'\x11\x00\x00'
        self.assertEqual(lzo1x_decompress(stream, 9), b'abcabcabc')

    def test_wrong_size_is_rejected(self):
        from lzo1x import lzo1x_decompress
        with self.assertRaises(ValueError):
            lzo1x_decompress(bytes([17 + 5]) + b'hello' + b'\x11\x00\x00', 6)


class TextureTests(unittest.TestCase):
    """Synthetic images (no game data)."""

    def test_mips_keep_colour_where_alpha_is_zero(self):
        import numpy as np
        from PIL import Image
        import mkx_meshmod as mk
        mask = Image.new('RGBA', (16, 16), (128, 0, 128, 0))   # Pmsk-style data: alpha 0 must not erase RGB
        for mips in (mk.image_mips(mask, 16, 16, 5), mk.image_mips(mask, 8, 8, 4)):
            for level in mips:
                self.assertEqual({tuple(int(x) for x in v) for v in np.unique(level.reshape(-1, 4), axis=0)}, {(128, 0, 128, 0)})


class LodTests(unittest.TestCase):
    """Synthetic one-triangle LOD (no game data)."""

    def test_triangle_sampling_table_is_parsed(self):
        import struct
        import mkx_meshmod as mk
        geo = dict(sections=[dict(mat=0, chunk=0, base=0, tris=1)], indices=[0, 1, 2], active_bones=[0],
                   chunks=[dict(base=0, bonemap=[0], rigid=3, soft=0, maxinf=1)], size_field=3, required_bones=b'\x00',
                   positions=[(0, 0, 0), (1, 0, 0), (0, 1, 0)], tangents=[bytes(8)] * 3, influences=[bytes(8)] * 3,
                   uv0=[(0, 0)] * 3, uv1=[(0, 0)] * 3, dq=None, adjacency=[0, 1, 2] * 4)
        empty = mk.build_lod_bytes(geo, 0)
        L = mk.LODModel.parse(empty, 0)
        self.assertEqual((L.prob, L.alias, L.end), ([], [], len(empty)))
        filled = empty[:-8] + struct.pack('<IfIi', 1, 1.0, 1, 0)   # as stored on some prop meshes
        L = mk.LODModel.parse(filled, 0)
        self.assertEqual((L.prob, L.alias, L.end), ([1.0], [0], len(filled)))


if __name__ == '__main__': unittest.main()
