# MKX Character Studio

An unofficial community tool for Mortal Kombat X (PC). It is not affiliated with or endorsed by Warner Bros. Games or NetherRealm Studios.

- **Export** a character from your own game installation to `.glb` (skeleton and textured mesh), plus its textures, to use as a Blender reference. If Blender is installed, a ready-to-open `.blend` is made too.
- **Convert** your own character `.glb`, rigged to that skeleton, and its textures into the game's character package format.
- **Make Pmsk textures** from grayscale body editing masks, or pack four raw channels without changing their values.

No game files are included; you need your own copy of the game. The tool only reads the game folder and writes to a separate mod folder.

## Setup (Windows)

1. Install Python 3.10 or newer (the python.org installer includes Tcl/Tk).
2. Run **Setup Dependencies.cmd** to install NumPy and Pillow into a local `.venv`.
3. Run **Launch Studio.cmd**.

## Use

1. Choose a mod folder outside the game folder. The app creates `character`, `textures`, `converted`, `vanilla_exports` and `vanilla_cache` inside it.
2. **Export tab:** choose a character package and its main mesh, then export. The export folder includes `slots_and_textures.txt`, and a `.blend` when Blender is found (Steam, the standard install folder or PATH; use **Blender...** to choose `blender.exe` yourself). Only the character is exported unless you tick some of its **extra objects** (hats, weapons...); each ticked object is exported the same way into `objects\<object>\`.
3. **Blender:** open the exported `.blend` (or import the `.glb`) and build your character over it in the same pose and scale. Rig it to that armature without changing the bones, with at most 4 weights per vertex. Name your materials `slot00_` to `slot11_` to match the slot list. Export a `.glb` with skinning, normals, tangents and UVs.
4. **Convert tab:** choose the same base character, then **Import GLB...** and, optionally, **Import textures...**. Check the texture choices and press **Convert**. Results are saved in `converted\<name>\`.
   - **One .glb per mesh:** **Mesh to replace** lists the character and its extra objects (for example Johnny Cage's glasses, rings and phone). Pick a mesh, then the `.glb` and textures that replace it; every mesh keeps its own choices, so switching to the glasses shows the glasses' `.glb` (none until you pick one), never the character's. The line under it lists everything that will be converted, and one **Convert** builds all of them. Choices are remembered per mod folder and character, also after closing the app. Rig each object to its own exported skeleton (export it from the Export tab).
   - **Wrong vertex normals are fixed automatically.** MKX lights, shades and reflects characters with their vertex normals, which on shipped characters follow the surface closely. Some exported models carry normals that point elsewhere (for example written in a different axis frame than the vertex positions), which shows up in the game as dark smudges and patchy shine. The converter checks this: normals that a single axis turn puts back on the surface are turned back (hard edges are kept), anything else is rebuilt smooth from the surface, and the tangents are then rebuilt from the UV map. The log says when this happens. Triangles are never turned around: the game hides the back of every triangle, so that would punch holes in the model.
   - **Blood and damage (2nd UV map):** the game paints blood and damage through a model's second UV map. By default the converter uses yours, or makes one if your model has none: every UV island gets its own spot, sized by its real surface area, with no overlaps. Copying from the original only suits edits of the original mesh.
   - **Diffuse alpha is shading** (ambient occlusion: it darkens creases and blocks reflections), not transparency. A diffuse image without its own alpha gets 210, the middle of MKX's own shading maps, instead of fully lit 255.
   - **Experimental: keep my images' own size** (off by default). Normally every image is resized to the original texture's size. With this on, each texture is stored at your image's size instead (rounded to power-of-two sides), which avoids pointless upscaling and makes smaller packages. The files verify, but this has not been tested in the game yet.
   - **Turn off the original face wrinkles** (on by default): intros and close-ups blend in face wrinkles painted for the original character's face layout. On a new model they appear as smudges, so their masks are blanked.
   - **Darken bright diffuse textures:** MKX diffuse textures average 58-72 brightness out of 255. Much brighter art (above 120) is darkened to 110 so it doesn't glow under the game's lighting.
   - **Extra objects (hats, weapons...):** these are separate meshes that the game attaches to the character, so replacing the main mesh does not replace them (for example Erron Black's Gunslinger hat ends up inside a taller model's neck). They are listed after you pick a base character, all ticked (shown). Untick one to hide it (an object you replace with your own `.glb` stays ticked): its triangles are blanked, so it draws nothing and casts no shadow; nothing else changes. The objects live in other packages, `TRAIT_<name><n>_ScriptAssets.xxx` (variation *n*), `Char_<name>_<costume>_ScriptAssets.xxx` (every variation) and `UI_PS_<NAME>_SCRIPTASSETS.xxx` (select screen), so hiding or replacing them puts those packages in the result folder too: copy every `.xxx` in it into the game's `Asset` folder, after backing up the originals. `TRAIT_` and `UI_PS_` packages are shared by all of the character's costumes, so a hidden object is hidden on every costume. Command line: `objects PACKAGE MESH` lists them, `convert ... --hide OBJECT` hides one, `convert ... --extra OBJECT=GLB` replaces one too, and `export ... --object OBJECT` exports one with the character.
5. **Make Pmsk mask tab (optional):** choose **Body editing masks** for up to four grayscale masks painted on your model's UV layout, or **Raw channels** to pack four equally sized grayscale channel images unchanged. Enter your `.glb`'s name and press **Make Pmsk**. The result is saved as `textures\<name>\<name>_Pmsk.png` and suggested in the Convert tab.
6. **Extract Pmsk tab (optional):** choose a game character and mesh. The export contains the decoded RGBA texture, four raw channel images, normalized editing masks for supported body materials, approximate previews, and `pmsk_report.txt` with material parameters and interpreted channel uses. Results go in `vanilla_exports\<package>_<mesh>_Pmsk\`.

In the `.blend`, each bone points at its child, as a normal Blender armature does. Bones are coloured and sorted into bone collections: regular bones keep the default colour, helper bones (`_helper`, twist `Roll`, corrective `Cheat`, `PIN_`, attachment and `Dummy_` bones) are yellow, facial bones (everything under `Head`, plus `wrinkle_` bones) are blue and cloth bones (`C_` capes, hair, tails) are green. Facial and cloth bones have Deform turned off, so Blender's automatic weights leave them out; their existing weights stay in the mesh and are exported normally. A `.glb` has no bone directions, so importing it shows every MKX bone pointing down. Only the display direction of the bones differs: joint positions and the mesh are identical, and models built on either one convert the same way.

### Pmsk channels

Pmsk packs four grayscale data channels into one RGBA texture. Their meaning depends on the material assigned to the mesh. The texture does not define new materials or supply its own surface settings.

The table below is the tool's **working interpretation** for Costume, Skin and Metal body material families and their supported variants. Material parameter sets and alternate-palette swapping are corroborated by asset/code inspection. The exact channel mappings, thresholds and blending still need traceable shader evidence and in-game validation; they are not a verified universal MKX specification.

This table describes **Body editing masks** inputs. In particular, white in the wound editing mask is encoded as raw red **127**, not 255.

| Channel | Controls | Black | White | Empty row in tab 3 |
|---|---|---|---|---|
| Red | Interpreted wound control (Costume/cloth/leather) | wound control off | wound control enabled | enabled throughout |
| Green | Glow areas (read by glowing materials) | no glow | full glow in the material's glow colour | no glow |
| Blue | Tint influence | primary tint; near middle grey = approximately original colour | alternate tint | near middle grey |
| Alpha | Surface settings blend | primary settings | alternate settings | primary settings |

- **Tint influence:** supported body materials have primary and alternate colour multipliers and desaturation settings. In the preview's interpretation, blue runs from primary tint at black, through approximately unchanged diffuse colour near middle grey, to alternate tint at white. Intermediate values blend tint influence. These are not just two on/off groups. Eight-bit images cannot encode exactly 0.5; both 127 and 128 are close approximations.
- **Palette and variation:** colour values come from the selected material, including its inherited settings. Alternate-palette selection uses an `_AP` material when it exists and otherwise keeps the original. Variation material sets can also change these values. The two tint settings are separate from the choice of normal or alternate palette.
- **Surface settings:** supported body materials provide primary and alternate sets of roughness, specularity, metalness, subsurface and detail settings. Alpha is interpreted as a blend between these sets, with intermediate gray values blending their influence. It is not directly a roughness map or ordinary image transparency.

### Raw extraction and reconstruction

- `*_raw_rgba.png` preserves the decoded RGBA pixels of the largest mip stored in the package, including RGB values under zero alpha.
- `*_raw_red.png`, `*_raw_green.png`, `*_raw_blue.png` and `*_raw_alpha.png` are untouched grayscale channels. Load all four into **Raw channels** mode to reconstruct those decoded pixels exactly. This works for body, hair, eye and unknown layouts without assigning meanings to the channels.
- Raw mode requires all four images at identical dimensions, accepts grayscale images, and never resizes or rescales them. Image transparency is ignored; grayscale values are packed directly. Higher-bit-depth inputs are rejected rather than silently reduced.
- `*_edit_*` images are convenience masks for **Body editing masks** mode. Red is stretched from 0–127 to 0–255 and clipped. Repacking these masks changes original red values above 127 to 127; this is **not lossless reconstruction**. Empty editing rows use `(127, 0, 127, 0)` defaults. Editing images are resized to the first supplied image, and transparent areas count as black.
- Raw PNG preservation applies to decoded pixels. It does not preserve original BC7 compressed bytes, other mip levels or streamed higher-resolution data. Converting back into the game format can introduce compression changes.

Channel maps use display thresholds stated on each panel; those thresholds do not establish shader cutoffs. Recolour previews approximate the interpreted tint step and omit game lighting and other shader effects. Material-family channel labels are interpretations, while the reported parameter values are read from the assets.

Other material types have different working interpretations. Use **Raw channels** mode for these layouts; the body editing controls do not describe them:
- **Eyes/mouth:** red = iris detail colour, green = glow, alpha = eye or mouth settings.
- **Eye film:** red = iris detail colour, green = glow.
- **Opaque hair:** red = highlight strength, green = shadowing between strands.
- **See-through hair:** like opaque hair, plus blue × alpha is a cut-out mask.

## Limits

- Replaces the main mesh (LOD0) of an existing character and keeps its original skeleton. It can't add characters, materials or shaders.
- Up to 65,535 vertices. Only textures stored inside the package are supported; streamed `.tfc` textures are not.
- All 232 character packages in the game pass the tool's verification. Conversion was tested with Jason, Johnny Cage and Tanya.
- The tool checks its output by reading it back. The game itself verifies its files and does not load modified packages.

## Command line and tests

- `python studio_cli.py --help` (commands: `init`, `add-original`, `meshes`, `export`, `import-glb`, `import-textures`, `convert`, `pmsk`, `extract-pmsk`)
- `pmsk NAME --raw --red RED.png --green GREEN.png --blue BLUE.png --alpha ALPHA.png` packs raw channels (add the usual `--workspace` and `--game` options before `pmsk`). The existing `--wounds`, `--glow`, `--tint`, `--surface` option names remain aliases for R/G/B/A respectively; raw mode gives them no material meaning.
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
