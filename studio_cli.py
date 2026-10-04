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
        else:
            s.add_argument('--no-textures', action='store_true')
    a = p.parse_args()
    try:
        core.setup_workspace(a.workspace, a.game)
        if a.command == 'init': result = a.workspace
        elif a.command == 'add-original': result = core.add_original_package(a.workspace, a.package, game_dir=a.game)
        elif a.command == 'import-glb': result = core.import_model(a.workspace, a.model, a.game)
        elif a.command == 'import-textures': result = core.import_images(a.workspace, a.images, a.model, a.game)
        elif a.command == 'meshes': result = core.job_load_package(a.workspace, a.game, a.package)
        elif a.command == 'export': result = core.job_export_vanilla(a.workspace, a.game, a.package, a.mesh, textures=not a.no_textures)
        elif a.command == 'convert':
            mapping = json.loads(core.Path(a.texture_map).read_text(encoding='utf-8')) if a.texture_map else {}
            result = core.job_convert(a.workspace, a.game, a.package, a.mesh, a.model, mapping,
                                      out_name=a.name, preview=not a.no_preview)
        print(result or 'Done')
        return 0
    except Exception as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
