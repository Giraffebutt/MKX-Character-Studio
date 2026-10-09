"""Safe command-line entry point: all writes use the same workspace rules as the GUI."""
import argparse
import json
import sys
import studio_core as core


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', required=True, help='Mod folder, outside the game or inside this edition/projects')
    p.add_argument('--game', default=core.detect_game_dir(), help='Read-only MK10 installation')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    s = sub.add_parser('add-original'); s.add_argument('package')
    s = sub.add_parser('import-glb'); s.add_argument('model')
    s = sub.add_parser('import-textures'); s.add_argument('images', nargs='+'); s.add_argument('--model', default='')
    s = sub.add_parser('meshes'); s.add_argument('package')
    for command in ('export', 'convert'):
        s = sub.add_parser(command); s.add_argument('package'); s.add_argument('mesh')
        if command == 'convert':
            s.add_argument('model', help='Imported GLB filename in character/')
            s.add_argument('--texture-map', help='JSON object mapping MKX texture paths to paths relative to textures/')
            s.add_argument('--name'); s.add_argument('--no-preview', action='store_true')
            s.add_argument('--uv2', choices=list(core.UV2_CHOICES), default='auto', help='second UV map (blood and damage)')
            s.add_argument('--no-darken', action='store_true', help='keep very bright diffuse textures as they are')
            s.add_argument('--keep-wrinkles', action='store_true', help="keep the original character's face wrinkles")
            s.add_argument('--keep-size', action='store_true', help="experimental: keep images' own size (power-of-two sides)")
            s.add_argument('--hide', action='append', default=[], metavar='OBJECT',
                           help='hide an extra object (hat, weapon...) in the game; repeat for more. See the objects command')
            s.add_argument('--extra', action='append', default=[], metavar='OBJECT=GLB',
                           help='also replace an extra object or other mesh with its own GLB; repeat for more. '
                                '--texture-map may hold textures of every replaced mesh')
        else:
            s.add_argument('--object', action='append', default=[], metavar='OBJECT',
                           help='also export an extra object (hat, weapon...); repeat for more. See the objects command')
            s.add_argument('--no-textures', action='store_true')
            s.add_argument('--no-blend', action='store_true', help='skip the .blend with bones pointing at their children')
            s.add_argument('--blender', default='', help='blender.exe to use (found automatically when omitted)')
    s = sub.add_parser('pmsk', help='make a Pmsk from greyscale images, saved as textures/<name>/<name>_Pmsk.png')
    s.add_argument('name')
    s.add_argument('--raw', action='store_true', help='pack four equally sized raw grayscale channels unchanged; '
                   '--red, --green, --blue and --alpha are all required')
    for (key, label, help_text), colour in zip(core.PMSK_LAYERS, ('red', 'green', 'blue', 'alpha')):
        s.add_argument('--' + key, '--' + colour, dest=key, default='',
                       help='%s image. In editing mode: %s' % (label, help_text))
    s = sub.add_parser('objects', help="list a character's extra objects (hats, weapons...) that convert --hide can hide")
    s.add_argument('package'); s.add_argument('mesh')
    s = sub.add_parser('extract-pmsk', help="break a game character's Pmsk down (images, previews and a report)")
    s.add_argument('package'); s.add_argument('mesh')
    a = p.parse_args()
    try:
        core.setup_workspace(a.workspace, a.game)
        if a.command == 'init': result = a.workspace
        elif a.command == 'add-original': result = core.add_original_package(a.workspace, a.package, game_dir=a.game)
        elif a.command == 'import-glb': result = core.import_model(a.workspace, a.model, a.game)
        elif a.command == 'import-textures': result = core.import_images(a.workspace, a.images, a.model, a.game)
        elif a.command == 'meshes': result = core.job_load_package(a.workspace, a.game, a.package)
        elif a.command == 'export': result = core.job_export_vanilla(a.workspace, a.game, a.package, a.mesh, textures=not a.no_textures,
                                                                       blend=not a.no_blend, blender=a.blender, objects=a.object)
        elif a.command == 'convert':
            mapping = json.loads(core.Path(a.texture_map).read_text(encoding='utf-8')) if a.texture_map else {}
            parts, used = [], set()
            for mesh, model in [(a.mesh, a.model)] + [x.split('=', 1) for x in a.extra]:
                slots = {s['texture'] for s in core.job_texture_slots(a.workspace, a.game, a.package, mesh)['slots']}
                parts.append(dict(mesh=mesh, model=model, textures={t: v for t, v in mapping.items() if t in slots}))
                used |= slots
            if set(mapping) - used:
                raise core.mk.MKXError('--texture-map names textures that none of the replaced meshes use: %s'
                                       % ', '.join(sorted(set(mapping) - used)))
            result = core.job_convert_parts(a.workspace, a.game, a.package, parts,
                                      out_name=a.name, preview=not a.no_preview, uv2=a.uv2, darken=not a.no_darken, keep_size=a.keep_size, wrinkles_off=not a.keep_wrinkles,
                                      hide=a.hide)
        elif a.command == 'pmsk':
            layers = {key: getattr(a, key) for key, _, _ in core.PMSK_LAYERS if getattr(a, key)}
            result = core.job_make_pmsk(a.workspace, a.game, a.name, layers, raw=a.raw)
        elif a.command == 'objects':
            objects = core.job_find_objects(a.workspace, a.game, a.package, a.mesh)
            result = '\n'.join('%-50s %s' % (o['label'], o['path']) for o in objects) or 'This character has no extra objects.'
        elif a.command == 'extract-pmsk':
            result = core.job_extract_pmsk(a.workspace, a.game, a.package, a.mesh)
        print(result or 'Done')
        return 0
    except Exception as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
