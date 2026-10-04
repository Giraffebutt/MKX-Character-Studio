"""Workspace-only operations shared by the desktop application and safe CLI."""
from pathlib import Path
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile

import mkx_meshmod as mk

HERE = Path(__file__).resolve().parent
APP = 'MKX Character Studio'
KEEP, FLAT, NEUTRAL = '(keep original)', '(flat normal map)', '(neutral mask)'
SOLIDS = {FLAT: 'solid:128,128,255,160', NEUTRAL: 'solid:127,77,240,0'}
IMAGE_EXT = ('.png', '.tga', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.dds', '.webp')
ROLE_NAMES = {'DiffuseMap': 'Diffuse / colour', 'NormalMap': 'Normal map', 'Pmsk': 'PMSK mask'}
FOLDERS = [
    ('character', 'Import your rigged GLB here using Import GLB in the app.'),
    ('textures', 'Custom PNG / TGA / JPG / DDS textures; one subfolder per model.'),
    ('converted', 'Verified MKX packages, previews and build reports. Each build gets a new folder.'),
    ('vanilla_exports', 'GLB references, original textures and material slot guides.'),
    ('vanilla_cache', 'Managed template copies and SHA-256 receipts. Do not edit these files.'),
]


def is_inside(path, parent):
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)),
                                  os.path.normcase(os.path.realpath(parent))]) == os.path.normcase(os.path.realpath(parent))
    except ValueError:
        return False


def detect_game_dir():
    for path in list(HERE.parents) + [Path(r'C:\Program Files (x86)\Steam\steamapps\common\MK10')]:
        if (path / 'Asset').is_dir() and (path / 'Binaries').is_dir():
            return str(path)
    return ''


def inside_game_content(path, game_dir):
    # A dedicated projects directory in this isolated edition is the sole in-install exception.
    roots = {str(Path(g).resolve()) for g in (game_dir, detect_game_dir()) if g}
    roots.update(str(p) for p in [Path(path).absolute(), *Path(path).absolute().parents]
                 if (p / 'Asset').is_dir() and (p / 'Binaries').is_dir())
    return any(is_inside(path, g) and not is_inside(path, HERE / 'projects') for g in roots)


def guard_output(path, game_dir=''):
    if inside_game_content(path, game_dir):
        raise mk.MKXError('Choose a mod folder outside the game installation, or inside:\n%s\n'
                          'Game folders and the original tool are protected.' % (HERE / 'projects'))
    for parent in [Path(path).absolute(), *Path(path).absolute().parents]:
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise mk.MKXError('Output folders must not use symbolic links or junctions: %s' % parent)
    if Path(path).is_file() and Path(path).stat().st_nlink > 1:
        raise mk.MKXError('Refusing to overwrite a linked file: %s' % path)


def child(root, *parts):
    path = Path(root).joinpath(*parts)
    if not is_inside(path, root) or path.resolve() == Path(root).resolve():
        raise mk.MKXError('Path must stay inside its workspace folder: %s' % path)
    return path


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).strip(' .')[:100]
    if not name or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}:
        raise mk.MKXError('Use a descriptive file name, such as MyCharacter.')
    return name


def package_name(name):
    if Path(name).name != name or '/' in name or '\\' in name or safe_name(name) != name or not name.lower().endswith('.xxx'):
        raise mk.MKXError('Choose a package filename ending in .xxx.')
    return name


def setup_workspace(ws, game_dir=''):
    if not ws or not str(ws).strip():
        raise mk.MKXError('Choose a mod folder first.')
    guard_output(ws, game_dir)
    for name, _ in FOLDERS:
        guard_output(child(ws, name), game_dir)
    Path(ws).mkdir(parents=True, exist_ok=True)
    for name, desc in FOLDERS:
        folder = child(ws, name)
        folder.mkdir(exist_ok=True)
        info = child(folder, '_what_goes_here.txt')
        if not info.exists():
            guard_output(info, game_dir)
            info.write_text(desc + '\n', encoding='utf-8')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def is_converted_file(path):
    # A conservative heuristic, NOT proof of authenticity. Cache hashes only detect later edits.
    return bool(mk.Package(str(path)).s['CompressionFlags'] & mk.COMPRESS_ZLIB)


def add_original_package(ws, path, log=print, game_dir=''):
    setup_workspace(ws, game_dir)
    path = Path(path)
    name = package_name(path.name)
    if is_converted_file(path):
        raise mk.MKXError('This package uses ZLIB compression, as converted packages do. Select an untouched backup.\n'
                          'Compression is only a heuristic; original provenance cannot be guaranteed by this tool.')
    dst = child(ws, 'vanilla_cache', name)
    receipt = dst.with_suffix('.xxx.json')
    guard_output(dst, game_dir); guard_output(receipt, game_dir)
    digest = sha256(path)
    if dst.exists():
        if not receipt.exists() or json.loads(receipt.read_text(encoding='utf-8')).get('sha256') != sha256(dst):
            raise mk.MKXError('Cached template has changed or has no receipt. Use a fresh mod folder.')
        if sha256(dst) != digest:
            raise mk.MKXError('A different template with this name is already cached. Use a fresh mod folder.')
        return str(dst)
    with tempfile.TemporaryDirectory(prefix='.cache-', dir=str(dst.parent)) as tmp:
        copy = Path(tmp) / name
        shutil.copyfile(path, copy)
        if sha256(copy) != digest:
            raise mk.MKXError('Source changed while copying. Try again with the game closed.')
        p = mk.load(str(copy))
        if not mk.verify_package(p, log=log):
            raise mk.MKXError('The template failed package verification.')
        meta = Path(tmp) / receipt.name
        meta.write_text(json.dumps({'sha256': digest, 'source': str(path.resolve()),
                                   'provenance': 'user-supplied or local game copy; authenticity unverified'}, indent=2), encoding='utf-8')
        copy.rename(dst)
        meta.rename(receipt)
    log('Cached template: %s' % dst)
    return str(dst)


def template_path(ws, game_dir, pkg_name, log=print):
    setup_workspace(ws, game_dir)
    package_name(pkg_name)
    cache = child(ws, 'vanilla_cache', pkg_name)
    if cache.exists():
        receipt = cache.with_suffix('.xxx.json')
        if not receipt.exists() or json.loads(receipt.read_text(encoding='utf-8')).get('sha256') != sha256(cache):
            raise mk.MKXError('Cached template was changed or has no integrity receipt. Use a fresh mod folder and add the original again.')
        return str(cache)
    src = Path(game_dir) / 'Asset' / pkg_name
    if not src.is_file():
        raise mk.MKXError('Original not found. Use Add original package to select your backup.')
    return add_original_package(ws, src, log, game_dir)


def list_character_packages(game_dir, show_all=False):
    root = Path(game_dir) / 'Asset'
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.glob('*.xxx') if show_all or
                  (re.match(r'(?i)^(char|dism)_', p.name) and 'scriptassets' not in p.name.lower()))


def default_mesh(meshes, pkg_name):
    expected = 'meshes.' + re.sub(r'(?i)^char_|\.xxx$', '', pkg_name)
    return next((m for m in meshes if m.lower() == expected.lower()),
                next((m for m in meshes if m.lower().startswith('meshes.')), next(iter(meshes), '')))


def job_load_package(ws, game_dir, pkg_name, log=print):
    p = mk.load(template_path(ws, game_dir, pkg_name, log))
    meshes = mk.list_skeletal_meshes(p)
    if not meshes:
        raise mk.MKXError('This package contains no skeletal mesh. Select a character package.')
    return dict(meshes=meshes, default=default_mesh(meshes, pkg_name))


def job_texture_slots(ws, game_dir, pkg_name, mesh, log=print):
    p = mk.load(template_path(ws, game_dir, pkg_name, log))
    m = mk.SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
    return dict(slots=mk.mesh_texture_slots(p, mesh), materials=[x.split('.')[-1] for x in m.materials()])


@contextmanager
def staged_output(ws, game_dir, folder, name):
    setup_workspace(ws, game_dir)
    parent = child(ws, folder)
    stem = safe_name(name)
    final = child(parent, stem)
    n = 2
    while final.exists():
        final = child(parent, '%s_%02d' % (stem, n)); n += 1
    guard_output(final, game_dir)
    with tempfile.TemporaryDirectory(prefix='.building-', dir=str(parent)) as temp:
        stage = Path(temp) / 'result'; stage.mkdir()
        yield stage, final
        guard_output(final, game_dir)
        stage.rename(final)


def job_export_vanilla(ws, game_dir, pkg_name, mesh, textures=True, embed=True, log=print):
    if embed or textures:
        from PIL import Image  # Fail before writing if PNG support is missing.
    p = mk.load(template_path(ws, game_dir, pkg_name, log))
    with staged_output(ws, game_dir, 'vanilla_exports', Path(pkg_name).stem + '_' + mesh.split('.')[-1]) as (stage, final):
        mk.export_ref(p, mesh, str(stage / (safe_name(mesh.split('.')[-1]) + '.glb')), embed_textures=embed, log=log)
        slots = mk.mesh_texture_slots(p, mesh)
        if textures:
            mk.export_textures(p, str(stage / 'textures'), only={s['texture'] for s in slots}, log=log)
        m = mk.SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
        lines = ['Blender materials (retain slotNN_ prefixes):']
        lines += ['slot%02d_%s' % (i, x.split('.')[-1]) for i, x in enumerate(m.materials())]
        lines += ['', 'Textures:'] + [json.dumps(s) for s in slots]
        lines += ['', 'Only inline BC7 textures are supported. Streamed .tfc textures and external material references are not exported.',
                  'The GLB embeds diffuse textures only. DDS/PNG files preserve other maps.',
                  'Keep the exported skeleton, scale and rest pose. This replaces LOD0; it does not generate fatality meshes.']
        (stage / 'slots_and_textures.txt').write_text('\n'.join(lines), encoding='utf-8')
    log('Export complete: %s' % final)
    return str(final)


def list_models(ws):
    return sorted(p.name for p in (Path(ws) / 'character').glob('*.glb'))


def list_images(ws):
    root = Path(ws) / 'textures'
    return sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_EXT)


def import_model(ws, source, game_dir='', log=print):
    setup_workspace(ws, game_dir)
    source = Path(source)
    if source.suffix.lower() != '.glb':
        raise mk.MKXError('Choose a self-contained binary glTF (.glb) file.')
    gl = mk.GLTF(str(source))
    if any('uri' in b for b in gl.j.get('buffers', [])) or any('uri' in im for im in gl.j.get('images', [])):
        raise mk.MKXError('Re-export as a self-contained GLB with embedded textures and buffers.')
    if not any('mesh' in n and 'skin' in n for n in gl.j.get('nodes', [])):
        raise mk.MKXError('This GLB has no rigged mesh. Rig it to the exported MKX skeleton first.')
    name = safe_name(source.stem)
    dst = child(ws, 'character', name + '.glb')
    if dst.exists() and sha256(dst) != sha256(source):
        raise mk.MKXError('A different GLB has this name. Rename it before importing; existing models are preserved.')
    # Extract embedded images and retain their material-slot associations for suggestions.
    root = child(ws, 'textures', name)
    guard_output(root, game_dir)
    root.mkdir(exist_ok=True)
    manifest = []
    for material in gl.j.get('materials', []):
        match = re.match(r'(?i)^slot(\d+)', material.get('name', ''))
        for role, tex in [('DiffuseMap', material.get('pbrMetallicRoughness', {}).get('baseColorTexture')),
                          ('NormalMap', material.get('normalTexture'))]:
            if not tex:
                continue
            index = gl.j['textures'][tex['index']].get('source')
            if index is None:
                raise mk.MKXError('Compressed GLB texture extensions are unsupported. Export PNG/JPEG images.')
            im = gl.j['images'][index]
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg'}.get(im.get('mimeType'))
            if suffix is None:
                raise mk.MKXError('Embedded images must be PNG or JPEG.')
            view = gl.j['bufferViews'][im['bufferView']]
            start = view.get('byteOffset', 0)
            data = gl.buffers[view['buffer']][start:start + view['byteLength']]
            filename = 'embedded_%02d_%s%s' % (index, role, suffix)
            image_path = child(root, filename); guard_output(image_path, game_dir)
            if image_path.exists() and image_path.read_bytes() != data:
                raise mk.MKXError('An extracted texture was edited. Rename the GLB to import a new version.')
            if not image_path.exists():
                image_path.write_bytes(data)
            manifest.append({'slot': int(match[1]) if match else None, 'role': role,
                             'file': str(image_path.relative_to(Path(ws) / 'textures'))})
    receipt = child(root, 'import_manifest.json'); guard_output(receipt, game_dir)
    receipt.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    guard_output(dst, game_dir)
    if not dst.exists():
        shutil.copyfile(source, dst)
    log('Imported %s; extracted %d texture associations. Review the texture choices before converting.' % (dst.name, len(manifest)))
    return dst.name


def import_images(ws, sources, model_name='', game_dir=''):
    setup_workspace(ws, game_dir)
    root = child(ws, 'textures', safe_name(Path(model_name).stem)) if model_name else child(ws, 'textures')
    guard_output(root, game_dir); root.mkdir(exist_ok=True)
    for source in map(Path, sources):
        if source.suffix.lower() not in IMAGE_EXT:
            raise mk.MKXError('Unsupported texture: %s' % source.name)
        dst = child(root, safe_name(source.name)); guard_output(dst, game_dir)
        if dst.exists():
            if sha256(dst) != sha256(source):
                raise mk.MKXError('Texture already exists with different contents: %s. Rename the new texture.' % dst)
        else:
            shutil.copyfile(source, dst)


def guess_textures(slots, images, model_name, ws=None):
    result = {s['texture']: KEEP for s in slots}
    stem = Path(model_name or 'unnamed').stem
    manifest = []
    receipt = Path(ws) / 'textures' / stem / 'import_manifest.json' if ws else None
    if receipt and receipt.exists():
        manifest = json.loads(receipt.read_text(encoding='utf-8'))
    words = {'DiffuseMap': ('diff', 'albedo', 'basecolor', 'base_color'), 'NormalMap': ('norm', 'nrm'), 'Pmsk': ('pmsk',)}
    primary = {}
    for slot in slots:
        if slot['replaceable'] and (slot['param'] not in primary or len(slot['slots']) > len(primary[slot['param']]['slots'])):
            primary[slot['param']] = slot
    for s in slots:
        if not s['replaceable']:
            continue
        candidates = {m['file'] for m in manifest if m['role'] == s['param'] and m['slot'] in s['slots'] and m['file'] in images}
        if not candidates and primary.get(s['param']) is s:
            candidates = {im for im in images if Path(im).parts[0].lower() == stem.lower()
                          and any(w in Path(im).name.lower() for w in words.get(s['param'], ())) }
        # Multiple custom materials sharing one MKX texture need an atlas, not an arbitrary first image.
        if len(candidates) == 1:
            result[s['texture']] = next(iter(candidates))
    return result


def apply_dds(p, texture, path, log=print):
    t = mk.read_texture(p, p.find_export(texture, 'Texture2D'))
    data = Path(path).read_bytes()
    if len(data) < 148 or data[:4] != b'DDS ' or data[84:88] != b'DX10':
        raise mk.MKXError('DDS must be BC7 with a DX10 header. Use a PNG for automatic conversion.')
    h, w = struct.unpack_from('<II', data, 12)
    count = struct.unpack_from('<I', data, 28)[0]
    fmt, dimension, misc, array, _ = struct.unpack_from('<5I', data, 128)
    if fmt not in (98, 99) or dimension != 3 or misc & 4 or array != 1 or t['fmt'] != 22:
        raise mk.MKXError('DDS must be a single 2D BC7 texture.')
    if (w, h, count) != (t['mips'][0]['w'], t['mips'][0]['h'], len(t['mips'])):
        raise mk.MKXError('DDS size and mip count must match the target. Use PNG to resize and generate mips automatically.')
    if any(m['data_pos'] is None for m in t['mips']) or len(data) != 148 + sum(m['size'] for m in t['mips']):
        raise mk.MKXError('DDS data length is wrong, or target uses external streamed textures.')
    offset = 148
    for m in t['mips']:
        p.image[m['data_pos']:m['data_pos'] + m['size']] = data[offset:offset + m['size']]
        offset += m['size']
    log('Copied BC7 DDS mip chain directly: %s (alpha preserved; colour interpretation follows the target)' % texture)


def job_convert(ws, game_dir, pkg_name, mesh, model_file, tex_choice, opaque=True, uv1_from_model=False,
                force=False, preview=True, out_name=None, log=print):
    setup_workspace(ws, game_dir)
    model = child(child(ws, 'character'), model_file)
    if model.suffix.lower() != '.glb' or not model.is_file():
        raise mk.MKXError('Import a rigged GLB first.')
    name = safe_name(out_name or model.stem)
    lines = []
    def report(message):
        lines.append(str(message)); log(message)
    template = template_path(ws, game_dir, pkg_name, report)
    p = mk.load(template)
    slots = {s['texture']: s for s in mk.mesh_texture_slots(p, mesh)}
    for tex, source in tex_choice.items():
        if source and source != KEEP and (tex not in slots or not slots[tex]['replaceable']):
            raise mk.MKXError('Texture cannot be replaced: %s' % tex)
    report('Base: %s / %s; model: %s' % (pkg_name, mesh, model_file))
    mk.apply_mesh(p, mesh, str(model), log=report, uv1='model' if uv1_from_model else 'transfer', force=force)
    for tex, source in tex_choice.items():
        if not source or source == KEEP:
            continue
        image = SOLIDS.get(source) or str(child(child(ws, 'textures'), source))
        if image.lower().endswith('.dds'):
            apply_dds(p, tex, image, report)
        else:
            mk.apply_image(p, tex, image, opaque=opaque and slots[tex]['param'] == 'DiffuseMap', log=report)
    if not mk.verify_package(p, log=report):
        raise mk.MKXError('Converted data failed verification; no result was published.')
    with staged_output(ws, game_dir, 'converted', name) as (stage, final):
        out = stage / package_name(pkg_name)
        p.save(str(out))
        report('Reopening the saved package for verification...')
        saved = mk.load(str(out))
        if not mk.verify_package(saved, log=report):
            raise mk.MKXError('Saved package failed verification; no result was published.')
        if preview:
            mk.export_ref(saved, mesh, str(stage / (name + '_preview.glb')), embed_textures=True, log=report)
        metadata = {'template': pkg_name, 'template_sha256': sha256(template), 'model': model_file,
                    'model_sha256': sha256(model), 'output_sha256': sha256(out), 'textures': tex_choice,
                    'mesh': mesh, 'created': datetime.datetime.now().astimezone().isoformat(),
                    'options': {'opaque': opaque, 'uv1_from_model': uv1_from_model, 'force': force},
                    'verification': 'saved package reparsed; in-game behavior not verified'}
        (stage / 'build.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        report('Verified result: %s' % (final / pkg_name))
        (stage / 'conversion_log.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(final)
