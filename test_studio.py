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

    def test_neutral_mask_adds_no_glow_or_tint(self):
        r, g, b, a = map(int, core.SOLIDS[core.NEUTRAL].split(':')[1].split(','))
        self.assertEqual((g, a), (0, 0))              # no glow, primary material settings
        self.assertIn(b, (127, 128))                  # middle of the tint mask: original diffuse colour
        self.assertGreaterEqual(r, 127)               # wounds still show

    def test_body_texture_is_not_guessed_for_eyes(self):
        slots = [{'texture': 'body', 'param': 'DiffuseMap', 'slots': [0, 1, 2], 'replaceable': True},
                 {'texture': 'eyes', 'param': 'DiffuseMap', 'slots': [3], 'replaceable': True}]
        result = core.guess_textures(slots, ['MyChar/diff.png'], 'MyChar.glb')
        self.assertEqual(result, {'body': 'MyChar/diff.png', 'eyes': core.KEEP})


class PmskTests(unittest.TestCase):
    """Synthetic greyscale images (no game data)."""

    def image(self, left, right=0):
        from PIL import Image
        im = Image.new('L', (8, 4), right)
        im.paste(left, (0, 0, 4, 4))
        return im

    def test_empty_rows_give_the_neutral_mask(self):
        import numpy as np
        px = np.asarray(core.compose_pmsk({}, (8, 4))).reshape(-1, 4)
        self.assertTrue((px == core.PMSK_NEUTRAL).all())

    def test_each_image_fills_its_channel(self):
        import numpy as np
        out = np.asarray(core.compose_pmsk({'wounds': self.image(0, 255), 'glow': self.image(255),
                                            'tint': self.image(0, 255), 'surface': self.image(0, 255)}))
        self.assertEqual(tuple(out[0, 0]), (0, 255, 0, 0))         # left: no wounds, glow, tint colour 1, settings 1
        self.assertEqual(tuple(out[0, 7]), (127, 0, 255, 255))     # right: wounds, no glow, tint colour 2, settings 2
        self.assertEqual(core.compose_pmsk({'glow': self.image(255)}).size, (8, 4))   # size follows the first image

    def test_transparent_pixels_count_as_black(self):
        from PIL import Image
        im = Image.new('RGBA', (4, 4), (255, 255, 255, 0))
        self.assertEqual(float(core.mask_array(im).max()), 0.0)


class PmskExtractTests(unittest.TestCase):
    """Synthetic data (no game files): the recolour maths, names and layouts used by Extract Pmsk."""

    def test_recolour_interpretation_endpoints(self):
        import numpy as np
        import mkx_pmsk as pm
        diffuse = np.full((1, 3, 3), 0.5)
        blue = np.array([[0.0, 0.5, 1.0]])                   # group 1, keep the texture colour, group 2
        slots = np.ones((1, 3), int)                          # every texel belongs to slot 0
        out = pm.recolour(diffuse, blue, slots, {0: (((2.0, 1.0, 1.0), 0.0), ((1.0, 1.0, 0.5), 0.0))})
        lin = 0.5 ** 2.2
        expect = [[lin * 2, lin, lin], [lin, lin, lin], [lin, lin, lin * 0.5]]
        np.testing.assert_allclose(out[0], np.clip(np.array(expect), 0, 1) ** (1 / 2.2), atol=1e-6)

    def test_names_and_layouts(self):
        import mkx_pmsk as pm
        self.assertEqual(pm.alt_palette_name('Materials.KT_A1_Cloth1_MIC'), 'Materials.KT_A1_Cloth1_AP_MIC')
        self.assertEqual(pm.material_family('CHAR_MetalGlow_MIC'), 'MetalGlow')
        self.assertEqual(pm.material_family('CHAR_CostumeRimGlow_MIC'), 'Costume')
        self.assertEqual(pm.material_family('CHAR_HairTransparent_MIC'), 'HairTransparent')
        for family in ('EyesMouth', 'EyeFilm', 'HairOpaque', 'HairTransparent', 'EyeLashes'):
            self.assertIn(family, pm.OTHER_LAYOUTS)
            self.assertNotIn(family, pm.FAMILY_CHANNELS)

    def test_normalized_editing_round_trip_for_red_0_and_127(self):
        import numpy as np
        from PIL import Image
        import mkx_pmsk as pm
        rng = np.random.default_rng(2)
        px = rng.integers(0, 256, (8, 8, 4)).astype(np.uint8); px[..., 0] = rng.choice([0, 127], (8, 8))
        images = pm.channel_images(Image.fromarray(px, 'RGBA'), body=True)
        again = np.asarray(core.compose_pmsk(images)).astype(int)
        self.assertLessEqual(int(np.abs(again - px).max()), 1)

    def test_raw_export_and_rebuild_preserve_every_byte_value(self):
        import numpy as np
        from PIL import Image
        import mkx_pmsk as pm
        values = np.arange(256, dtype=np.uint8).reshape(16, 16)
        px = np.stack([values, np.flipud(values), np.fliplr(values), np.zeros_like(values)], axis=-1)
        # Preserve RGB data even where alpha is zero, including red values above 127.
        with tempfile.TemporaryDirectory() as folder:
            names = pm.export_channel_images(Image.fromarray(px), folder, 'Sample', body=True)
            with Image.open(Path(folder) / names['rgba']) as im:
                np.testing.assert_array_equal(np.asarray(im), px)
            paths = {k: Path(folder) / name for k, name in names['raw'].items()}
            np.testing.assert_array_equal(np.asarray(core.compose_pmsk(paths, raw=True)), px)
            self.assertEqual(len(names['editing']), 4)
            edits = {k: Path(folder) / name for k, name in names['editing'].items()}
            rebuilt = np.asarray(core.compose_pmsk(edits))
            np.testing.assert_array_equal(rebuilt[..., 1:], px[..., 1:])
            np.testing.assert_array_equal(rebuilt[..., 0], np.minimum(values, 127))

    def test_raw_export_other_layout_has_no_body_editing_masks(self):
        from PIL import Image
        import mkx_pmsk as pm
        with tempfile.TemporaryDirectory() as folder:
            names = pm.export_channel_images(Image.new('RGBA', (3, 2), (255, 1, 2, 3)), folder, 'Hair', body=False)
            self.assertEqual(names['editing'], {})
            result = core.compose_pmsk({k: Path(folder) / v for k, v in names['raw'].items()}, raw=True)
            self.assertEqual(result.getpixel((0, 0)), (255, 1, 2, 3))

    def test_raw_mode_rejects_inputs_that_would_need_conversion(self):
        from PIL import Image
        keys = [k for k, _, _ in core.PMSK_LAYERS]
        layers = {k: Image.new('L', (4, 2), 128) for k in keys}
        with self.assertRaisesRegex(core.mk.MKXError, 'all four'):
            core.compose_pmsk({'wounds': layers['wounds']}, raw=True)
        for bad, message in [(Image.new('L', (3, 2)), 'identical dimensions'),
                             (Image.new('RGB', (4, 2), (255, 0, 0)), 'grayscale'),
                             (Image.new('I;16', (4, 2)), '8-bit')]:
            with self.subTest(message=message), self.assertRaisesRegex(core.mk.MKXError, message):
                core.compose_pmsk(dict(layers, wounds=bad), raw=True)
        with self.assertRaisesRegex(core.mk.MKXError, 'identical dimensions'):
            core.compose_pmsk(layers, size=(8, 4), raw=True)

    def test_raw_mode_ignores_transparency_on_grayscale_inputs(self):
        from PIL import Image
        im = Image.new('RGBA', (2, 1), (173, 173, 173, 0))
        result = core.compose_pmsk({k: im for k, _, _ in core.PMSK_LAYERS}, raw=True)
        self.assertEqual(result.getpixel((0, 0)), (173, 173, 173, 173))

    def test_raw_cli_writes_exact_channels(self):
        import sys
        from PIL import Image
        import studio_cli
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = ['studio_cli.py', '--workspace', str(root / 'mod'), '--game', '', 'pmsk', 'Example', '--raw']
            for colour, value in zip(('red', 'green', 'blue', 'alpha'), (255, 42, 128, 0)):
                path = root / (colour + '.png')
                Image.new('L', (4, 2), value).save(path)
                args += ['--' + colour, str(path)]
            with patch.object(sys, 'argv', args):
                self.assertEqual(studio_cli.main(), 0)
            with Image.open(root / 'mod' / 'textures' / 'Example' / 'Example_Pmsk.png') as im:
                self.assertEqual(im.getpixel((0, 0)), (255, 42, 128, 0))


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


class BoneDirectionTests(unittest.TestCase):
    """Synthetic skeleton (no game data): the bone directions used for the generated .blend."""

    def setUp(self):
        import blender_rig
        self.tails = blender_rig.bone_tails
        # 0 root, 1 hips, 2 spine, 3 head, 4-10 face joints, 11/12 legs, 13 twist joint on the hips, 14 hand, 15-17 fingers
        self.heads = [(0, 0, 0), (0, 0, 100), (0, 0, 130), (0, 0, 170), *[(10, x, 175) for x in range(-3, 4)],
                      (0, -10, 95), (0, 10, 95), (0, 0, 100), (40, 0, 130), (45, -2, 130), (45, 0, 130), (45, 2, 130)]
        self.parents = [-1, 0, 1, 2, *[3] * 7, 1, 1, 1, 2, 14, 14, 14]

    def direction(self, i):
        t, h = self.tails(self.heads, self.parents)[i], self.heads[i]
        v = [t[k] - h[k] for k in range(3)]
        n = sum(x * x for x in v) ** 0.5
        return tuple(round(x / n, 3) for x in v), n

    def test_chain_bones_reach_their_main_child(self):
        tails = self.tails(self.heads, self.parents)
        self.assertEqual(tuple(tails[1]), self.heads[2])          # hips -> spine, not the legs or the twist joint
        self.assertEqual(self.direction(0)[0], (0, 0, 1))          # root aims at the hips but stays short
        self.assertLess(self.direction(0)[1], 100)

    def test_hub_and_hand_and_leaves(self):
        self.assertEqual(self.direction(3)[0], (0, 0, 1))          # head with face joints keeps going up
        self.assertEqual(tuple(self.tails(self.heads, self.parents)[14]), (45, 0, 130))   # hand -> average of fingers
        self.assertEqual(self.direction(16)[0], (1, 0, 0))         # fingertip continues its finger
        self.assertEqual(self.direction(13)[0], self.direction(1)[0])   # joint on its parent's head follows the parent
        self.assertTrue(all(self.direction(i)[1] > 0 for i in range(len(self.heads))))


class ConvertHelperTests(unittest.TestCase):
    """Synthetic data: the made second UV map and the diffuse preparation."""

    def test_made_uv2_separates_overlapping_islands(self):
        import mkx_meshmod as mk
        tri = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        uv = [(0.1, 0.1), (0.5, 0.1), (0.1, 0.5)]                 # two islands on exactly the same UVs
        a, b = mk.pack_uv2([(tri, uv, [0, 1, 2]), (tri, uv, [0, 1, 2])])
        boxes = [(min(u for u, _ in x), max(u for u, _ in x), min(v for _, v in x), max(v for _, v in x)) for x in (a, b)]
        self.assertTrue(all(0 <= c <= 1 for box in boxes for c in box))
        (a0, a1, a2, a3), (b0, b1, b2, b3) = boxes
        self.assertTrue(a1 <= b0 or b1 <= a0 or a3 <= b2 or b3 <= a2)  # no overlap

    def test_prepare_diffuse(self):
        import numpy as np
        from PIL import Image
        bright = core.prepare_diffuse(Image.new('RGB', (4, 4), (250, 250, 250)), log=lambda m: None)
        px = np.asarray(bright)
        self.assertEqual(int(px[0, 0, 3]), core.DIFFUSE_SHADING)
        self.assertLess(int(px[0, 0, 0]), core.BRIGHT_LIMIT)
        dark = np.asarray(core.prepare_diffuse(Image.new('RGBA', (4, 4), (60, 50, 40, 120)), log=lambda m: None))
        self.assertEqual(tuple(dark[0, 0]), (60, 50, 40, 120))       # MKX-like textures stay untouched

    def test_extra_object_packages_and_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            game, ws = Path(tmp) / 'MK10', Path(tmp) / 'mod'
            (game / 'Asset').mkdir(parents=True); core.setup_workspace(str(ws))
            for name in ('CHAR_Nomad_A.xxx', 'Char_Nomad_A_ScriptAssets.xxx', 'Char_Nomad_B_ScriptAssets.xxx',
                         'TRAIT_Nomad1_ScriptAssets.xxx', 'TRAIT_NomadX1_ScriptAssets.xxx', 'UI_PS_NOMAD_SCRIPTASSETS.xxx',
                         'Nomad_Fatality1_MapAssets.xxx'):
                (game / 'Asset' / name).write_bytes(b'')
            self.assertEqual(sorted(core.object_packages(str(ws), str(game), 'CHAR_Nomad_A.xxx')),
                             ['Char_Nomad_A_ScriptAssets.xxx', 'TRAIT_Nomad1_ScriptAssets.xxx', 'UI_PS_NOMAD_SCRIPTASSETS.xxx'])
        self.assertEqual(core.object_label('Characters.CHAR.Nomad.SharedProps.Meshes.NO_TraitHat_sk',
                                           ['UI_PS_NOMAD_SCRIPTASSETS.xxx', 'TRAIT_Nomad1_ScriptAssets.xxx']),
                         'Trait Hat  (variation 1, select screen)')

    def test_normals_are_checked_against_the_surface(self):
        import mkx_meshmod as mk
        # a closed box (counter-clockwise outward faces, glTF convention) with exact outward face normals
        P, N, idx = [], [], []
        for axis in range(3):
            for s in (1, -1):
                u, w = (axis + 1) % 3, (axis + 2) % 3
                corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)] if s > 0 else [(-1, -1), (-1, 1), (1, 1), (1, -1)]
                base = len(P)
                for a, b in corners:
                    p = [0.0] * 3; p[axis], p[u], p[w] = s, a, b; P.append(tuple(p))
                    n = [0.0] * 3; n[axis] = s; N.append(tuple(n))
                idx += [base, base + 1, base + 2, base, base + 2, base + 3]
        good, msg = mk.check_normals([(P, N, idx)])
        self.assertIsNone(msg)
        self.assertEqual(good[0], N)
        turned = [(n[2], -n[0], -n[1]) for n in N]                  # the same normals in a different axis frame
        fixed, msg = mk.check_normals([(P, turned, idx)])
        self.assertIn('turned them back', msg)
        self.assertTrue(all(abs(a - b) < 1e-9 for x, y in zip(fixed[0], N) for a, b in zip(x, y)))


class BoneCategoryTests(unittest.TestCase):
    """Synthetic bone names following MKX naming (no game data)."""

    def test_categories(self):
        import blender_rig
        names = ['Reference', 'Hips', 'Spine', 'Neck', 'Head', 'Jaw', 'LeftEye', 'C_CenterPonytailJoint0', 'LeftArm',
                 'LeftArmRoll', 'LeftElbow_helper', 'C_LeftCapeJoint0', 'Dummy_LeftCapeJoint0', 'wrinkle_smile', 'CenterOfMass']
        parents = [-1, 0, 1, 2, 3, 4, 4, 4, 2, 8, 8, 2, 2, 0, 1]
        self.assertEqual(blender_rig.bone_categories(names, parents),
                         ['body', 'body', 'body', 'body', 'body', 'facial', 'facial', 'cloth', 'body',
                          'helper', 'helper', 'cloth', 'helper', 'facial', 'helper'])
        self.assertEqual(blender_rig.NO_DEFORM, {'facial', 'cloth'})


class BlenderTests(unittest.TestCase):
    def test_chosen_blender_is_preferred(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root) / 'blender.exe'; exe.write_bytes(b'')
            self.assertEqual(core.find_blender('', str(exe)), str(exe))

    def test_failed_blender_run_is_reported(self):
        import sys
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(core.mk.MKXError):   # Python stands in for a Blender that exits without a result
                core.make_blend(Path(root) / 'in.glb', Path(root) / 'out.blend', sys.executable)


if __name__ == '__main__': unittest.main()
