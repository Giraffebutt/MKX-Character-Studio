"""
mkx_pmsk.py - what a Mortal Kombat X character does with its Pmsk mask texture.

Reads each material slot's settings (following the Parent chain into the base materials in Startup.xxx), maps the
mesh's UV layout to its material slots, and approximates the interpreted recolour step.

Working channel interpretation for CHAR_Costume, CHAR_Skin, CHAR_Metal and their Glow/Opacity versions.
Material parameters and alternate-palette swapping are corroborated by asset/code inspection. Exact channel
mapping, thresholds and blending still need traceable shader evidence and in-game validation.
  red    wound areas: 0 = wounds and blood never show, 127 or more = they can (cloth/leather "Costume" materials)
  green  glow areas: 0 = none, 255 = full GLOW_Color (Glow materials only)
  blue   recolour groups: 0 = group 1 (PRIMARY_AlbedoTint), 127 = keep the texture colour, 255 = group 2 (ALTERNATE_)
  alpha  surface finish: 0 = finish 1 (PRIMARY_ roughness, specularity, metalness, subsurface, detail pattern),
         255 = finish 2 (ALTERNATE_ ...)
"""
import io
import os
import struct

import mkx_meshmod as mk

# Interpreted channels per base family; this table is not runtime shader introspection.
FAMILY_CHANNELS = {'Costume': 'RBA', 'Skin': 'BA', 'Metal': 'BA', 'CostumeGlow': 'RGBA', 'SkinGlow': 'GBA',
                   'MetalGlow': 'GBA', 'Hair': 'BA'}
FAMILY_TEXT = {'Costume': 'cloth/leather', 'Skin': 'skin', 'Metal': 'metal', 'CostumeGlow': 'glowing cloth/leather',
               'SkinGlow': 'glowing skin', 'MetalGlow': 'glowing metal', 'Hair': 'hair', 'EyesMouth': 'eyes/mouth',
               'EyeFilm': 'eye film', 'EyeLashes': 'eyelash', 'HairOpaque': 'opaque hair', 'HairTransparent': 'see-through hair'}
# Working interpretations of other layouts: (description, channel image names).
OTHER_LAYOUTS = {
    'EyesMouth': ('red = iris detail colour, green = glow, alpha = eye settings (black) or tongue/gums/mouth settings '
                  '(white); blue is not used', ('iris_detail', 'glow', 'unused', 'eye_or_mouth_settings')),
    'EyeFilm': ('red = iris detail colour, green = glow; blue and alpha are not used',
                ('iris_detail', 'glow', 'unused', 'unused')),
    'HairOpaque': ('red = highlight, roughness and shine strength, green = shadowing between strands; blue and alpha '
                   'are not used', ('highlight_strength', 'strand_shadowing', 'unused', 'unused')),
    'HairTransparent': ('red = highlight, roughness and shine strength, green = shadowing between strands, blue x alpha '
                        '= cut-out: pixels where blue times alpha is below half are cut away (alpha is read through the '
                        'second UV map)', ('highlight_strength', 'strand_shadowing', 'cutout', 'cutout_uv2')),
    'EyeLashes': ('does not read the Pmsk', ('unused', 'unused', 'unused', 'unused')),
}
CHANNELS = [('wounds', 'red', 'wound_areas'), ('glow', 'green', 'glow_areas'), ('tint', 'blue', 'recolour_groups'),
            ('surface', 'alpha', 'surface_finish')]


def material_family(name):
    """Base material family from a CHAR_MaterialLibrary material name, e.g. CHAR_MetalGlow_MIC -> MetalGlow."""
    n = (name or '').lower()
    for key, family in (('eyesmouth', 'EyesMouth'), ('eyefilm', 'EyeFilm'), ('eyelash', 'EyeLashes'),
                        ('hairopaque', 'HairOpaque'), ('hairtransparent', 'HairTransparent'), ('hair', 'Hair')):
        if key in n:
            return family
    glow = 'glow' in n and 'rimglow' not in n
    for family in ('Costume', 'Skin', 'Metal'):
        if family.lower() in n:
            return family + ('Glow' if glow else '')
    return None


def alt_palette_name(path):
    """The game's alternate-palette material name: '_AP' inserted before the last '_' (X_Cloth1_MIC -> X_Cloth1_AP_MIC)."""
    cut = path.rfind('_')
    return path if cut < 0 else path[:cut] + '_AP' + path[cut:]


class MaterialInfo:
    """Material parameter values, following each material's Parent chain into the base materials in Startup.xxx."""

    def __init__(self, startup_path):
        self.startup_path, self._startup, self._index, self._cache = startup_path, None, None, {}

    def startup(self):
        if self._startup is None:
            if not self.startup_path or not os.path.isfile(self.startup_path):
                raise mk.MKXError('Asset\\Startup.xxx was not found in the game folder; it holds the base character materials.')
            self._startup = mk.load(self.startup_path)
            self._index = {self._startup.objref(i + 1).lower(): i + 1 for i in range(len(self._startup.exports))}
        return self._startup

    @staticmethod
    def _props(p, idx):
        e = p.exports[idx - 1]
        try:
            return mk.parse_props(p, p.image, e['SerialOffset'], e['SerialOffset'] + e['SerialSize'])[0]
        except Exception:
            return []

    @staticmethod
    def _value(p, prop):
        name, typ, extra, size, aidx, vo = prop
        if typ == 'FloatProperty':
            return struct.unpack_from('<f', p.image, vo)[0]
        if typ == 'StructProperty' and size >= 16:
            return struct.unpack_from('<4f', p.image, vo)
        if typ == 'ObjectProperty':
            ref = struct.unpack_from('<i', p.image, vo)[0]
            return p.objref(ref) if ref else None
        return None

    def _own(self, p, idx):
        """(this object's own parameter values, parent index)."""
        values, parent = {}, 0
        props = self._props(p, idx)
        if p.classname(p.exports[idx - 1]['Class']) == 'Material':
            for name, typ, extra, size, aidx, vo in props:
                if name == 'Expressions' and typ == 'ArrayProperty':
                    n = struct.unpack_from('<i', p.image, vo)[0]
                    for x in struct.unpack_from('<%di' % n, p.image, vo + 4):
                        if x > 0:
                            em = mk.prop_map(self._props(p, x))
                            if 'ParameterName' in em:
                                pn = p.name(*struct.unpack_from('<II', p.image, em['ParameterName'][5]))
                                vector = 'Vector' in p.classname(p.exports[x - 1]['Class'])
                                values[pn] = self._value(p, em['DefaultValue']) if 'DefaultValue' in em else (
                                    (0.0, 0.0, 0.0, 0.0) if vector else 0.0)
            return values, 0
        for name, typ, extra, size, aidx, vo in props:
            if name == 'Parent' and typ == 'ObjectProperty':
                parent = struct.unpack_from('<i', p.image, vo)[0]
            elif name in ('ScalarParameterValues', 'VectorParameterValues', 'TextureParameterValues') and typ == 'ArrayProperty':
                o = vo + 4
                for _ in range(struct.unpack_from('<i', p.image, vo)[0]):
                    el, o = mk.parse_props(p, p.image, o, vo + size)
                    em = mk.prop_map(el)
                    if 'ParameterName' in em and 'ParameterValue' in em:
                        values[p.name(*struct.unpack_from('<II', p.image, em['ParameterName'][5]))] = self._value(p, em['ParameterValue'])
        return values, parent

    def param_guid(self, p, idx, name):
        """ExpressionGUID of scalar parameter `name` as material `idx` sees it: from the nearest material instance that
        sets it, or from the parameter expression in its base material."""
        pkg, seen = p, set()
        while idx and (id(pkg), idx) not in seen:
            seen.add((id(pkg), idx))
            if idx < 0:
                path = pkg.objref(idx)
                pkg = self.startup(); idx = self._index.get(path.lower(), 0)
                continue
            if pkg.classname(pkg.exports[idx - 1]['Class']) == 'Material':
                pm = mk.prop_map(self._props(pkg, idx))
                if 'Expressions' not in pm:
                    return None
                vo = pm['Expressions'][5]
                for x in struct.unpack_from('<%di' % struct.unpack_from('<i', pkg.image, vo)[0], pkg.image, vo + 4):
                    if x > 0:
                        em = mk.prop_map(self._props(pkg, x))
                        if ('ParameterName' in em and 'ExpressionGUID' in em and
                                pkg.name(*struct.unpack_from('<II', pkg.image, em['ParameterName'][5])) == name):
                            return bytes(pkg.image[em['ExpressionGUID'][5]:em['ExpressionGUID'][5] + 16])
                return None
            own = mk.material_scalars(pkg, idx)
            if name in own:
                return own[name][1]
            pm = mk.prop_map(self._props(pkg, idx))
            idx = struct.unpack_from('<i', pkg.image, pm['Parent'][5])[0] if 'Parent' in pm else 0
        return None

    def resolve(self, p, idx):
        """(parameter values, base material name) of material `idx` in package `p`; the nearest setting wins."""
        key = (id(p), idx)
        if key in self._cache:
            return self._cache[key]
        values, base, pkg, seen = {}, '', p, set()
        while idx and (id(pkg), idx) not in seen:
            seen.add((id(pkg), idx))
            if idx < 0:                                              # a library material: continue inside Startup.xxx
                path = pkg.objref(idx)
                base = base or path.split('.')[-1]
                s = self.startup()
                pkg, idx = s, self._index.get(path.lower(), 0)
                continue
            own, parent = self._own(pkg, idx)
            for k, v in own.items():
                values.setdefault(k, v)
            if pkg is self._startup or not parent:
                base = base or pkg.objref(idx).split('.')[-1]
            idx = parent
        self._cache[key] = (values, base)
        return values, base


def mesh_looks(p, m):
    """[(look name, material index per slot)]: the default look, the alternate palette, and each variation set
    (with its alternate palette), keeping only looks that use different materials."""
    ids = mk.mesh_material_ids(p, m)
    paths = {p.objref(i + 1).lower(): i + 1 for i in range(len(p.exports))}

    def ap(mats):
        return [paths.get(alt_palette_name(p.objref(i)).lower(), i) if i > 0 else i for i in mats]
    looks = [('Default look', ids), ('Alternate palette', ap(ids))]
    pr = m.pm.get('AlternateMaterialSets')
    if pr:
        d, vo, size = p.image, pr[5], pr[3]
        o = vo + 4
        for _ in range(struct.unpack_from('<i', d, vo)[0]):
            el, o = mk.parse_props(p, d, o, vo + size)
            em = mk.prop_map(el)
            if 'Materials' not in em:
                continue
            name = p.name(*struct.unpack_from('<II', d, em['name'][5])) if 'name' in em else 'variation'
            av = em['Materials'][5]
            mats = list(struct.unpack_from('<%di' % struct.unpack_from('<i', d, av)[0], d, av + 4))
            mats = [x or ids[k] for k, x in enumerate(mats[:len(ids)])] + ids[len(mats):]
            looks += [('Variation %s' % name, mats), ('Variation %s, alternate palette' % name, ap(mats))]
    out, seen = [], set()
    for name, mats in looks:
        if tuple(mats) not in seen:
            seen.add(tuple(mats)); out.append((name, mats))
    return out


def slot_map(lod, size, slots=None):
    """(H, W) array: material slot + 1 of each texel by rasterising the LOD's UV0 triangles (0 = unused). Pass the
    slots that share one texture: other materials use other textures, so their UVs may overlap this layout."""
    import math
    import numpy as np
    from PIL import Image, ImageDraw
    w, h = size
    im = Image.new('L', size, 0)
    draw = ImageDraw.Draw(im)
    for s in lod.sections:
        if slots is not None and s['mat'] not in slots:
            continue
        idx = lod.indices[s['base']:s['base'] + 3 * s['tris']]
        for k in range(0, len(idx), 3):
            uv = [lod.uv(v, 0) for v in idx[k:k + 3]]
            du = math.floor(sum(u for u, _ in uv) / 3); dv = math.floor(sum(v for _, v in uv) / 3)
            draw.polygon([((u - du) * w, (v - dv) * h) for u, v in uv], fill=min(s['mat'] + 1, 255))
    return np.asarray(im)


def texture_image(p, path):
    """Largest stored mip of a BC7 texture as a PIL RGBA image, or None when none is stored in the package."""
    from PIL import Image
    try:
        t = mk.read_texture(p, p.find_export(path, 'Texture2D'))
    except KeyError:
        return None
    stored = [x for x in t['mips'] if x['data_pos'] is not None]      # PC builds leave out some top levels
    if t['fmt'] != 22 or not stored:
        return None
    m = stored[0]
    im = Image.open(io.BytesIO(mk.dds_bytes(m['w'], m['h'], [bytes(p.image[m['data_pos']:m['data_pos'] + m['size']])], t['srgb'])))
    im.load()
    return im.convert('RGBA')


def tint_of(values, which):
    """(colour multiplier (r, g, b), desaturation 0..1) of recolour group `which` (1 or 2)."""
    prefix = 'PRIMARY_' if which == 1 else 'ALTERNATE_'
    c = values.get(prefix + 'AlbedoTint') or (1.0, 1.0, 1.0)
    return tuple(c[:3]), float(values.get(prefix + 'AlbedoTintDesaturate') or 0.0)


def recolour(diffuse, blue, slots, tints):
    """Approximate diffuse colours (H, W, 3 floats 0..1, sRGB) under the interpreted recolour step. `blue` is the Pmsk blue channel
    (0..1), `slots` the slot map, `tints` {slot: ((colour1, desat1), (colour2, desat2))}."""
    import numpy as np
    lin = diffuse ** 2.2
    out = lin.copy()
    grey = (lin @ np.array([0.3, 0.59, 0.11]))[..., None]
    g1 = np.clip(2 * blue, 0, 1)[..., None]
    g2 = np.clip(2 * blue - 1, 0, 1)[..., None]
    for slot, ((c1, d1), (c2, d2)) in tints.items():
        m = slots == slot + 1
        if not m.any():
            continue
        D, G = lin[m], grey[m]
        col1 = (D + d1 * (G - D)) * np.array(c1)
        col2 = (D + d2 * (G - D)) * np.array(c2)
        r = col1 + g1[m] * (D - col1)                # blue 0 -> group 1 colour, 0.5 -> texture colour
        out[m] = r + g2[m] * (col2 - r)              # blue 1 -> group 2 colour
    return np.clip(out, 0, 1) ** (1 / 2.2)


def channel_images(pmsk, body):
    """Channel images; body=True produces editing masks with red stretched/clipped, NOT a lossless split."""
    import numpy as np
    from PIL import Image
    px = np.asarray(pmsk.convert('RGBA')).astype(np.float64)
    out = {}
    for k, (key, colour, _) in enumerate(CHANNELS):
        ch = px[..., k] * (255.0 / 127 if body and key == 'wounds' else 1.0)
        out[key] = Image.fromarray(np.clip(np.rint(ch), 0, 255).astype(np.uint8), 'L')
    return out


def export_channel_images(pmsk, folder, stem, body):
    """Save decoded RGBA pixels and raw grayscale channels, plus optional normalized body editing masks.

    Raw PNGs preserve the decoded stored mip, not its original BC7 compression or other mip levels.
    """
    from pathlib import Path
    folder = Path(folder)
    rgba = pmsk.convert('RGBA')
    names = {'rgba': stem + '_raw_rgba.png', 'raw': {}, 'editing': {}}
    rgba.save(folder / names['rgba'])
    for k, (key, colour, label) in enumerate(CHANNELS):
        name = '%s_raw_%s.png' % (stem, colour)
        rgba.getchannel(k).save(folder / name)
        names['raw'][key] = name
    if body:
        editing = channel_images(rgba, True)
        for key, colour, label in CHANNELS:
            name = '%s_edit_%s_%s.png' % (stem, colour, label)
            editing[key].save(folder / name)
            names['editing'][key] = name
    return names


def describe_tint(colour, desat):
    if all(abs(x - 1) < 1e-3 for x in colour) and desat < 1e-3:
        return 'unchanged'
    text = 'x(%.2f, %.2f, %.2f)' % tuple(colour)
    return text + (', %d%% greyed first' % round(desat * 100) if desat >= 0.005 else '')
