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
import subprocess
import sys
import tempfile

import mkx_meshmod as mk

HERE = Path(__file__).resolve().parent
APP = 'MKX Character Studio'
KEEP, FLAT, NEUTRAL = '(keep original)', '(flat normal map)', '(neutral mask)'
# Working body-channel interpretation (CHAR_Costume/Skin/Metal and Glow/Opacity variants).
# Exact channel mapping and thresholds still need traceable shader/in-game validation.
#   R  interpreted wounds allowed: 0 off, 127 full (Costume materials only)
#   G  interpreted glow strength (Glow materials only)
#   B  albedo tint: 0 PRIMARY_AlbedoTint, 127 near-neutral, 255 ALTERNATE_AlbedoTint (values between blend)
#   A  material settings: 0 PRIMARY_, 255 ALTERNATE_ (roughness, specularity, metallic, subsurface, detail normal)
PMSK_NEUTRAL = (127, 0, 127, 0)
SOLIDS = {FLAT: 'solid:128,128,255,160', NEUTRAL: 'solid:%d,%d,%d,%d' % PMSK_NEUTRAL}
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
    p = mk.load(template_path(ws, game_dir, mesh_homes(ws, game_dir, pkg_name, mesh, log)[0], log))
    m = mk.SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
    return dict(slots=mk.mesh_texture_slots(p, mesh), materials=[x.split('.')[-1] for x in m.materials()])


# ----------------------------------------------------------------------------------------------- extra objects
# Hats, weapons and other props are separate SkeletalMeshes that the game spawns and attaches to the character. They
# are not in CHAR_<name>_<costume>.xxx but in the script-asset packages (same naming for the whole cast):
#   TRAIT_<name><n>_ScriptAssets.xxx        props of variation n, shared by every costume of the character
#   Char_<name>_<costume>_ScriptAssets.xxx  props used in every variation (moves, specials) for this costume
#   UI_PS_<NAME>_SCRIPTASSETS.xxx           copies shown on the variation select screen
def object_packages(ws, game_dir, pkg_name):
    """{package name: path} of the packages that hold this character's extra objects (workspace copies first)."""
    m = re.match(r'(?i)^char_(.+)_([^_]+)\.xxx$', pkg_name)
    if not m:
        return {}
    code, skin = map(re.escape, m.groups())
    rx = re.compile(r'(?i)^(char_%s_%s_scriptassets|trait_%s\d*_scriptassets|ui_ps_%s_scriptassets)\.xxx$' % (code, skin, code, code))
    found = {}
    for folder in (Path(game_dir) / 'Asset' if game_dir else None, child(ws, 'vanilla_cache')):
        if folder and folder.is_dir():
            for f in folder.glob('*.xxx'):
                if rx.match(f.name):
                    found[f.name] = f
    return found


def _object_where(pkg):
    low = pkg.lower()
    m = re.match(r'trait_.*?(\d+)_scriptassets', low)
    if m:
        return 'variation %s' % m.group(1)
    if low.startswith('ui_ps_'):
        return 'select screen'
    return 'every variation' if low.endswith('_scriptassets.xxx') else 'costume'


def object_label(path, packages):
    name = re.sub(r'(?i)_(sk|st)$', '', path.split('.')[-1])
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', re.sub(r'^[A-Z]{2}_', '', name).replace('_', ' '))
    where = sorted({_object_where(p) for p in packages}, key=lambda w: (w == 'select screen', w))
    return '%s  (%s)' % (name, ', '.join(where))


_MESH_LISTS = {}


def package_meshes(path):
    """SkeletalMesh paths in a package file, remembered while the file is unchanged (listing reads the whole package)."""
    st = os.stat(path); key = (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
    if key not in _MESH_LISTS:
        _MESH_LISTS[key] = mk.list_skeletal_meshes(mk.load(str(path)))
    return _MESH_LISTS[key]


def mesh_homes(ws, game_dir, pkg_name, mesh, log=print):
    """Names of the packages that hold `mesh`: the character package, or every package with that extra object
    (gameplay packages first, then the select-screen copy)."""
    if mesh.lower() in {m.lower() for m in package_meshes(template_path(ws, game_dir, pkg_name, log))}:
        return [pkg_name]
    homes = [name for name, path in sorted(object_packages(ws, game_dir, pkg_name).items(),
                                           key=lambda kv: (kv[0].lower().startswith('ui_ps_'), kv[0].lower()))
             if mesh.lower() in {m.lower() for m in package_meshes(path)}]
    if not homes:
        raise mk.MKXError('%s was not found for this character.' % mesh)
    return homes


def job_find_objects(ws, game_dir, pkg_name, mesh, log=print):
    """The character's extra objects (hats, weapons...): [{path, label, packages}]. `mesh` (the main mesh) is left out."""
    objects = {}
    def add(path, pkg):
        objects.setdefault(path.lower(), dict(path=path, packages=[]))['packages'].append(pkg)
    for m in package_meshes(template_path(ws, game_dir, pkg_name, log)):
        if m.lower() != mesh.lower() and not m.lower().startswith('xray.'):
            add(m, pkg_name)
    for name, path in sorted(object_packages(ws, game_dir, pkg_name).items()):
        for m in package_meshes(path):
            if m.lower().startswith('characters.') and '.npc.' not in m.lower():
                add(m, name)
    labels = {}
    for o in objects.values():
        o['label'] = object_label(o['path'], o['packages'])
        labels[o['label']] = labels.get(o['label'], 0) + 1
    for o in objects.values():                                   # two objects with the same short name: show the path
        if labels[o['label']] > 1:
            o['label'] += '  ' + o['path']
    return sorted(objects.values(), key=lambda o: o['label'].lower())


def job_convert_targets(ws, game_dir, pkg_name, log=print):
    """Everything the Convert tab can replace: the main mesh first, then the extra objects, then any other mesh."""
    r = job_load_package(ws, game_dir, pkg_name, log)
    objects = job_find_objects(ws, game_dir, pkg_name, r['default'], log)
    targets = [dict(path=r['default'], label=r['default'])] + [dict(path=o['path'], label=o['label']) for o in objects]
    seen = {t['path'].lower() for t in targets}
    targets += [dict(path=m, label=m) for m in r['meshes'] if m.lower() not in seen]
    return dict(targets=targets, objects=objects, default=r['default'])


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


def _blender_version(path):
    nums = re.findall(r'\d+', Path(path).parent.name)
    return tuple(int(n) for n in nums) if nums else (0,)


def steam_libraries():
    roots = [Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Steam']
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            roots.insert(0, Path(winreg.QueryValueEx(key, 'SteamPath')[0]))
    except (ImportError, OSError):
        pass
    libs = []
    for root in roots:
        libs.append(root)
        try:
            vdf = (root / 'steamapps' / 'libraryfolders.vdf').read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        libs += [Path(m.replace('\\\\', '\\')) for m in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    return libs


def find_blender(game_dir='', configured=''):
    """Path of blender.exe: the chosen one, then PATH, Steam libraries and the standard install folder; '' if none."""
    found = [Path(configured)] if configured else []
    if os.environ.get('MKX_BLENDER'):
        found.append(Path(os.environ['MKX_BLENDER']))
    if shutil.which('blender'):
        found.append(Path(shutil.which('blender')))
    steam = ([Path(game_dir).parent] if game_dir else []) + [lib / 'steamapps' / 'common' for lib in steam_libraries()]
    found += [folder / 'Blender' / 'blender.exe' for folder in steam]
    for env in ('ProgramFiles', 'ProgramW6432'):
        if os.environ.get(env):
            found += sorted((Path(os.environ[env]) / 'Blender Foundation').glob('*/blender.exe'), key=_blender_version, reverse=True)
    return next((str(path) for path in found if path.is_file()), '')


def make_blend(glb, out, blender, log=print):
    """Run Blender in the background to turn an exported .glb into a .blend whose bones point at their children."""
    cmd = [blender, '-b', '--factory-startup', '--python-exit-code', '1', '--python', str(HERE / 'blender_rig.py'),
           '--', str(glb), str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise mk.MKXError('Blender could not run: %s' % exc)
    done = next((line for line in r.stdout.splitlines() if line.startswith('MKX_BLEND_OK')), None)
    if r.returncode or not done or not Path(out).is_file():
        tail = [line for line in (r.stdout + r.stderr).splitlines() if line.strip()][-8:]
        raise mk.MKXError('Blender could not build the .blend:\n  ' + '\n  '.join(tail))
    log('wrote %s: %s' % (out, done.split(' ', 1)[1]))


def _export_mesh(p, mesh, folder, game_dir, textures, embed, blend, blender, log):
    """One mesh as .glb (+ .blend, textures and slots_and_textures.txt) into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    glb = folder / (safe_name(mesh.split('.')[-1]) + '.glb')
    mk.export_ref(p, mesh, str(glb), embed_textures=embed, log=log)
    made_blend = False
    if blend:
        exe = find_blender(game_dir, blender)
        if not exe:
            log('Blender was not found, so no .blend was made. Choose blender.exe (Blender... in the app, --blender on the command line) and export again.')
        else:
            log('Building %s with %s...' % (glb.with_suffix('.blend').name, exe))
            try:
                make_blend(glb, glb.with_suffix('.blend'), exe, log)
                made_blend = True
            except mk.MKXError as exc:
                log('WARNING: %s' % exc)
    slots = mk.mesh_texture_slots(p, mesh)
    if textures:
        mk.export_textures(p, str(folder / 'textures'), only={s['texture'] for s in slots}, log=log)
    m = mk.SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
    lines = ['Blender materials (retain slotNN_ prefixes):']
    lines += ['slot%02d_%s' % (i, x.split('.')[-1]) for i, x in enumerate(m.materials())]
    lines += ['', 'Textures:'] + [json.dumps(s) for s in slots]
    lines += ['', 'Only inline BC7 textures are supported. Streamed .tfc textures and external material references are not exported.',
              'The GLB embeds diffuse textures only. DDS/PNG files preserve other maps.',
              'Keep the exported skeleton, scale and rest pose. This replaces LOD0; it does not generate fatality meshes.']
    if made_blend:
        lines += ['', '%s is the same model ready to open in Blender, with each bone pointing at its child.' % glb.with_suffix('.blend').name,
                  'Only the bone directions differ from the .glb; joint positions and the mesh are identical, so either one can be the base.']
    (folder / 'slots_and_textures.txt').write_text('\n'.join(lines), encoding='utf-8')


def job_export_vanilla(ws, game_dir, pkg_name, mesh, textures=True, embed=True, blend=True, blender='', objects=(), log=print):
    """Export `mesh`, plus the chosen extra objects (paths from job_find_objects) into objects\\<name>\\."""
    if embed or textures:
        from PIL import Image  # Fail before writing if PNG support is missing.
    p = mk.load(template_path(ws, game_dir, pkg_name, log))
    homes = {o: mesh_homes(ws, game_dir, pkg_name, o, log)[0] for o in objects}
    with staged_output(ws, game_dir, 'vanilla_exports', Path(pkg_name).stem + '_' + mesh.split('.')[-1]) as (stage, final):
        _export_mesh(p, mesh, stage, game_dir, textures, embed, blend, blender, log)
        for o, home in homes.items():
            log('Exporting extra object %s (from %s)...' % (o.split('.')[-1], home))
            q = p if home == pkg_name else mk.load(template_path(ws, game_dir, home, log))
            _export_mesh(q, o, stage / 'objects' / safe_name(o.split('.')[-1]), game_dir, textures, embed, blend, blender, log)
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


# Diffuse alpha is the character's baked shading (ambient occlusion: it darkens creases and blocks reflections).
# MKX's own maps are about 240-255 on open surfaces and darker only in creases (median 174-209); an image without its
# own alpha gets this middle value instead of a fully lit 255.
DIFFUSE_SHADING = 210
# MKX diffuse textures average 58-72 brightness (0-255); art brighter than BRIGHT_LIMIT is darkened to BRIGHT_TARGET
# (kept above MKX's own average: stylised textures have no baked shading to darken them).
BRIGHT_LIMIT, BRIGHT_TARGET = 120, 110
UV2_CHOICES = {'auto': "Your model's 2nd UV map, or make one if it has none",
               'generate': 'Always make a new one',
               'transfer': 'Copy from the original character (only for edits of the original mesh)'}


def prepare_diffuse(image, darken=True, log=print):
    """A diffuse image ready for MKX: its own alpha kept as shading if it has one, otherwise DIFFUSE_SHADING; colours
    darkened to BRIGHT_TARGET when they are much brighter than MKX textures (and `darken` is on)."""
    import numpy as np
    from PIL import Image
    im = Image.open(image) if isinstance(image, (str, Path)) else image
    px = np.asarray(im.convert('RGBA')).astype(np.float64)
    if px[..., 3].min() >= 250:
        px[..., 3] = DIFFUSE_SHADING
        log("diffuse has no alpha of its own: shading (alpha) set to %d, the middle of MKX's own shading maps" % DIFFUSE_SHADING)
    mean = px[..., :3].mean()
    if darken and mean > BRIGHT_LIMIT:
        px[..., :3] *= BRIGHT_TARGET / mean
        log('diffuse is much brighter than MKX textures (average %.0f): darkened by %.0f%% to %d' % (
            mean, 100 * (1 - BRIGHT_TARGET / mean), BRIGHT_TARGET))
    return Image.fromarray(np.clip(np.rint(px), 0, 255).astype(np.uint8), 'RGBA')


def wrinkle_mask_textures(p):
    """Inline face-wrinkle mask textures (the WrinkleMasks parameter of the cinematic skin materials)."""
    found = set()
    for i, e in enumerate(p.exports):
        if p.classname(e['Class']) == 'MaterialInstanceConstant':
            tid = mk.mic_texture_params(p, i + 1).get('WrinkleMasks')
            if tid and tid > 0:
                found.add(p.objref(tid))
    return sorted(found)


def job_convert(ws, game_dir, pkg_name, mesh, model_file, tex_choice, extra_parts=(), log=print, **options):
    """Replace `mesh` with `model_file` (and any extra_parts: [{mesh, model, textures}]); see job_convert_parts."""
    parts = [dict(mesh=mesh, model=model_file, textures=tex_choice or {})] if mesh and model_file else []
    return job_convert_parts(ws, game_dir, pkg_name, parts + [dict(x) for x in extra_parts], log=log, **options)


def job_convert_parts(ws, game_dir, pkg_name, parts, uv2='auto', darken=True, keep_size=False, wrinkles_off=True,
                      force=False, preview=True, out_name=None, hide=(), log=print):
    """Replace every mesh in `parts` ([{mesh, model, textures: {texture: image}}]) with its own .glb and hide the
    objects in `hide`, in the character package and in every other package that holds them. Writes only the packages
    that changed into one result folder."""
    setup_workspace(ws, game_dir)
    hide = sorted(set(hide or ()))
    if not parts and not hide:
        raise mk.MKXError('Link a .glb to at least one mesh first (or untick an object to hide).')
    models, seen = {}, set()
    for part in parts:
        model = child(child(ws, 'character'), part['model'])
        if model.suffix.lower() != '.glb' or not model.is_file():
            raise mk.MKXError('Import a rigged GLB first (%s).' % part['model'])
        if part['mesh'].lower() in seen:
            raise mk.MKXError('%s has two models linked to it.' % part['mesh'])
        seen.add(part['mesh'].lower()); models[part['mesh']] = model
    both = [h for h in hide if h.lower() in seen]
    if both:
        raise mk.MKXError('These objects are set to be replaced and hidden; keep one: %s' % ', '.join(both))
    name = safe_name(out_name or (models[parts[0]['mesh']].stem if parts else Path(pkg_name).stem))
    lines = []

    def report(message):
        lines.append(str(message)); log(message)
    pkgs, changed, applied = {}, [], {}

    def load(home):
        if home not in pkgs:
            pkgs[home] = mk.load(template_path(ws, game_dir, home, report))
        return pkgs[home]

    def touched(home):
        if home not in changed: changed.append(home)
    main = load(pkg_name)
    main_mesh = default_mesh(mk.list_skeletal_meshes(main), pkg_name)
    homes = {}
    for part in parts:
        mesh, tex_choice = part['mesh'], part.get('textures') or {}
        homes[mesh] = mesh_homes(ws, game_dir, pkg_name, mesh, report)
        for n, home in enumerate(homes[mesh]):
            p = load(home)
            slots = {s['texture']: s for s in mk.mesh_texture_slots(p, mesh)}
            for tex, source in tex_choice.items():
                if n == 0 and source and source != KEEP and (tex not in slots or not slots[tex]['replaceable']):
                    raise mk.MKXError('Texture cannot be replaced: %s' % tex)
            report('Base: %s / %s; model: %s' % (home, mesh, part['model']))
            mk.apply_mesh(p, mesh, str(models[mesh]), log=report, uv1=uv2, force=force)
            touched(home)
            for tex, source in tex_choice.items():
                if not source or source == KEEP or tex not in slots or not slots[tex]['replaceable']:
                    continue
                if (home, tex) in applied:                      # a texture shared by two replaced meshes
                    if applied[home, tex] != source:
                        raise mk.MKXError('%s is shared by two meshes, but they have different images (%s, %s). '
                                          'Use one image for both.' % (tex.split('.')[-1], applied[home, tex], source))
                    continue
                applied[home, tex] = source
                image = SOLIDS.get(source) or str(child(child(ws, 'textures'), source))
                if image.lower().endswith('.dds'):
                    apply_dds(p, tex, image, report)
                else:
                    if slots[tex]['param'] == 'DiffuseMap' and not image.startswith('solid:'):
                        image = prepare_diffuse(image, darken, report)
                    mk.apply_image(p, tex, image, log=report, keep_size=keep_size)
    if wrinkles_off and main_mesh in models:
        # The original character's face-wrinkle zones are painted for its own face layout; on a new model they put
        # the original's wrinkle bumps in random places during intros and close-ups. Black masks turn them off.
        for tex in wrinkle_mask_textures(main):
            try:
                mk.apply_image(main, tex, 'solid:0,0,0,255', log=lambda m: None)
                report('turned off the original face wrinkles: %s' % tex)
            except mk.MKXError as exc:
                report('WARNING: could not turn off face wrinkles in %s: %s' % (tex, exc))
    for h in hide:
        if h.lower() == main_mesh.lower():
            raise mk.MKXError('The main character mesh cannot be hidden.')
        for home in mesh_homes(ws, game_dir, pkg_name, h, report):
            where = '' if home == pkg_name else ' in %s' % home
            report('hid %s%s (%d triangles)' % (h.split('.')[-1], where, mk.hide_skeletal_mesh(load(home), h)))
            touched(home)
    for home in changed:
        if not mk.verify_package(pkgs[home], log=lambda m: None):
            mk.verify_package(pkgs[home], log=report)
            raise mk.MKXError('%s failed verification; no result was published.' % home)
    shared = [h for h in changed if not h.lower().startswith('char_')]
    if shared:
        report('NOTE: %s %s shared by every costume of this character, so these changes show on all of them.'
               % (', '.join(shared), 'is' if len(shared) == 1 else 'are'))
    with staged_output(ws, game_dir, 'converted', name) as (stage, final):
        saved = {}
        for home in changed:
            out = stage / package_name(home)
            pkgs[home].save(str(out))
            report('Reopening %s for verification...' % home)
            saved[home] = mk.load(str(out))
            if not mk.verify_package(saved[home], log=report):
                raise mk.MKXError('Saved %s failed verification; no result was published.' % home)
            report('Verified result: %s' % (final / home))
        if preview:
            for part in parts:
                mesh = part['mesh']
                stem = name if mesh == main_mesh else '%s_%s' % (name, safe_name(mesh.split('.')[-1]))
                mk.export_ref(saved[homes[mesh][0]], mesh, str(stage / (stem + '_preview.glb')), embed_textures=True, log=report)
        if len(changed) > 1 or changed[0] != pkg_name:
            report("Copy every .xxx file in this folder into the game's Asset folder (keep backups of the originals).")
        metadata = {'character': pkg_name, 'created': datetime.datetime.now().astimezone().isoformat(),
                    'parts': [dict(mesh=pt['mesh'], model=pt['model'], model_sha256=sha256(models[pt['mesh']]),
                                   textures=pt.get('textures') or {}, packages=homes[pt['mesh']]) for pt in parts],
                    'hidden_objects': hide,
                    'packages': {h: dict(template_sha256=sha256(template_path(ws, game_dir, h, lambda m: None)),
                                         output_sha256=sha256(stage / h)) for h in changed},
                    'options': {'uv2': uv2, 'darken': darken, 'keep_size': keep_size, 'wrinkles_off': wrinkles_off, 'force': force},
                    'verification': 'saved packages reparsed; in-game behavior not verified'}
        (stage / 'build.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        (stage / 'conversion_log.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(final)


# ----------------------------------------------------------------------------------------------- Pmsk maker
# One greyscale image per Pmsk channel. An empty channel gets the neutral value from PMSK_NEUTRAL.
PMSK_LAYERS = [
    ('wounds', 'Wound mask (red)', 'Interpreted cloth/leather wound control. Black = off; white = allowed. White packs as red 127.'),
    ('glow', 'Glow mask (green)', 'Interpreted glow strength for glow materials: black = off, white = full.'),
    ('tint', 'Tint mask (blue)', 'Black = primary tint; near middle grey = original colour; white = alternate tint. Intermediate values blend.'),
    ('surface', 'Surface blend (alpha)', 'Black = primary settings, white = alternate settings; intermediate values blend in the working interpretation.'),
]
PMSK_RAW_LAYERS = [(key, colour.title() + ' channel', 'Raw grayscale values, packed unchanged; image transparency is ignored.')
                   for key, colour in zip(('wounds', 'glow', 'tint', 'surface'), ('red', 'green', 'blue', 'alpha'))]


def mask_array(image, size=None):
    """A greyscale image as a 0..1 float array (resized to `size` if given); transparent pixels count as black."""
    import numpy as np
    from PIL import Image
    im = Image.open(image) if isinstance(image, (str, Path)) else image
    im.load()
    rgba = im.convert('RGBA')
    lum, alpha = rgba.convert('RGB').convert('L'), rgba.getchannel('A')
    if size and lum.size != tuple(size):
        lum, alpha = lum.resize(size, Image.BILINEAR), alpha.resize(size, Image.BILINEAR)
    return np.asarray(lum, np.float64) * np.asarray(alpha, np.float64) / (255.0 * 255.0)


def compose_pmsk(layers, size=None, raw=False):
    """Pack editing masks, or four equally sized raw grayscale channels without rescaling their values."""
    import numpy as np
    from PIL import Image
    layers = {k: v for k, v in (layers or {}).items() if v is not None and v != ''}
    unknown = set(layers) - {k for k, _, _ in PMSK_LAYERS}
    if unknown:
        raise mk.MKXError('Unknown Pmsk layer: %s' % ', '.join(sorted(unknown)))
    if raw:
        keys = [k for k, _, _ in PMSK_LAYERS]
        if set(layers) != set(keys):
            raise mk.MKXError('Raw mode requires all four channel images: red, green, blue and alpha.')
        channels = []
        for key in keys:
            value = layers[key]
            im = Image.open(value) if isinstance(value, (str, Path)) else value
            im.load()
            if im.mode not in ('1', 'L', 'LA', 'RGB', 'RGBA', 'P'):
                raise mk.MKXError('Raw channels must be 8-bit grayscale images (or grayscale RGB images).')
            if size is None:
                size = im.size
            if im.size != tuple(size):
                raise mk.MKXError('Raw channel images must have identical dimensions; raw mode never resizes them.')
            rgb = np.asarray(im.convert('RGB'))
            if not (np.array_equal(rgb[..., 0], rgb[..., 1]) and np.array_equal(rgb[..., 0], rgb[..., 2])):
                raise mk.MKXError('Raw channel images must be grayscale, not coloured images.')
            channels.append(im.convert('L'))
        return Image.merge('RGBA', channels)
    if not size:
        first = next((layers[k] for k, _, _ in PMSK_LAYERS if k in layers), None)
        if isinstance(first, (str, Path)):
            with Image.open(first) as im:
                size = im.size
        else:
            size = first.size if first is not None else (2048, 2048)
    size = tuple(size)
    out = np.empty((size[1], size[0], 4))
    out[:] = PMSK_NEUTRAL
    scale = {'wounds': 127, 'glow': 255, 'tint': 255, 'surface': 255}     # normalized body-editing convention
    for channel, (key, _, _) in enumerate(PMSK_LAYERS):
        if key in layers:
            out[..., channel] = scale[key] * mask_array(layers[key], size)
    return Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8), 'RGBA')


def job_make_pmsk(ws, game_dir, name, layers, log=print, raw=False):
    """Make a Pmsk image and save it as textures/<name>/<name>_Pmsk.png (tab 2 suggests it for a model of that name)."""
    setup_workspace(ws, game_dir)
    name = safe_name(name)
    image = compose_pmsk(layers, raw=raw)
    folder = child(ws, 'textures', name); guard_output(folder, game_dir); folder.mkdir(exist_ok=True)
    out = child(folder, name + '_Pmsk.png'); guard_output(out, game_dir)
    existed = out.exists()
    image.save(out)
    log('%s %s (%dx%d)' % ('Replaced' if existed else 'Wrote', out, image.size[0], image.size[1]))
    return str(out)


# ----------------------------------------------------------------------------------------------- Pmsk extraction
PMSK_HELP = [
    'Pmsk packs four grayscale data channels. Their meaning depends on the assigned material.',
    'Body layout below is a working interpretation, not a verified universal shader specification.',
    'Material parameters and _AP swapping are corroborated by asset/code inspection; exact channel mappings,',
    'thresholds and blending still need traceable shader evidence and in-game validation.',
    '  Red   = interpreted wound control for Costume (cloth/leather) materials.',
    '  Green = interpreted glow strength for Glow materials.',
    '  Blue  = recolour groups. Black pixels are recolour group 1, white pixels are group 2, middle grey keeps the texture',
    '          colour approximately. Intermediate values blend tint influence in this interpretation.',
    '          Supported body materials store two tint settings (x1 = unchanged).',
    '          The default look usually leaves both unchanged; the alternate palette and the variations set real colours.',
    '  Alpha = interpreted blend between primary and alternate surface settings (roughness, specularity, metalness,',
    '          subsurface and detail pattern). Black = primary, white = alternate; gray = intermediate blend.',
    'Raw exports preserve all decoded RGBA values of the largest stored mip, including colour under zero alpha.',
    'Use the four *_raw_<channel>.png images in tab 3 Raw channels mode for exact decoded-pixel reconstruction.',
    'Body *_edit_* masks are normalized for editing: red is stretched/clipped from 0..127 to 0..255.',
    'Editing mode packs white in the wound mask as red 127; it does not preserve original red values above 127.',
    'Raw PNGs do not preserve original compressed texture bytes, other mips or streamed higher resolutions.',
    'Previews are approximate diagnostics, not the final game render or proof of damage/glow behavior.',
    'Looks:',
    '  Alternate palette: the game uses the "_AP" material when one exists, otherwise keeps the original',
    '                     (this is what makes the second copy look different when both players pick the same character).',
    '  Variation <name>:  the material set the game uses for that variation.',
]


def _pmsk_sheet(panels, columns=3, label_height=34):
    from PIL import Image, ImageDraw, ImageFont
    w, h = panels[0][0].size
    rows = (len(panels) + columns - 1) // columns
    sheet = Image.new('RGB', (w * min(columns, len(panels)), (h + label_height) * rows), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype('arial.ttf', max(12, label_height * 2 // 3))
    except OSError:
        font = ImageFont.load_default()
    for k, (image, label) in enumerate(panels):
        x, y = (k % columns) * w, (k // columns) * (h + label_height)
        sheet.paste(image, (x, y + label_height))
        draw.text((x + 8, y + 6), label, fill=(255, 255, 255), font=font)
    return sheet


def _pmsk_overview(pmsk, title, labels, tile=300):
    """One row: the Pmsk texture as stored (shown without its alpha) = its four channels."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font, big = ImageFont.truetype('arial.ttf', 20), ImageFont.truetype('arial.ttf', 26)
    except OSError:
        font = big = ImageFont.load_default()
    sheet = Image.new('RGB', (tile * 5 + 100, tile + 100), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 12), title, fill=(255, 220, 120), font=big)
    sheet.paste(pmsk.convert('RGB').resize((tile, tile)), (20, 55))
    draw.text((20, tile + 62), 'the texture in the game', fill=(200, 200, 200), font=font)
    draw.text((tile + 32, 55 + tile // 2 - 16), '=', fill=(255, 255, 255), font=big)
    for k, label in enumerate(labels):
        x = tile + 70 + k * (tile + 6)
        sheet.paste(pmsk.getchannel(k).resize((tile, tile)).convert('RGB'), (x, 55))
        draw.text((x, tile + 62), label, fill=(200, 200, 200), font=font)
    return sheet


def _finish_text(values, prefix):
    def num(key):
        v = values.get(prefix + key)
        return '-' if v is None else '%.2f' % v
    detail = (values.get(prefix + 'DetailNormalMap') or '').split('.')[-1] or 'none'
    return ('roughness %s, specularity %s, metalness %s, subsurface %s, detail pattern %s (strength %s, repeats %s)'
            % (num('Roughness'), num('Specularity'), num('Metallic'), num('Subsurface'), detail,
               num('DetailNormal_Blend'), num('DetailNormal_Tiling')))


def job_extract_pmsk(ws, game_dir, pkg_name, mesh, log=print):
    """Export raw RGBA/channels, optional body masks, approximate previews and a report
    in vanilla_exports/<package>_<mesh>_Pmsk/."""
    import numpy as np
    from PIL import Image
    import mkx_pmsk as pm
    p = mk.load(template_path(ws, game_dir, pkg_name, log))
    m = mk.SkeletalMesh(p, p.find_export(mesh, 'SkeletalMesh'))
    targets = [s for s in mk.mesh_texture_slots(p, mesh) if s['param'].lower() in ('pmsk', 'psmk')]
    if not targets:
        raise mk.MKXError('%s uses no Pmsk texture.' % mesh)
    info = pm.MaterialInfo(str(Path(game_dir) / 'Asset' / 'Startup.xxx') if game_dir else '')
    looks = pm.mesh_looks(p, m)
    ids = looks[0][1]
    full = {'R': 'red (wound areas)', 'G': 'green (glow areas)', 'B': 'blue (recolour groups)', 'A': 'alpha (surface finish)'}
    lines = ['Pmsk breakdown: %s / %s' % (pkg_name, mesh), ''] + PMSK_HELP
    with staged_output(ws, game_dir, 'vanilla_exports', Path(pkg_name).stem + '_' + mesh.split('.')[-1] + '_Pmsk') as (stage, final):
        for t in targets:
            short = t['texture'].split('.')[-1]
            lines += ['', '=' * 100, '%s  (%dx%d)' % (short, t['size'][0], t['size'][1])]
            pmsk = pm.texture_image(p, t['texture'])
            if pmsk is None:
                lines.append('Cannot decode: no supported BC7 mip stored in this package (possibly streamed or another format).')
                continue
            slots = {}
            for slot in t['slots']:
                values, base = info.resolve(p, ids[slot])
                slots[slot] = (values, base, pm.material_family(base))
            body = all(f in pm.FAMILY_CHANNELS for _, _, f in slots.values())
            others = [f for _, _, f in slots.values() if f in pm.OTHER_LAYOUTS]
            labels = [l for _, _, l in pm.CHANNELS] if body else (
                max((pm.OTHER_LAYOUTS[f][1] for f in others), key=lambda n: sum(x != 'unused' for x in n)) if others
                else ('', '', '', ''))
            if pmsk.size != tuple(t['size']):
                lines.append('The largest mip stored in this package is %dx%d; that size is used. Streamed mips are not read.' % pmsk.size)
            files = pm.export_channel_images(pmsk, stage, short, body)
            lines.append('Decoded RGBA texture: ' + files['rgba'])
            lines.append('Raw channels (tab 3 Raw channels mode; all four required): ' + ', '.join(files['raw'].values()))
            if body:
                lines.append('Normalized body editing masks (tab 3 Body editing masks mode; red is rescaled/clipped): ' +
                             ', '.join(files['editing'].values()))
            else:
                lines.append('Body editing mode does not describe this layout. Use raw channels to preserve its values.')
            kinds = sorted({pm.FAMILY_TEXT.get(f, f or 'unknown') for _, _, f in slots.values()})
            _pmsk_overview(pmsk, '%s: the Pmsk of the %s materials' % (short, ', '.join(kinds)),
                           [('%s: %s' % (c, l.replace('_', ' ')) if l else c) for (_, c, _), l in zip(pm.CHANNELS, labels)]
                           ).save(stage / ('%s_overview.png' % short))
            lines.append('%s_overview.png: decoded RGB and raw channel values; labels are material-dependent interpretations.' % short)
            if body:
                size = (min(1024, pmsk.size[0]), min(1024, pmsk.size[1]))
                px = np.asarray(mk.resize_channels(pmsk, size, Image.BILINEAR) if pmsk.size != size else pmsk).astype(np.float64) / 255
                dpath = slots[t['slots'][0]][0].get('DiffuseMap')
                diffuse = pm.texture_image(p, dpath) if dpath else None
                diff = (np.asarray(diffuse.convert('RGB').resize(size, Image.BILINEAR)).astype(np.float64) / 255
                        if diffuse is not None else np.full((size[1], size[0], 3), 0.5))
                smap = pm.slot_map(m.lod, size, t['slots'])
                mine = np.isin(smap, [s + 1 for s in t['slots']])
                reads = {c: np.isin(smap, [s + 1 for s, (_, _, f) in slots.items() if c in pm.FAMILY_CHANNELS[f]]) for c in 'RGBA'}
                stripes = (np.add.outer(np.arange(size[1]), np.arange(size[0])) // 6) % 2 == 0

                def panel(c, mask, colour, label):
                    out = diff * 0.45
                    out[~mine] *= 0.35
                    ignored = mine & ~reads[c]
                    out[ignored] = np.where(stripes[ignored][:, None], 0.32, 0.22)
                    hit = mask & reads[c]
                    out[hit] = out[hit] * 0.3 + np.array(colour) * 0.7
                    return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)), label
                r, g, b, a = (px[..., k] for k in range(4))
                _pmsk_sheet([(Image.fromarray((diff * 255).astype(np.uint8)), 'Texture'),
                             panel('R', r < 0.25, (1, 0.25, 0.25), 'Low red (<25%)'),
                             panel('G', g >= 0.02, (0.3, 1, 0.3), 'Green >=2%'),
                             panel('B', b < 0.25, (1, 0.62, 0), 'Low blue (<25%)'),
                             panel('B', b >= 0.75, (0.25, 0.55, 1), 'High blue (>=75%)'),
                             panel('A', a >= 0.5, (1, 0.3, 1), 'Alpha >=50%')]
                            ).save(stage / ('%s_channel_map.png' % short))
                lines.append('%s_channel_map.png: thresholded diagnostic only. These display thresholds are not shader '
                             'cutoffs. Striped = interpreted unused channel; dark = outside mapped slots.' % short)
                previews, seen = [], set()
                for look, mats in looks:
                    tints = {s: (pm.tint_of(info.resolve(p, mats[s])[0], 1), pm.tint_of(info.resolve(p, mats[s])[0], 2))
                             for s in t['slots'] if mats[s] > 0}
                    key = tuple(sorted(tints.items()))
                    if key in seen:
                        continue
                    seen.add(key)
                    img = pm.recolour(diff, b, smap, tints)
                    img[~mine] *= 0.35
                    previews.append((Image.fromarray((img * 255).astype(np.uint8)), look))
                _pmsk_sheet(previews).save(stage / ('%s_recolour_previews.png' % short))
                lines.append('%s_recolour_previews.png: approximate tint previews, without game lighting (%s). Looks with the '
                             'same colours as an earlier one are left out.' % (short, ', '.join(l for _, l in previews)))
            lines += ['', 'Material slots using %s:' % short]
            for slot, (values, base, family) in sorted(slots.items()):
                lines += ['', 'Slot %02d  %s  -  %s material (%s)' % (slot, p.objref(ids[slot]).split('.')[-1],
                                                                       pm.FAMILY_TEXT.get(family, family or 'unknown'), base)]
                if family not in pm.FAMILY_CHANNELS:
                    lines.append('    Interpreted Pmsk use: ' + (pm.OTHER_LAYOUTS[family][0] if family in pm.OTHER_LAYOUTS else
                                                     'not analysed by this tool (material type %s)' % (base or 'unknown')))
                    continue
                used = pm.FAMILY_CHANNELS[family]
                lines.append('    Interpreted channels: uses %s; unused %s.' % (', '.join(full[c] for c in used),
                                                         ', '.join(full[c] for c in 'RGBA' if c not in used) or 'nothing'))
                if 'G' in used:
                    glow = values.get('GLOW_Color') or (0, 0, 0)
                    follow = [n for k, n in (('UsePrimaryTintMaskForGlow', 'group 1'), ('UseAlternateTintMaskForGlow', 'group 2'))
                              if (values.get(k) or 0) > 0.5]
                    lines.append('    Glow colour x(%.2f, %.2f, %.2f)%s' % (tuple(glow[:3]) + (
                        "; in recolour %s areas the glow takes that group's colour instead" % ' and '.join(follow) if follow else '',)))
                lines.append('    Recolour colours, multiplied onto the texture (looks using the same material as a line above are left out):')
                shown = []
                for look, mats in looks:
                    if mats[slot] in shown:
                        continue
                    shown.append(mats[slot])
                    v = info.resolve(p, mats[slot])[0] if mats[slot] > 0 else values
                    look_name = '%s (%s)' % (look, p.objref(mats[slot]).split('.')[-1]) if mats[slot] > 0 else look
                    lines.append('      %-58s group 1: %-34s group 2: %s' % (
                        look_name, pm.describe_tint(*pm.tint_of(v, 1)), pm.describe_tint(*pm.tint_of(v, 2))))
                lines.append('    Surface finish 1 (alpha black): ' + _finish_text(values, 'PRIMARY_'))
                lines.append('    Surface finish 2 (alpha white): ' + _finish_text(values, 'ALTERNATE_'))
        (stage / 'pmsk_report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    log('Pmsk extracted: %s' % final)
    return str(final)
