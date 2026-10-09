"""Build a .blend from an exported MKX .glb, with each bone pointing at its child.

glTF stores joints without a bone direction, so Blender's importer aims every bone along the joint's local axis; for
MKX skeletons that makes them all point down. This script re-aims the bones only: joint positions stay where they are,
each bone keeps the twist of its original frame as closely as possible, and the mesh does not move. Helper, facial and
cloth bones get their own colours and bone collections; facial and cloth bones have Deform turned off.

Run by MKX Character Studio:  blender -b --factory-startup --python blender_rig.py -- in.glb out.blend
The bone-direction rules (bone_tails) are plain Python so they can be tested without Blender.
"""
import math
import sys

HUB_CHILDREN = 6      # a bone with this many spread-out children (a head with face joints) continues its parent's line
# Bone colours (Blender's built-in bone colour themes); regular body bones keep the default colour.
CATEGORY_COLOURS = {'helper': 'THEME09', 'facial': 'THEME04', 'cloth': 'THEME03'}    # yellow, blue, green
CATEGORY_NAMES = {'body': 'Body', 'helper': 'Helper', 'facial': 'Facial', 'cloth': 'Cloth'}
NO_DEFORM = {'facial', 'cloth'}


def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _mul(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _len(a): return math.sqrt(_dot(a, a))
def _unit(a): return _mul(a, 1.0 / _len(a))


def bone_tails(heads, parents, fallback=(0.0, 0.0, 1.0)):
    """Tail position for every bone, given head positions and parent indices (-1 for roots; parents come first).

    * one child, or one child whose branch holds most of the bones below (hips -> spine, upper arm -> forearm):
      the tail sits on that child's head
    * several comparable children (hand -> fingers): the tail sits on their average
    * many spread-out children (head -> face joints): the bone continues its parent's line, as long as its children reach
    * no children: the bone continues the line from its parent's head (fingertips, toes, face joints)
    * a root bone points at its child but stays short, so it does not cover the hips and legs
    Children sitting on the bone's own head (twist and helper joints) are ignored.
    """
    n = len(heads)
    kids = [[] for _ in range(n)]
    for i, p in enumerate(parents):
        if p >= 0: kids[p].append(i)
    size = [1] * n
    for i in reversed(range(n)):
        if parents[i] >= 0: size[parents[i]] += size[i]
    lo = [heads[0][k] for k in range(3)]; hi = list(lo)
    for h in heads:
        for k in range(3): lo[k] = min(lo[k], h[k]); hi[k] = max(hi[k], h[k])
    extent = max(hi[k] - lo[k] for k in range(3)) or 1.0
    eps, min_len, max_leaf, max_root = extent * 1e-4, extent * 0.006, extent * 0.015, extent * 0.1

    tails, dirs = [None] * n, [None] * n
    for i in range(n):
        h, p = heads[i], parents[i]
        pdir = dirs[p] if p >= 0 else fallback
        near = [c for c in kids[i] if _len(_sub(heads[c], h)) > eps]
        target = None
        if near:
            big = max(near, key=lambda c: size[c])
            if len(near) == 1 or 2 * size[big] > sum(size[c] for c in near):
                target = heads[big]
            elif len(near) >= HUB_CHILDREN and p >= 0:
                reach = max(_dot(_sub(heads[c], h), pdir) for c in near)
                target = _add(h, _mul(pdir, max(reach, min_len)))
            else:
                target = tuple(sum(heads[c][k] for c in near) / len(near) for k in range(3))
            if _len(_sub(target, h)) <= eps:
                target = None
        if target is None:                               # leaf, or children that cancel out
            off = _sub(h, heads[p]) if p >= 0 else (0.0, 0.0, 0.0)
            d = _unit(off) if _len(off) > eps else pdir
            target = _add(h, _mul(d, min(max(0.2 * _len(off), min_len), max_leaf)))
        else:
            d = _sub(target, h)
            if _len(d) < min_len:                        # very short bones stay visible
                target = _add(h, _mul(_unit(d), min_len))
            elif p < 0 and _len(d) > max_root:           # a root on the floor stays short instead of spanning the legs
                target = _add(h, _mul(_unit(d), max_root))
        dirs[i] = _unit(_sub(target, h))
        tails[i] = target
    return tails


def bone_categories(names, parents):
    """'body', 'helper', 'facial' or 'cloth' for each bone, from MKX's bone naming (parents come first):
    cloth = C_ bones (capes, hair, tails, jiggle); facial = everything under Head and the wrinkle_ bones;
    helper = _helper, Roll (twist), Cheat (corrective), Dummy_, PIN_, Attach, CenterOfMass and position_locator
    bones; body = the rest."""
    head = names.index('Head') if 'Head' in names else -1
    out = []
    for i, name in enumerate(names):
        low, p = name.lower(), parents[i]
        while p >= 0 and p != head:
            p = parents[p]
        if name.startswith('C_'):
            out.append('cloth')
        elif low.startswith('wrinkle_') or (head >= 0 and i != head and p == head):
            out.append('facial')
        elif (any(k in low for k in ('_helper', 'roll', 'cheat', 'attach')) or low.startswith(('dummy_', 'pin_'))
              or low in ('centerofmass', 'position_locator')):
            out.append('helper')
        else:
            out.append('body')
    return out


# ----------------------------------------------------------------------------------------------- Blender side
def build_blend(glb, out):
    import bpy
    from mathutils import Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb, bone_heuristic='BLENDER', disable_bone_shape=True)
    arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
    meshes = [o for o in bpy.data.objects if o.type == 'MESH' and o.find_armature() == arm]

    def evaluated(o):
        dg = bpy.context.evaluated_depsgraph_get()
        e = o.evaluated_get(dg); m = e.to_mesh()
        co = [e.matrix_world @ v.co for v in m.vertices]; e.to_mesh_clear(); return co
    before = {o.name: evaluated(o) for o in meshes}

    bpy.context.view_layer.objects.active = arm
    for o in bpy.context.view_layer.objects: o.select_set(o == arm)
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = list(arm.data.edit_bones)
    order, index = [], {}
    def visit(b):
        index[b.name] = len(order); order.append(b)
        for c in b.children: visit(c)
    for b in ebs:
        if b.parent is None: visit(b)
    names, heads = [b.name for b in order], [tuple(b.head) for b in order]
    frames = [b.matrix.to_3x3() for b in order]
    tails = bone_tails(heads, [index[b.parent.name] if b.parent else -1 for b in order])
    for b, frame, tail in zip(order, frames, tails):
        new_y = (Vector(tail) - b.head).normalized()
        # rotate the original frame by the smallest turn that brings its nearest axis onto the new bone direction,
        # then roll the bone so it matches that frame: local axes stay close to the game's joint axes
        axes = [frame.col[k] * s for k in range(3) for s in (1, -1)]
        k = max(range(6), key=lambda j: axes[j].dot(new_y))
        q = axes[k].rotation_difference(new_y)
        b.tail = Vector(tail)
        b.align_roll(q @ frame.col[(k // 2 + 1) % 3])
        b.use_connect = False
    bpy.ops.object.mode_set(mode='OBJECT')

    # the mesh must not have moved, and the joint positions must be unchanged
    worst = 0.0
    for o in meshes:
        worst = max([worst] + [(a - b).length for a, b in zip(before[o.name], evaluated(o))])
    joint = max((Vector(h) - arm.data.bones[nm].head_local).length for nm, h in zip(names, heads))
    if worst > 1e-3 or joint > 1e-4:
        raise RuntimeError('bone fix moved the mesh by %.4f / joints by %.4f' % (worst, joint))

    # facial and cloth bones do not deform (so automatic weights skip them); each kind gets its own colour and
    # bone collection. Turning deform off keeps the vertex groups, so exported .glb files keep those weights.
    categories = bone_categories(names, [names.index(arm.data.bones[nm].parent.name) if arm.data.bones[nm].parent else -1
                                         for nm in names])
    groups = {}
    for nm, category in zip(names, categories):
        bone = arm.data.bones[nm]
        bone.use_deform = category not in NO_DEFORM
        if category in CATEGORY_COLOURS:
            bone.color.palette = CATEGORY_COLOURS[category]
        if category not in groups:
            groups[category] = arm.data.collections.new(CATEGORY_NAMES[category])
        groups[category].assign(bone)
    arm.data.show_bone_colors = True
    for pb in arm.pose.bones:
        pb.custom_shape = None
    arm.data.display_type = 'OCTAHEDRAL'; arm.show_in_front = True
    for c in [c for c in bpy.data.collections if c.name.startswith('glTF_not_exported')]:
        for o in list(c.objects): bpy.data.objects.remove(o)
        bpy.data.collections.remove(c)
    for im in bpy.data.images:
        if not im.packed_file and im.source == 'FILE': im.pack()
    bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
    count = {c: categories.count(c) for c in CATEGORY_NAMES}
    print('MKX_BLEND_OK %d bones (%s), mesh moved %.6f' % (
        len(names), ', '.join('%d %s' % (count[c], CATEGORY_NAMES[c].lower()) for c in CATEGORY_NAMES if count[c]), worst))


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    if len(args) != 2:
        sys.exit('usage: blender -b --factory-startup --python blender_rig.py -- in.glb out.blend')
    build_blend(*args)
