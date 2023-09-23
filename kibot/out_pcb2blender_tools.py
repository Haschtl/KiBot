# -*- coding: utf-8 -*-
# Copyright (c) 2023 Salvador E. Tropea
# Copyright (c) 2023 Instituto Nacional de Tecnología Industrial
# License: GPL-3.0
# Project: KiBot (formerly KiPlot)
# Some code is adapted from: https://github.com/30350n/pcb2blender
from dataclasses import dataclass, field
import json
import os
import re
import struct
from typing import List, Tuple
from enum import IntEnum
from pcbnew import B_Paste, F_Paste, PCB_TEXT_T, ToMM
from .gs import GS
from .misc import (MOD_THROUGH_HOLE, MOD_SMD, UI_VIRTUAL, W_UNKPCB3DTXT, W_NOPCB3DBR, W_NOPCB3DTL, W_BADPCB3DTXT,
                   W_UNKPCB3DNAME, W_BADPCB3DSTK, MISSING_TOOL)
from .optionable import Optionable
from .out_base import VariantOptions
from .macros import macros, document, output_class  # noqa: F401
from . import log

logger = log.get_logger()


@dataclass
class StackedBoard:
    """ Name and position of a stacked board """
    name: str
    offset: List[float]


@dataclass
class BoardDef:
    """ A sub-PCBs, its bounds and stacked boards """
    name: str
    bounds: List[float]
    stacked_boards: List[StackedBoard] = field(default_factory=list)

class KiCadColor(IntEnum):
    CUSTOM = 0
    GREEN  = 1
    RED    = 2
    BLUE   = 3
    PURPLE = 4
    BLACK  = 5
    WHITE  = 6
    YELLOW = 7

class SurfaceFinish(IntEnum):
    HASL = 0
    ENIG = 1
    NONE = 2

SURFACE_FINISH_MAP = {
    "ENIG": SurfaceFinish.ENIG,
    "ENEPIG": SurfaceFinish.ENIG,
    "Hard gold": SurfaceFinish.ENIG,
    "HT_OSP": SurfaceFinish.NONE,
    "OSP": SurfaceFinish.NONE,
}

def hex2rgb(hex_string):
    return (
        int(hex_string[0:2], 16),
        int(hex_string[2:4], 16),
        int(hex_string[4:6], 16),
    )

def parse_kicad_color(string):
    if string[0] == "#":
        return KiCadColor.CUSTOM, hex2rgb(string[1:7])
    else:
        return KiCadColor[string.upper()], (0, 0, 0)

@dataclass
class Stackup:
    thickness_mm: float = 1.6
    mask_color: KiCadColor = KiCadColor.GREEN
    mask_color_custom: Tuple[int, int, int] = (0, 0, 0)
    silks_color: KiCadColor = KiCadColor.WHITE
    silks_color_custom: Tuple[int, int, int] = (0, 0, 0)
    surface_finish: SurfaceFinish = SurfaceFinish.HASL

    def pack(self) -> bytes:
        return struct.pack("!fbBBBbBBBb",
            self.thickness_mm,
            self.mask_color, *self.mask_color_custom,
            self.silks_color, *self.silks_color_custom,
            self.surface_finish,
        )

def sanitized(name):
    """ Replace character that aren't alphabetic by _ """
    return re.sub(r"[\W]+", "_", name)


class PCB2Blender_ToolsOptions(VariantOptions):
    def __init__(self):
        with document:
            self.output = GS.def_global_output
            """ *Filename for the output (%i=pcb2blender, %x=pcb3d) """
            self.board_bounds_create = True
            """ Create the file that informs the size of the used PCB area.
                This is the bounding box reported by KiCad for the PCB edge with 1 mm of margin """
            self.board_bounds_dir = 'layers'
            """ Sub-directory where the bounds file is stored """
            self.board_bounds_file = 'bounds'
            """ Name of the bounds file """
            self.pads_info_create = True
            """ Create the files containing the PCB pads information """
            self.pads_info_dir = 'pads'
            """ Sub-directory where the pads info files are stored """
            self.stackup_create = True
            """ Create a JSON file containing the board stackup """
            self.stackup_file2 = 'stackup'
            """ Name for the stackup file """
            self.stackup_file = 'board.yaml'
            """ Name for the stackup file """
            self.stackup_dir = '.'
            """ Directory for the stackup file """
            self.sub_boards_create = True
            """ Extract sub-PCBs and their Z axis position """
            self.sub_boards_dir = 'boards'
            """ Directory for the boards definitions """
            self.sub_boards_bounds_file = 'bounds'
            """ File name for the sub-PCBs bounds """
            self.sub_boards_stacked_prefix = 'stacked_'
            """ Prefix used for the stack files """
            self.show_components = Optionable
            """ *[list(string)|string=all] [none,all] List of components to include in the pads list,
                can be also a string for `none` or `all`. The default is `all` """
        super().__init__()
        self._expand_id = 'pcb2blender'
        self._expand_ext = 'pcb3d'

    def config(self, parent):
        super().config(parent)
        # List of components
        self._show_all_components = False
        if isinstance(self.show_components, str):
            if self.show_components == 'all':
                self._show_all_components = True
            self.show_components = []
        elif isinstance(self.show_components, type):
            # Default is all
            self._show_all_components = True
        else:  # a list
            self.show_components = self.solve_kf_filters(self.show_components)

    def do_board_bounds(self, dir_name):
        if not self.board_bounds_create:
            return
        dir_name = os.path.join(dir_name, self.board_bounds_dir)
        os.makedirs(dir_name, exist_ok=True)
        fname = os.path.join(dir_name, self.board_bounds_file)
        # PCB bounding box using the PCB edge, converted to mm
        bounds = tuple(map(GS.to_mm, GS.get_rect_for(GS.board.ComputeBoundingBox(aBoardEdgesOnly=True))))
        # Apply 1 mm margin (x, y, w, h)
        bounds = (bounds[0]-1, bounds[1]-1, bounds[2]+2, bounds[3]+2)
        with open(fname, 'wb') as f:
            # Four big endian float numbers
            f.write(struct.pack("!ffff", *bounds))

    @staticmethod
    def is_not_virtual_ki6(m):
        return bool(m.GetAttributes() & (MOD_THROUGH_HOLE | MOD_SMD))

    @staticmethod
    def is_not_virtual_ki5(m):
        return bool(m.GetAttributes() != UI_VIRTUAL)

    def do_pads_info(self, dir_name):
        if not self.pads_info_create:
            return
        dir_name = os.path.join(dir_name, self.pads_info_dir)
        os.makedirs(dir_name, exist_ok=True)
        is_not_virtual = self.is_not_virtual_ki5 if GS.ki5 else self.is_not_virtual_ki6
        for i, footprint in enumerate(GS.get_modules()):
            has_model = len(footprint.Models()) > 0
            is_tht_or_smd = is_not_virtual(footprint)
            value = footprint.GetValue()
            value = value.replace('/', '_')
            reference = footprint.GetReference()
            for j, pad in enumerate(footprint.Pads()):
                name = os.path.join(dir_name, sanitized("{}_{}_{}_{}".format(value, reference, i, j)))
                is_flipped = pad.IsFlipped()
                has_paste = pad.IsOnLayer(B_Paste if is_flipped else F_Paste)
                with open(name, 'wb') as f:
                    f.write(struct.pack("!ff????BBffffBff",
                                        *map(GS.to_mm, pad.GetPosition()),
                                        is_flipped,
                                        has_model,
                                        is_tht_or_smd,
                                        has_paste,
                                        pad.GetAttribute(),
                                        pad.GetShape(),
                                        *map(GS.to_mm, pad.GetSize()),
                                        GS.get_pad_orientation_in_radians(pad),
                                        pad.GetRoundRectRadiusRatio(),
                                        pad.GetDrillShape(),
                                        *map(GS.to_mm, pad.GetDrillSize())))

    def do_stackup(self, dir_name):
        if not self.stackup_create or (not GS.global_pcb_finish and not GS.stackup):
            return
        dir_name = os.path.join(dir_name, self.stackup_dir)
        os.makedirs(dir_name, exist_ok=True)
        fname = os.path.join(dir_name, self.stackup_file)
        fname2 = os.path.join(dir_name, self.stackup_file2)
        # Create the board_info
        board_info = {}
        if GS.global_pcb_finish:
            board_info['copper_finish'] = GS.global_pcb_finish
        if GS.stackup:
            layers_parsed = []
            for la in GS.stackup:
                parsed_layer = {'name': la.name, 'type': la.type}
                if la.color is not None:
                    parsed_layer['color'] = la.color
                if la.thickness is not None:
                    parsed_layer['thickness'] = la.thickness/1000
                layers_parsed.append(parsed_layer)
            board_info['stackup'] = layers_parsed
        data = json.dumps(board_info, indent=3)
        logger.debug('Stackup: '+str(data))
        # Write the boardinfo
        stackup2=self.get_stackup(board_info).pack()
        with open(fname, 'wt') as f:
            f.write(data)
        with open(fname2, 'wb') as f:
            f.write(stackup2)

    def get_stackup(self, board_info):
        stackup = Stackup()
        for layer in board_info['stackup']:
            if layer["type"]=="core" and "thickness" in layer:
                stackup.thickness_mm =layer["thickness"]
            if layer["type"]=="Top Solder Mask":
                stackup.mask_color, stackup.mask_color_custom = parse_kicad_color(layer["color"])
            if layer["type"]=="Top Silk Screen":
                stackup.silks_color, stackup.silks_color_custom = parse_kicad_color(layer["color"])
        if "copper_finish" in board_info and board_info["copper_finish"]!="None":
            stackup.surface_finish = SURFACE_FINISH_MAP.get(board_info["copper_finish"], SurfaceFinish.HASL)

        return stackup

    def get_boarddefs(self):
        """ Extract the sub-PCBs and their positions using texts.
            This is the original mechanism and the code is from the plug-in. """
        boarddefs = {}
        tls = {}   # Top Left coordinates
        brs = {}   # Bottom right coordinates
        stacks = {}  # PCB stack relations
        # Collect the information from the texts
        for drawing in GS.board.GetDrawings():
            if drawing.Type() != PCB_TEXT_T:
                continue
            text = drawing.GetText()
            # Only process text starting with PCB3D_
            if not text.startswith("PCB3D_"):
                continue
            # Store the position of the text according to the declared type
            pos = tuple(map(ToMM, drawing.GetPosition()))
            if text.startswith("PCB3D_TL_"):
                tls.setdefault(text[9:], pos)
            elif text.startswith("PCB3D_BR_"):
                brs.setdefault(text[9:], pos)
            elif text.startswith("PCB3D_STACK_"):
                stacks.setdefault(text, pos)
            else:
                logger.warning(W_UNKPCB3DTXT+'Unknown PCB3D mark: `{}`'.format(text))
        # Separate the PCBs
        for name in tls.copy():
            # Look for the Bottom Right corner
            if name in brs:
                # Remove both
                tl_pos = tls.pop(name)
                br_pos = brs.pop(name)
                # Add a definition with the bbox (x, y, w, h)
                boarddef = BoardDef(sanitized(name), (tl_pos[0], tl_pos[1], br_pos[0]-tl_pos[0], br_pos[1]-tl_pos[1]))
                boarddefs[boarddef.name] = boarddef
            else:
                logger.warning(W_NOPCB3DBR+'PCB3D_TL_{} without corresponding PCB3D_BR_{}'.format(name, name))
        for name in brs.keys():
            logger.warning(W_NOPCB3DTL+'PCB3D_BR_{} without corresponding PCB3D_TL_{}'.format(name, name))
        # Solve the stack (relative positions)
        for stack_str in stacks.copy():
            # Extract the parameters
            try:
                other, onto, target, z_offset = stack_str[12:].split("_")
                z_offset = float(z_offset)
            except ValueError:
                onto = ''
            if onto != "ONTO":
                logger.warning(W_BADPCB3DTXT+'Malformed stack marker `{}` must be PCB3D_STACK_other_ONTO_target_zoffset'.
                               format(stack_str))
                continue
            # Check the names and sanity check
            other_name = sanitized(other)    # The name of the current board
            if other_name not in boarddefs and other_name != 'FPNL':
                logger.warning(W_UNKPCB3DNAME+'Unknown `{}` in `{}` valid names are: {}'.
                               format(other_name, stack_str, list(boarddefs)))
                continue
            target_name = sanitized(target)  # The name of the board below
            if target_name not in boarddefs:
                logger.warning(W_UNKPCB3DNAME+'Unknown `{}` in `{}` valid names are: {}'.
                               format(target_name, stack_str, list(boarddefs)))
                continue
            if target_name == other_name:
                logger.warning(W_BADPCB3DSTK+"Can't stack a board onto itself ({})".format(stack_str))
                continue
            # Add this board to the target
            stack_pos = stacks.pop(stack_str)
            target_pos = boarddefs[target_name].bounds[:2]
            stacked = StackedBoard(other_name, (stack_pos[0]-target_pos[0], stack_pos[1]-target_pos[1], z_offset))
            boarddefs[target_name].stacked_boards.append(stacked)
        return boarddefs

    def do_sub_boards(self, dir_name):
        if not self.sub_boards_create:
            return
        dir_name = os.path.join(dir_name, self.sub_boards_dir)
        os.makedirs(dir_name, exist_ok=True)
        boarddefs = self.get_boarddefs()
        logger.debug('Collected board definitions: '+str(boarddefs))
        for boarddef in boarddefs.values():
            subdir = os.path.join(dir_name, boarddef.name)
            os.makedirs(subdir, exist_ok=True)
            with open(os.path.join(subdir, self.sub_boards_bounds_file), 'wb') as f:
                f.write(struct.pack("!ffff", *boarddef.bounds))
            for stacked in boarddef.stacked_boards:
                with open(os.path.join(subdir, self.sub_boards_stacked_prefix+stacked.name), 'wb') as f:
                    f.write(struct.pack("!fff", *stacked.offset))

    def run(self, output):
        super().run(output)
        if GS.ki5:
            logger.error("`pcb2blender_tools` needs KiCad 6+")
            exit(MISSING_TOOL)
        dir_name = os.path.dirname(output)
        self.apply_show_components()
        self.filter_pcb_components(do_3D=True)
        self.do_board_bounds(dir_name)
        self.do_pads_info(dir_name)
        self.do_stackup(dir_name)
        self.do_sub_boards(dir_name)
        self.unfilter_pcb_components(do_3D=True)
        self.undo_show_components()

    def get_targets(self, out_dir):
        files = []
        if self.board_bounds_create:
            files.append(os.path.join(out_dir, self.board_bounds_dir, self.board_bounds_file))
        if self.pads_info_create:
            dir_name = os.path.join(out_dir, self.pads_info_dir)
            for i, footprint in enumerate(GS.get_modules()):
                value = footprint.GetValue()
                reference = footprint.GetReference()
                for j in range(len(footprint.Pads())):
                    files.append(os.path.join(dir_name, sanitized("{}_{}_{}_{}".format(value, reference, i, j))))
        if self.stackup_create and (GS.global_pcb_finish or GS.stackup):
            files.append(os.path.join(out_dir, self.stackup_dir, self.stackup_file))
            files.append(os.path.join(out_dir, self.stackup_dir, self.stackup_file2))
        if self.sub_boards_create:
            dir_name = os.path.join(out_dir, self.sub_boards_dir)
            boarddefs = self.get_boarddefs()
            for boarddef in boarddefs.values():
                subdir = os.path.join(dir_name, boarddef.name)
                files.append(os.path.join(subdir, self.sub_boards_bounds_file))
                for stacked in boarddef.stacked_boards:
                    files.append(os.path.join(subdir, self.sub_boards_stacked_prefix+stacked.name))
            else:
                files.append(dir_name)
        return files


@output_class
class PCB2Blender_Tools(BaseOutput):  # noqa: F821
    """ PCB2Blender Tools
        A bunch of tools used to generate PCB3D files used to export PCBs to Blender.
        Blender is the most important free software 3D render package.
        This output needs KiCad 6 or newer.
        The PCB3D file format is used by the PCB2Blender project (https://github.com/30350n/pcb2blender)
        to import KiCad PCBs in Blender.
        You need to install a Blender plug-in to load PCB3D files.
        The tools in this output are used by internal templates used to generate PCB3D files. """
    def __init__(self):
        super().__init__()
        self._category = 'PCB/3D/Auxiliar'
        with document:
            self.options = PCB2Blender_ToolsOptions
            """ *[dict] Options for the `pcb2blender_tools` output """
