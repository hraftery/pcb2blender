import bpy, bmesh, addon_utils
from bpy_extras.io_utils import ImportHelper, orientation_helper, axis_conversion
from bpy.props import *
from mathutils import Vector, Matrix

import tempfile, random, shutil, re, struct, math, io
from pathlib import Path
from zipfile import ZipFile, BadZipFile, Path as ZipPath
from dataclasses import dataclass, field
from enum import Enum
import numpy as np

from skia import SVGDOM, Stream, Surface, Color4f
SKIA_MAGIC = 0.282222222
from PIL import Image, ImageOps

from .materials import setup_pcb_material, merge_materials, enhance_materials

from io_scene_x3d import ImportX3D, X3D_PT_import_transform, import_x3d
from io_scene_x3d import menu_func_import as menu_func_import_x3d_original

PCB = "pcb.wrl"
COMPONENTS = "components"
LAYERS = "layers"
LAYERS_BOUNDS = "bounds"
BOARDS = "boards"
BOUNDS = "bounds"
STACKED = "stacked_"
PADS = "pads"

INCLUDED_LAYERS = (
    "F_Cu", "B_Cu", "F_Paste", "B_Paste", "F_SilkS", "B_SilkS", "F_Mask", "B_Mask"
)

REQUIRED_MEMBERS = {PCB, LAYERS}

PCB_THICKNESS = 1.6 # mm

@dataclass
class Board:
    bounds: tuple[Vector, Vector]
    stacked_boards: list[tuple[str, Vector]]
    obj: bpy.types.Object = None

class PadType(Enum):
    UNKNOWN = -1
    THT = 0
    SMD = 1
    CONN = 2
    NPTH = 3

    @classmethod
    def _missing_(cls, value):
        print(f"warning: unknown pad type '{value}'")
        return cls.UNKNOWN

class PadShape(Enum):
    UNKNOWN = -1
    CIRCLE = 0
    RECT = 1
    OVAL = 2
    TRAPEZOID = 3
    ROUNDRECT = 4
    CHAMFERED_RECT = 5
    CUSTOM = 6

    @classmethod
    def _missing_(cls, value):
        print(f"warning: unknown pad shape '{value}'")
        return cls.UNKNOWN

class DrillShape(Enum):
    UNKNOWN = -1
    CIRCULAR = 0
    OVAL = 1

    @classmethod
    def _missing_(cls, value):
        print(f"warning: unknown drill shape '{value}'")
        return cls.UNKNOWN

@dataclass
class Pad:
    position: Vector
    is_flipped: bool
    has_model: bool
    is_tht_or_smd: bool
    pad_type: PadType
    shape: PadShape
    size: Vector
    rotation: float
    roundness: float
    drill_shape: DrillShape
    drill_size: Vector

@dataclass
class PCB3D:
    content: str
    components: list[str]
    layers_bounds: tuple[float, float, float, float]
    boards: dict[str, Board]
    pads: dict[str, Pad]

class PCB2BLENDER_OT_import_pcb3d(bpy.types.Operator, ImportHelper):
    """Import a PCB3D file"""
    bl_idname = "pcb2blender.import_pcb3d"
    bl_label = "Import .pcb3d"
    bl_options = {"PRESET", "UNDO"}

    import_components: BoolProperty(name="Import Components", default=True)
    add_solder_joints: EnumProperty(name="Add Solder Joints", default="SMART",
        items=(
            ("NONE", "None", "Do not add any solder joints"),
            ("SMART", "Smart", "Only add solder joints to footprints that have THT/SMD "\
                "attributes set and have 3D models"),
            ("ALL", "All", "Add solder joints to all pads")))
    center_pcb:        BoolProperty(name="Center PCB", default=True)

    merge_materials:   BoolProperty(name="Merge Materials", default=True)
    enhance_materials: BoolProperty(name="Enhance Materials", default=True)

    cut_boards:        BoolProperty(name="Cut PCBs", default=True)
    stack_boards:      BoolProperty(name="Stack PCBs", default=True)

    pcb_material:      EnumProperty(name="PCB Material", default="RASTERIZED",
        items=(("RASTERIZED", "Rasterized", ""), ("3D", "3D", "")))
    texture_dpi:       FloatProperty(name="Texture DPI",
        default=1016.0, soft_min=508.0, soft_max=2032.0)

    import_fpnl:       BoolProperty(name="Import Frontpanel (.fpnl)", default=True,
        description="Import the specified .fpnl file and align it (if its stacked to a pcb).")
    fpnl_path:         StringProperty(name="", subtype="FILE_PATH",
        description="")

    fpnl_thickness:    FloatProperty(name="Panel Thickness (mm)",
        default=2.0, soft_min=0.0, soft_max=5.0)
    fpnl_bevel_depth:  FloatProperty(name="Bevel Depth (mm)",
        default=0.05, soft_min=0.0, soft_max=0.25)
    fpnl_setup_camera: BoolProperty(name="Setup Orthographic Camera", default=True)

    filter_glob:       StringProperty(default="*.pcb3d", options={"HIDDEN"})

    def __init__(self):
        self.last_fpnl_path = ""
        self.component_cache = {}
        self.new_materials = set()
        super().__init__()

    def execute(self, context):
        filepath = Path(self.filepath)

        # import boards

        if (pcb := self.import_pcb3d(context, filepath)) == {"CANCELLED"}:
            return {"CANCELLED"}

        # import front panel

        if has_svg2blender() and self.import_fpnl and self.fpnl_path != "":
            if Path(self.fpnl_path).is_file():
                bpy.ops.svg2blender.import_fpnl(
                    filepath=self.fpnl_path,
                    thickness=self.fpnl_thickness,
                    bevel_depth=self.fpnl_bevel_depth,
                    setup_camera=self.fpnl_setup_camera
                )
                pcb.boards["FPNL"] = Board((Vector(), Vector()), [], context.object)
            else:
                self.warning(f"frontpanel file \"{filepath}\" does not exist")

        # stack boards

        if self.stack_boards:
            for board in pcb.boards.values():
                for (name, offset) in board.stacked_boards:
                    if not name in pcb.boards:
                        self.warning(f"ignoring stacked board \"{name}\" (unknown board)")
                        continue

                    if not pcb.boards[name].obj:
                        self.warning(
                            f"ignoring stacked board \"{name}\" (cut_boards is set to False)")
                        continue

                    stacked_obj = pcb.boards[name].obj
                    stacked_obj.parent = board.obj

                    pcb_offset = Vector((0, 0, np.sign(offset.z) * PCB_THICKNESS))
                    if name == "FPNL":
                        pcb_offset.z += (self.fpnl_thickness - PCB_THICKNESS) * 0.5
                    stacked_obj.location = (offset + pcb_offset) * MM_TO_M

        # select pcb objects and make one active

        bpy.ops.object.select_all(action="DESELECT")
        top_level_boards = [board for board in pcb.boards.values() if not board.obj.parent]
        context.view_layer.objects.active = top_level_boards[0].obj
        for board in top_level_boards:
            board.obj.select_set(True)

        # center pcbs

        if self.center_pcb:
            center = Vector((0, 0))
            for board in top_level_boards:
                center += (board.bounds[0] + board.bounds[1]) * 0.5
            center /= len(top_level_boards)

            for board in top_level_boards:
                board.obj.location.xy = (board.bounds[0] - center) * MM_TO_M

        # materials

        if self.merge_materials:
            merge_materials(self.component_cache.values())

        for material in self.new_materials.copy():
            if not material.users:
                self.new_materials.remove(material)
                bpy.data.materials.remove(material)

        if self.enhance_materials:
            enhance_materials(self.new_materials)

        return {"FINISHED"}

    def import_pcb3d(self, context, filepath):
        if not filepath.is_file():
            return self.error(f"file \"{filepath}\" does not exist")

        dirname = filepath.name.replace(".", "_") + f"_{random.getrandbits(64)}"
        tempdir = Path(tempfile.gettempdir()) / "pcb2blender_tmp" / dirname
        tempdir.mkdir(parents=True, exist_ok=True)

        try:
            with ZipFile(filepath) as file:
                MEMBERS = {path.name for path in ZipPath(file).iterdir()}
                if missing := REQUIRED_MEMBERS.difference(MEMBERS):
                    return self.error(f"not a valid .pcb3d file: missing {str(missing)[1:-1]}")
                pcb = self.parse_pcb3d(file, tempdir)
        except BadZipFile:
            return self.error("not a valid .pcb3d file: not a zip file")
        except (KeyError, struct.error) as e:
            return self.error(f"pcb3d file is corrupted: {e}")

        # import objects

        materials_before = set(bpy.data.materials)

        objects_before = set(bpy.data.objects)
        bpy.ops.pcb2blender.import_x3d(
            filepath=str(tempdir / PCB), scale=1.0, join=False, enhance_materials=False)
        pcb_objects = set(bpy.data.objects).difference(objects_before)
        pcb_objects = sorted(pcb_objects, key=lambda obj: obj.name)

        for obj in pcb_objects:
            obj.data.transform(Matrix.Diagonal((*obj.scale, 1)))
            obj.scale = (1, 1, 1)
            self.setup_uvs(obj, pcb.layers_bounds)

        # rasterize/import layer svgs

        if self.pcb_material == "RASTERIZED" and self.enhance_materials:
            layers_path = tempdir / LAYERS
            dpi = self.texture_dpi
            images = {}
            for f_layer, b_layer in zip(INCLUDED_LAYERS[0::2], INCLUDED_LAYERS[1::2]):
                front = self.svg2img(layers_path / f"{f_layer}.svg", dpi).getchannel(0)
                back  = self.svg2img(layers_path / f"{b_layer}.svg", dpi).getchannel(0)

                if (layer := f_layer[2:]) != "Mask":
                    front = ImageOps.invert(front)
                    back  = ImageOps.invert(back)
                    empty = Image.new("L", front.size)

                png_path = layers_path / f"{layer}.png"
                merged = Image.merge("RGB", (front, back, empty))
                merged.save(png_path)

                image = bpy.data.images.load(str(png_path))
                image.colorspace_settings.name = "Non-Color"
                image.pack()
                image.filepath = ""

                images[layer] = image

        # import components

        if self.import_components:
            for component in pcb.components:
                bpy.ops.pcb2blender.import_x3d(
                    filepath=str(tempdir / component), enhance_materials=False)
                obj = context.object
                obj.data.name = component.rsplit("/", 1)[1].rsplit(".", 1)[0]
                self.component_cache[component] = obj.data
                bpy.data.objects.remove(obj)

        self.new_materials |= set(bpy.data.materials) - materials_before

        shutil.rmtree(tempdir)

        # enhance pcb

        can_enhance = len(pcb_objects) == len(PCB2_LAYER_NAMES)
        if can_enhance:
            layers = dict(zip(PCB2_LAYER_NAMES, pcb_objects))
            for name, obj in layers.items():
                obj.data.materials[0].name = name

            board = layers["Board"]
            self.improve_board_mesh(board.data)
        else:
            self.warning(f"cannot enhance pcb"\
                f"(imported {len(pcb_objects)} layers, expected {len(PCB2_LAYER_NAMES)})")

        pcb_meshes = {obj.data for obj in pcb_objects if obj.type == "MESH"}

        if self.pcb_material == "RASTERIZED" and self.enhance_materials:
            for obj in pcb_objects[1:]:
                bpy.data.objects.remove(obj)
            pcb_object = pcb_objects[0]

            pcb_object.data.transform(Matrix.Diagonal((1, 1, 1.015, 1)))

            board_material = pcb_object.data.materials[0]
            setup_pcb_material(board_material.node_tree, images)
        else:
            if can_enhance:
                self.enhance_pcb_layers(context, layers)
                pcb_objects = list(layers.values())

            pcb_object = pcb_objects[0]
            bpy.ops.object.select_all(action="DESELECT")
            for obj in pcb_objects:
                obj.select_set(True)
            context.view_layer.objects.active = pcb_object
            bpy.ops.object.join()
            bpy.ops.object.transform_apply()

        for mesh in pcb_meshes:
            if not mesh.users:
                bpy.data.meshes.remove(mesh)

        # cut boards

        if not (has_multiple_boards := bool(pcb.boards and self.cut_boards)):
            name = f"PCB_{filepath.stem}"
            pcb_object.name = pcb_object.data.name = name
            bounds = (
                Vector(pcb_object.bound_box[3]).xy * M_TO_MM,
                Vector(pcb_object.bound_box[5]).xy * M_TO_MM,
            )
            matrix = Matrix.Translation(bounds[0].to_3d() * MM_TO_M)
            pcb_object.data.transform(matrix.inverted())
            pcb_object.matrix_world = matrix @ pcb_object.matrix_world

            pcb_board = Board(bounds, [], pcb_object)
            pcb.boards[name] = pcb_board
        else:
            pcb_mesh = pcb_object.data
            bpy.data.objects.remove(pcb_object)
            for name, board in pcb.boards.items():
                board_obj = bpy.data.objects.new(f"PCB_{name}", pcb_mesh.copy())
                context.collection.objects.link(board_obj)
                boundingbox = self.get_boundingbox(context, board.bounds)

                self.cut_object(context, board_obj, boundingbox, "INTERSECT")

                # get rid of the bounding box if it got merged into the board for some reason
                # also reapply board edge vcs on the newly cut edge faces
                bm = bmesh.new()
                bm.from_mesh(board_obj.data)

                for bb_vert in boundingbox.data.vertices:
                    for vert in reversed(bm.verts[:]):
                        if (bb_vert.co - vert.co).length_squared < 1e-8:
                            bm.verts.remove(vert)
                            break

                board_edge = bm.loops.layers.color[0]
                for bb_face in boundingbox.data.polygons:
                    point = boundingbox.data.vertices[bb_face.vertices[0]].co
                    board_faces = (face for face in bm.faces if face.material_index == 0)
                    faces = self.get_overlapping_faces(board_faces, point, bb_face.normal)
                    for face in faces:
                        for loop in face.loops:
                            loop[board_edge] = (1, 1, 1, 1)

                bm.to_mesh(board_obj.data)
                bm.free()

                bpy.data.objects.remove(boundingbox)

                offset = board.bounds[0].to_3d() * MM_TO_M
                board_obj.data.transform(Matrix.Translation(-offset))
                board_obj.location = offset

                board.obj = board_obj

        related_objects = []

        # populate components

        if self.import_components and self.component_cache:
            match = regex_filter_components.search(pcb.content)
            matrix_all = match2matrix(match)
            
            for match_instance in regex_component.finditer(match.group("instances")):
                matrix_instance = match2matrix(match_instance)
                url = match_instance.group("url")

                component = self.component_cache[url]
                instance = bpy.data.objects.new(component.name, component)
                instance.matrix_world = matrix_all @ matrix_instance @ MATRIX_FIX_SCALE_INV
                context.collection.objects.link(instance)
                related_objects.append(instance)

        # add solder joints

        solder_joint_cache = {}
        if self.add_solder_joints != "NONE" and pcb.pads:
            for pad_name, pad in pcb.pads.items():
                if self.add_solder_joints == "SMART":
                    if not pad.has_model or not pad.is_tht_or_smd:
                        continue

                if not pad.pad_type in {PadType.THT, PadType.SMD}:
                    continue
                if pad.shape == PadShape.UNKNOWN or pad.drill_shape == DrillShape.UNKNOWN:
                    continue

                pad_type = pad.pad_type.name
                pad_size = pad.size
                hole_shape = pad.drill_shape.name
                hole_size = pad.drill_size
                roundness = 0.0
                match pad.shape:
                    case PadShape.CIRCLE:
                        pad_size = (pad.size[0], pad.size[0])
                        roundness = 1.0
                    case PadShape.OVAL:
                        roundness = 1.0
                    case PadShape.ROUNDRECT:
                        roundness = pad.roundness * 2.0
                    case PadShape.TRAPEZOID | PadShape.CHAMFERED_RECT | PadShape.CUSTOM:
                        print(f"skipping solder joint for '{pad_name}', "\
                            f"unsupported shape '{pad.shape.name}'")
                        continue

                cache_id = (pad_type, tuple(pad_size), hole_shape, tuple(hole_size), roundness)
                if not (solder_joint := solder_joint_cache.get(cache_id)):
                    bpy.ops.pcb2blender.solder_joint_add(
                        pad_type=pad_type,
                        pad_shape="RECTANGULAR",
                        pad_size=pad_size,
                        hole_shape=hole_shape,
                        hole_size=hole_size,
                        roundness=roundness,
                        reuse_material=True,
                    )
                    solder_joint = context.object
                    solder_joint_cache[cache_id] = solder_joint

                obj = solder_joint.copy()
                obj.name = f"SOLDER_{pad_name}"
                obj.location.xy = pad.position * MM_TO_M
                obj.rotation_euler.z = pad.rotation
                obj.scale.z *= 1.0 if pad.is_flipped ^ (pad.pad_type == PadType.SMD) else -1.0
                context.collection.objects.link(obj)
                related_objects.append(obj)

        for obj in solder_joint_cache.values():
            bpy.data.objects.remove(obj)

        if not has_multiple_boards:
            for obj in related_objects:
                obj.location.xy -= pcb_board.bounds[0] * MM_TO_M
                obj.parent = pcb_board.obj
        else:
            for obj in related_objects:
                for board in pcb.boards.values():
                    x, y = obj.location.xy * M_TO_MM
                    p_min, p_max = board.bounds
                    if x >= p_min.x and x < p_max.x and y <= p_min.y and y > p_max.y:
                        parent_board = board
                        break
                else:
                    closest = None
                    min_distance = math.inf
                    for name, board in pcb.boards.items():
                        center = (board.bounds[0] + board.bounds[1]) * 0.5
                        distance = (obj.location.xy * M_TO_MM - center).length_squared
                        if distance < min_distance:
                            min_distance = distance
                            closest = (name, board)

                    name, parent_board = closest
                    self.warning(
                        f"assigning \"{obj.name}\" (out of bounds) " \
                        f"to closest board \"{name}\""
                    )

                obj.location.xy -= parent_board.bounds[0] * MM_TO_M
                obj.parent = parent_board.obj

        return pcb

    def parse_pcb3d(self, file, extract_dir) -> PCB3D:
        zip_path = ZipPath(file)

        with file.open(PCB) as pcb_file:
            pcb_file_content = pcb_file.read().decode("UTF-8")
            with open(extract_dir / PCB, "wb") as filtered_file:
                filtered = regex_filter_components.sub("\g<prefix>", pcb_file_content)
                filtered_file.write(filtered.encode("UTF-8"))

        components = list({
            name for name in file.namelist()
            if name.startswith(f"{COMPONENTS}/") and name.endswith(".wrl")
        })
        file.extractall(extract_dir, components)

        layers = (f"{LAYERS}/{layer}.svg" for layer in INCLUDED_LAYERS)
        file.extractall(extract_dir, layers)

        layers_bounds_path = zip_path / LAYERS / LAYERS_BOUNDS
        layers_bounds = struct.unpack("!ffff", layers_bounds_path.read_bytes())

        boards = {}
        for board_dir in (zip_path / BOARDS).iterdir():
            bounds_path = board_dir / BOUNDS
            if not bounds_path.exists():
                continue

            try:
                bounds = struct.unpack("!ffff", bounds_path.read_bytes())
            except struct.error:
                self.warning(f"ignoring board \"{board_dir}\" (corrupted)")
                continue

            bounds = (
                Vector((bounds[0], -bounds[1])),
                Vector((bounds[0] + bounds[2], -(bounds[1] + bounds[3])))
            )

            stacked_boards = []
            for path in board_dir.iterdir():
                if path.name.startswith(STACKED):
                    try:
                        offset = struct.unpack("!fff", path.read_bytes())
                    except struct.error:
                        self.warning("ignoring stacked board (corrupted)")
                        continue

                    stacked_boards.append((
                        path.name.split(STACKED, 1)[-1],
                        Vector((offset[0], -offset[1], offset[2])),
                    ))

            boards[board_dir.name] = Board(bounds, stacked_boards)

        pads = {}
        for path in (zip_path / PADS).iterdir():
            pad_struct = struct.unpack("!ff???BBffffBff", path.read_bytes())
            pads[path.name] = Pad(
                Vector((pad_struct[0], -pad_struct[1])),
                *pad_struct[2:5],
                PadType(pad_struct[5]),
                PadShape(pad_struct[6]),
                Vector(pad_struct[7:9]),
                *pad_struct[9:11],
                DrillShape(pad_struct[11]),
                Vector(pad_struct[12:14]),
            )

        return PCB3D(pcb_file_content, components, layers_bounds, boards, pads)

    @staticmethod
    def get_boundingbox(context, bounds):
        name = "pcb2blender_bounds_tmp"
        mesh = bpy.data.meshes.new(name)
        obj = bpy.data.objects.new(name, mesh)
        context.collection.objects.link(obj)

        margin = 0.01

        size = Vector((1.0, -1.0, 1.0)) * (bounds[1] - bounds[0]).to_3d()
        scale =  (size + 2.0 * Vector((margin, margin, 5.0))) * MM_TO_M
        translation = (bounds[0] - Vector.Fill(2, margin)).to_3d() * MM_TO_M
        matrix_scale = Matrix.Diagonal(scale).to_4x4()
        matrix_offset = Matrix.Translation(translation)
        bounds_matrix = matrix_offset @ matrix_scale @ Matrix.Translation((0.5, -0.5, 0))

        bm = bmesh.new()
        bmesh.ops.create_cube(bm, matrix=bounds_matrix)
        bm.to_mesh(obj.data)
        bm.free()

        return obj

    @staticmethod
    def setup_uvs(obj, layers_bounds):
        mesh = obj.data

        vertices = np.empty(len(mesh.vertices) * 3)
        mesh.vertices.foreach_get("co", vertices)
        vertices = vertices.reshape((len(mesh.vertices), 3))

        indices = np.empty(len(mesh.loops), dtype=int)
        mesh.loops.foreach_get("vertex_index", indices)

        offset = np.array((layers_bounds[0], -layers_bounds[1]))
        size = np.array((layers_bounds[2], layers_bounds[3]))
        uvs = (vertices[:,:2][indices] * M_TO_MM - offset) / size + np.array((0, 1))

        uv_layer = mesh.uv_layers[0]
        uv_layer.data.foreach_set("uv", uvs.flatten())

    @staticmethod
    def improve_board_mesh(mesh):
        # fill holes in board mesh to make subsurface shading work
        # create vertex color layer for board edge and through holes

        bm = bmesh.new()
        bm.from_mesh(mesh)

        board_edge = bm.loops.layers.color.new("Board Edge")

        for face in bm.faces:
            color = ((1, 1, 1, 1) if abs(face.normal.z) < 1e-3 and face.calc_area() > 1e-10
                else (0, 0, 0, 1))
            for loop in face.loops:
                loop[board_edge] = color

        n_upper_verts = len(bm.verts) // 2
        bm.verts.ensure_lookup_table()
        for i, vert in enumerate(bm.verts[:n_upper_verts]):
            other_vert = bm.verts[n_upper_verts + i]
            try:
                bm.edges.new((vert, other_vert))
            except ValueError:
                pass

        filled = bmesh.ops.holes_fill(bm, edges=bm.edges[:])

        through_holes = bm.loops.layers.color.new("Through Holes")
        for face in bm.faces:
            color = (1, 1, 1, 1) if face in filled["faces"] else (0, 0, 0, 1)
            for loop in face.loops:
                loop[through_holes] = color

        bm.to_mesh(mesh)
        bm.free()

    @classmethod
    def enhance_pcb_layers(cls, context, layers):
        for side, direction in reversed(list(zip(("F", "B"), (1, -1)))):
            mask = layers[f"{side}_Mask"]
            copper = layers[f"{side}_Cu"]
            silk = layers[f"{side}_Silk"]

            # split copper layer into tracks and pads

            tracks = copper
            pads = cls.copy_object(copper, context.collection)
            layers[f"{side}_Pads"] = pads

            mask_cutter = cls.copy_object(mask, context.collection)
            cls.extrude_mesh_z(mask_cutter.data, 1e-3, True)
            cls.cut_object(context, tracks, mask_cutter, "INTERSECT")
            cls.cut_object(context, pads, mask_cutter, "DIFFERENCE")
            bpy.data.objects.remove(mask_cutter)

            # remove silkscreen on pads

            pads_cutter = cls.copy_object(pads, context.collection)
            cls.extrude_mesh_z(pads_cutter.data, 1e-3, True)
            cls.cut_object(context, silk, pads_cutter, "DIFFERENCE")
            bpy.data.objects.remove(pads_cutter)

            silk.visible_shadow = False

            # align the layers

            cls.translate_mesh_z(mask.data, -2e-5 * direction)
            cls.translate_mesh_z(silk.data, -7e-5 * direction)
            cls.translate_mesh_z(tracks.data, -1e-5 * direction)
            cls.translate_mesh_z(pads.data, -1e-5 * direction)

        # scale down vias to match the other layers
        vias = layers["Vias"]
        vias.data.transform(Matrix.Diagonal((1, 1, 0.97, 1)))
        vias.data.polygons.foreach_set("use_smooth", [True] * len(vias.data.polygons))

    @staticmethod
    def copy_object(obj, collection):
        new_obj = obj.copy()
        new_obj.data = obj.data.copy()
        collection.objects.link(new_obj)
        return new_obj

    @staticmethod
    def cut_object(context, obj, cutter, mode):
        mod_name = "Cut Object"
        modifier = obj.modifiers.new(mod_name, type="BOOLEAN")
        modifier.operation = mode
        modifier.object = cutter
        context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=mod_name)

    @staticmethod
    def extrude_mesh_z(mesh, z, symmetric=False):
        vec = Vector((0, 0, z))
        bm = bmesh.new()
        bm.from_mesh(mesh)

        result = bmesh.ops.extrude_face_region(bm, geom=bm.faces[:])
        extruded_verts = [v for v in result["geom"] if isinstance(v, bmesh.types.BMVert)]
        if symmetric:
            bmesh.ops.translate(bm, vec=vec * 2, verts=extruded_verts)
            bmesh.ops.translate(bm, vec=-vec, verts=bm.verts[:])
        else:
            bmesh.ops.translate(bm, vec=vec, verts=extruded_verts)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])

        bm.to_mesh(mesh)
        bm.free()

    @staticmethod
    def translate_mesh_z(mesh, z):
        mesh.transform(Matrix.Translation((0, 0, z)))

    @staticmethod
    def apply_transformation(obj, matrix):
        obj.data.transform(matrix)
        for child in obj.children:
            child.matrix_basis = matrix @ child.matrix_basis

    @staticmethod
    def get_overlapping_faces(bm_faces, point, normal):
        overlapping_faces = []
        for face in bm_faces:
            if (1.0 - normal.dot(face.normal)) > 1e-4:
                continue

            direction = point - face.verts[0].co
            distance = abs(
                direction.normalized().dot(face.normal) * direction.length)
            if distance > 1e-4:
                continue

            overlapping_faces.append(face)

        return overlapping_faces

    @staticmethod
    def svg2img(svg_path, dpi):
        svg = SVGDOM.MakeFromStream(Stream.MakeFromFile(str(svg_path)))
        width, height = svg.containerSize()
        dpmm = dpi * INCH_TO_MM * SKIA_MAGIC
        pixels_width, pixels_height = round(width * dpmm), round(height * dpmm)
        surface = Surface(pixels_width, pixels_height)

        with surface as canvas:
            canvas.clear(Color4f.kWhite)
            canvas.scale(pixels_width / width, pixels_height / height)
            svg.render(canvas)

        with io.BytesIO(surface.makeImageSnapshot().encodeToData()) as file:
            image = Image.open(file)
            image.load()

        return image

    def draw(self, context):
        layout = self.layout

        layout.prop(self, "import_components")
        layout.prop(self, "center_pcb")
        layout.split()
        layout.prop(self, "cut_boards")
        layout.prop(self, "stack_boards")
        layout.split()

        layout.prop(self, "merge_materials")
        layout.prop(self, "enhance_materials")
        col = layout.column()
        col.enabled = self.enhance_materials
        col.label(text="PCB Material")
        col.prop(self, "pcb_material", text="")
        if self.pcb_material == "RASTERIZED":
            col.prop(self, "texture_dpi", slider=True)

        if has_svg2blender():
            layout.split()
            layout.prop(self, "import_fpnl")
            box = layout.box()
            box.enabled = self.import_fpnl
            box.prop(self, "fpnl_path")

            box.prop(self, "fpnl_thickness", slider=True)
            box.prop(self, "fpnl_bevel_depth", slider=True)
            box.prop(self, "fpnl_setup_camera")

            filebrowser_params = context.space_data.params
            filename  = Path(filebrowser_params.filename)
            directory = Path(filebrowser_params.directory.decode())

            if filename.suffix == ".pcb3d":
                if self.fpnl_path == "" or self.fpnl_path == self.last_fpnl_path:
                    auto_path = directory / (filename.stem + ".fpnl")
                    if auto_path.is_file():
                        self.fpnl_path = str(auto_path)
                        self.last_fpnl_path = self.fpnl_path
                    else:
                        self.fpnl_path = ""
            else:
                self.fpnl_path = ""

    def error(self, msg):
        print(f"error: {msg}")
        self.report({"ERROR"}, msg)
        return {"CANCELLED"}

    def warning(self, msg):
        print(f"warning: {msg}")
        self.report({"WARNING"}, msg)

PCB2_LAYER_NAMES = (
    "Board",
    "F_Cu",
    "F_Paste",
    "F_Mask",
    "B_Cu",
    "B_Paste",
    "B_Mask",
    "Vias",
    "F_Silk",
    "B_Silk",
)

MM_TO_M = 1e-3
M_TO_MM = 1e3
INCH_TO_MM = 1 / 25.4

FIX_X3D_SCALE = 2.54 * MM_TO_M
MATRIX_FIX_SCALE_INV = Matrix.Scale(FIX_X3D_SCALE, 4).inverted()

regex_filter_components = re.compile(
    r"(?P<prefix>Transform\s*{\s*"
    r"(?:rotation (?P<r>[^\n]*)\n)?\s*"
    r"(?:translation (?P<t>[^\n]*)\n)?\s*"
    r"(?:scale (?P<s>[^\n]*)\n)?\s*"
    r"children\s*\[\s*)"
    r"(?P<instances>(?:Transform\s*{\s*"
    r"(?:rotation [^\n]*\n)?\s*(?:translation [^\n]*\n)?\s*(?:scale [^\n]*\n)?\s*"
    r"children\s*\[\s*Inline\s*{\s*url\s*\"[^\"]*\"\s*}\s*]\s*}\s*)+)"
)

regex_component = re.compile(
    r"Transform\s*{\s*"
    r"(?:rotation (?P<r>[^\n]*)\n)?\s*"
    r"(?:translation (?P<t>[^\n]*)\n)?\s*"
    r"(?:scale (?P<s>[^\n]*)\n)?\s*"
    r"children\s*\[\s*Inline\s*{\s*url\s*\"(?P<url>[^\"]*)\"\s*}\s*]\s*}\s*"
)

def match2matrix(match):
    rotation    = match.group("r")
    translation = match.group("t")
    scale       = match.group("s")

    matrix = Matrix()
    if translation:
        translation = map(float, translation.split())
        matrix = matrix @ Matrix.Translation(translation)
    if rotation:
        rotation = tuple(map(float, rotation.split()))
        matrix = matrix @ Matrix.Rotation(rotation[3], 4, rotation[:3])
    if scale:
        scale = map(float, scale.split())
        matrix = matrix @ Matrix.Diagonal(scale).to_4x4()

    return matrix

@orientation_helper(axis_forward="Y", axis_up="Z")
class PCB2BLENDER_OT_import_x3d(bpy.types.Operator, ImportHelper):
    __doc__ = ImportX3D.__doc__
    bl_idname = "pcb2blender.import_x3d"
    bl_label = ImportX3D.bl_label
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ".x3d"
    filter_glob: StringProperty(default="*.x3d;*.wrl;*.wrz", options={"HIDDEN"})

    join:              BoolProperty(name="Join Shapes", default=True)
    tris_to_quads:     BoolProperty(name="Tris to Quads", default=True)
    enhance_materials: BoolProperty(name="Enhance Materials", default=True)
    scale:             FloatProperty(name="Scale", default=FIX_X3D_SCALE, precision=5)

    def execute(self, context):
        bpy.ops.object.select_all(action='DESELECT')

        objects_before = set(bpy.data.objects)
        matrix = axis_conversion(from_forward=self.axis_forward, from_up=self.axis_up).to_4x4()
        result = import_x3d.load(context, self.filepath, global_matrix=matrix)
        if not result == {"FINISHED"}:
            return result

        if not (objects := list(set(bpy.data.objects).difference(objects_before))):
            return {"FINISHED"}

        for obj in objects:
            obj.matrix_world = Matrix.Scale(self.scale, 4) @ obj.matrix_world
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]

        bpy.ops.object.shade_smooth()
        for obj in objects:
            if obj.type == "MESH":
                obj.data.use_auto_smooth = True

        if self.join:
            meshes = {obj.data for obj in objects if obj.type == "MESH"}
            if len(objects) > 1:
                bpy.ops.object.join()
            bpy.ops.object.transform_apply()
            for mesh in meshes:
                if not mesh.users:
                    bpy.data.meshes.remove(mesh)

            joined_obj = context.object
            joined_obj.name = Path(self.filepath).name.rsplit(".", 1)[0]
            joined_obj.data.name = joined_obj.name
            objects = [joined_obj]
        else:
            bpy.ops.object.transform_apply(location=False, rotation=False)

        if self.tris_to_quads:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.mesh.tris_convert_to_quads()
            bpy.ops.object.mode_set(mode="OBJECT")

        if self.enhance_materials:
            materials = sum((obj.data.materials[:] for obj in objects), [])
            merge_materials([obj.data for obj in objects])
            for material in materials[:]:
                if not material.users:
                    materials.remove(material)
                    bpy.data.materials.remove(material)
            enhance_materials(materials)

        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout

        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.prop(self, "join")
        layout.prop(self, "tris_to_quads")
        layout.prop(self, "enhance_materials")
        layout.split()
        layout.prop(self, "scale")

bases = X3D_PT_import_transform.__bases__
namespace = dict(X3D_PT_import_transform.__dict__)
del namespace["bl_rna"]
X3D_PT_import_transform_copy = type("X3D_PT_import_transform_copy", bases, namespace)
class PCB2BLENDER_PT_import_transform_x3d(X3D_PT_import_transform_copy):
    @classmethod
    def poll(cls, context):
        return context.space_data.active_operator.bl_idname == "PCB2BLENDER_OT_import_x3d"

def has_svg2blender():
    return addon_utils.check("svg2blender_importer") == (True, True)

def menu_func_import_pcb3d(self, context):
    self.layout.operator(PCB2BLENDER_OT_import_pcb3d.bl_idname, text="PCB (.pcb3d)")

def menu_func_import_x3d(self, context):
    self.layout.operator(PCB2BLENDER_OT_import_x3d.bl_idname,
        text="X3D Extensible 3D (.x3d/.wrl)")

classes = (
    PCB2BLENDER_OT_import_pcb3d,
    PCB2BLENDER_OT_import_x3d,
    PCB2BLENDER_PT_import_transform_x3d,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_x3d_original)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import_x3d)

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import_pcb3d)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_pcb3d)

    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_x3d)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import_x3d_original)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
