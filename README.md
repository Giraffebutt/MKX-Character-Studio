# MKX Character Studio

An unofficial community tool for Mortal Kombat X (PC). It is not affiliated with or endorsed by Warner Bros. Games or NetherRealm Studios.

- **Export** a character from your own game installation to `.glb` (skeleton and textured mesh), plus its textures, to use as a Blender reference.
- **Convert** your own character `.glb`, rigged to that skeleton, and its textures into the game's character package format.

No game files are included; you need your own copy of the game. The tool only reads the game folder and writes to a separate mod folder.

## Setup (Windows)

1. Install Python 3.10 or newer (the python.org installer includes Tcl/Tk).
2. Run **Setup Dependencies.cmd** to install NumPy and Pillow into a local `.venv`.
3. Run **Launch Studio.cmd**.

## Use

1. Choose a mod folder outside the game folder. The app creates `character`, `textures`, `converted`, `vanilla_exports` and `vanilla_cache` inside it.
2. **Export tab:** choose a character package and its main mesh, then export. The export folder includes `slots_and_textures.txt`.
3. **Blender:** import the exported `.glb` and build your character over it in the same pose and scale. Rig it to that armature without changing the bones, with at most 4 weights per vertex. Name your materials `slot00_` to `slot11_` to match the slot list. Export a `.glb` with skinning, normals, tangents and UVs.
4. **Convert tab:** choose the same base character, then **Import GLB...** and, optionally, **Import textures...**. Check the texture choices and press **Convert**. Results are saved in `converted\<name>\`.

## Limits

- Replaces the main mesh (LOD0) of an existing character and keeps its original skeleton. It can't add characters, materials or shaders.
- Up to 65,535 vertices. Only textures stored inside the package are supported; streamed `.tfc` textures are not.
- All 232 character packages in the game pass the tool's verification. Conversion was tested with Jason, Johnny Cage and Tanya.
- The tool checks its output by reading it back. The game itself verifies its files and does not load modified packages.

## Command line and tests

- `python studio_cli.py --help` (commands: `init`, `add-original`, `meshes`, `export`, `import-glb`, `import-textures`, `convert`)
- `python -m unittest -v test_studio` (uses synthetic data; no game files needed)

## Legal disclaimer

MKX Character Studio is an unofficial, AI-assisted modding tool and is not affiliated with, endorsed by, or sponsored by Warner Bros. Games or NetherRealm Studios.

Mortal Kombat, Mortal Kombat X, and all related trademarks, characters, assets, and intellectual property are the property of their respective owners.

This software does not include or distribute Mortal Kombat X game assets. Users must provide their own legally obtained copy of the game, and any game data processed by this tool is accessed locally from the user's installation.

This software is intended for personal modding. Users are responsible for ensuring that their use of this software complies with applicable laws, licenses, and terms of service.

The developers of MKX Character Studio are not responsible for game corruption, data loss, account issues, or other consequences resulting from use of this software.

## Author's note

I am not a programmer and I will not claim to be one throughout any of these kinds of projects that I may or may not continue making. This custom project in particular has been vibecoded from start to finish using AI. The project's code, implementation, and development process was produced with the assistance of AI rather than all completely written by me. I do not claim to have personally written the code or present myself as the programmer. This project was created with AI assistance under my direction, testing, and feature requirements. If this project ever does get released publicly, no personal credit is required for using, modifying or redistributing the project, subject to the license terms below. I wish to not be credited as the programmer, developer or author of this project or code.

Anyone is able to use this project without crediting me.

## License

MKX Character Studio is licensed under the **PolyForm Noncommercial License 1.0.0**.

You may use, modify, and redistribute this software for noncommercial purposes, subject to the terms of the license.

Commercial use of this software is not permitted under this license.

Full license terms: [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). A complete copy is included in [LICENSE](LICENSE).

No personal credit is required. Redistribution must include the license terms or their URL and any required notices specified by the license. See also [THIRD_PARTY.md](THIRD_PARTY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
