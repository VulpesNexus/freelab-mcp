# -*- coding: utf-8 -*-
"""Spy fakes of the FontLab 7 objects the listener touches (Python 2.7 and 3).

Every fake native object allows only the members listed in its _members; any
other member, a subscript, or an iteration raises SpyViolation, which derives
from BaseException so the listener's `except Exception` cannot hide it. That
keeps the listener on the calls listed in docs/FONTLAB7-API.md.
"""

from __future__ import absolute_import, division, print_function

import sys
import types


class SpyViolation(BaseException):
    pass


class Native(object):
    _members = ()

    def __getattribute__(self, name):
        if name.startswith("__") or name.startswith("_f_") or name == "_members":
            return object.__getattribute__(self, name)
        if name not in object.__getattribute__(self, "_members"):
            raise SpyViolation("%s.%s is not a member the listener may use" % (type(self).__name__, name))
        return object.__getattribute__(self, name)

    def __getitem__(self, key):
        raise SpyViolation("subscript on native %s" % type(self).__name__)

    def __iter__(self):
        raise SpyViolation("iteration over native %s" % type(self).__name__)

    def __contains__(self, item):
        raise SpyViolation("'in' on native %s" % type(self).__name__)


# Qt value types: recognized by the listener by class name.
class QRectF(object):
    def __init__(self, x, y, w, h):
        self._v = (x, y, w, h)

    def x(self):
        return self._v[0]

    def y(self):
        return self._v[1]

    def width(self):
        return self._v[2]

    def height(self):
        return self._v[3]


class QTransform(object):
    def __init__(self, m11=1.0, m12=0.0, m21=0.0, m22=1.0, dx=0.0, dy=0.0):
        self._v = (m11, m12, m21, m22, dx, dy)

    def m11(self):
        return self._v[0]

    def m12(self):
        return self._v[1]

    def m21(self):
        return self._v[2]

    def m22(self):
        return self._v[3]

    def dx(self):
        return self._v[4]

    def dy(self):
        return self._v[5]


class flNode(Native):
    _members = ("x", "y", "type", "smooth")

    def __init__(self, x, y, kind="on", smooth=False):
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "type", kind)
        object.__setattr__(self, "smooth", smooth)


class flContour(Native):
    _members = ("closed", "nodes")

    def __init__(self, nodes, closed=True):
        object.__setattr__(self, "_f_nodes", list(nodes))
        object.__setattr__(self, "closed", closed)

    def nodes(self):
        return list(self._f_nodes)


class flShape(Native):
    _members = ("name", "isComponent", "transform", "contours")

    def __init__(self, contours, name="", transform=None, component=False):
        object.__setattr__(self, "contours", list(contours))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "transform", transform or QTransform())
        object.__setattr__(self, "isComponent", component)


class flLayer(Native):
    _members = ("name", "shapes", "advanceWidth", "boundingBox", "shapesCount", "nodesCount", "isNull")

    def __init__(self, name, shapes=(), width=500.0, bbox=None):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "shapes", list(shapes))
        object.__setattr__(self, "advanceWidth", width)
        object.__setattr__(self, "boundingBox", bbox or QRectF(0, 0, 0, 0))
        object.__setattr__(self, "shapesCount", len(shapes))
        object.__setattr__(self, "isNull", False)

    def nodesCount(self):  # a method in FontLab 7's reference: int nodesCount()
        return sum(len(c.nodes()) for s in self.shapes for c in s.contours)


class fgGlyph(Native):
    _members = ("unicodes",)

    def __init__(self, unicodes):
        object.__setattr__(self, "unicodes", list(unicodes))


class flGlyph(Native):
    _members = ("name", "layers", "activeLayer", "fgGlyph", "mark", "tags", "note", "isNull")

    def __init__(self, name, layers, unicodes=(), active=None):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "layers", list(layers))
        object.__setattr__(self, "activeLayer", active if active is not None else (layers[0] if layers else None))
        object.__setattr__(self, "fgGlyph", fgGlyph(unicodes))
        object.__setattr__(self, "mark", 0)
        object.__setattr__(self, "tags", [])
        object.__setattr__(self, "note", "")
        object.__setattr__(self, "isNull", False)


class fgFont(Native):
    _members = ("keys",)

    def __init__(self, names):
        object.__setattr__(self, "_f_names", list(names))

    def keys(self):
        return list(self._f_names)


class flPackage(Native):
    _members = (
        "tfn", "tsn", "psn", "ffn", "path", "upm", "glyphsCount", "master", "mastersCount",
        "versionMajor", "versionMinor", "hasChanges", "fgPackage", "findName", "findUnicode", "isNull",
    )

    def __init__(self, family, style, glyphs, master="Regular", path="fixtures/Test.vfc"):
        object.__setattr__(self, "_f_glyphs", list(glyphs))
        object.__setattr__(self, "tfn", family)
        object.__setattr__(self, "tsn", style)
        object.__setattr__(self, "psn", (family + "-" + style).replace(" ", ""))
        object.__setattr__(self, "ffn", family + " " + style)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "upm", 1000.0)
        object.__setattr__(self, "glyphsCount", len(glyphs))
        object.__setattr__(self, "master", master)
        object.__setattr__(self, "mastersCount", 1)
        object.__setattr__(self, "versionMajor", 1)
        object.__setattr__(self, "versionMinor", 0)
        object.__setattr__(self, "hasChanges", False)
        object.__setattr__(self, "fgPackage", fgFont([g.name for g in glyphs]))
        object.__setattr__(self, "isNull", False)

    def findName(self, name):
        for glyph in self._f_glyphs:
            if glyph.name == name:
                return glyph
        return None

    def findUnicode(self, codepoint):
        for glyph in self._f_glyphs:
            if codepoint in glyph.fgGlyph.unicodes:
                return glyph
        return None


class _BrokenPath(flPackage):
    """A package whose path read fails, as a native read sometimes does."""

    def __getattribute__(self, name):
        if name == "path":
            raise RuntimeError("path is not available")
        return flPackage.__getattribute__(self, name)


class NullUnknown(flGlyph):
    """A glyph whose isNull read fails."""

    def __getattribute__(self, name):
        if name == "isNull":
            raise RuntimeError("isNull is not available")
        return flGlyph.__getattribute__(self, name)


class _PackageObject(Native):
    """What flWorkspace.packages() returns in FontLab 7: generic flObject items.

    unsaved: True (hasChanges), "history" (only hasHistoryStates: an edit, as FontLab 7.2 reports one
    live), or False."""
    _members = ("hasChanges", "hasHistoryStates")

    def __init__(self, unsaved):
        object.__setattr__(self, "_f_unsaved", unsaved)

    def __getattribute__(self, name):
        if name == "hasChanges":
            return object.__getattribute__(self, "_f_unsaved") is True
        if name == "hasHistoryStates":
            return object.__getattribute__(self, "_f_unsaved") is not False
        return Native.__getattribute__(self, name)


class _Workspace(Native):
    _members = ("currentPackage", "hasChanges", "packages")

    def __init__(self, state):
        object.__setattr__(self, "_f_state", state)

    def __getattribute__(self, name):
        state = object.__getattribute__(self, "_f_state")
        if name == "currentPackage":
            return state.current
        if name == "hasChanges":
            return state.workspace_changed
        if name == "packages":  # a method in FontLab 7: flWorkspace.packages()
            return lambda: [_PackageObject(unsaved) for unsaved in state.unsaved]
        return Native.__getattribute__(self, name)


class FontLabState(object):
    def __init__(self):
        self.fonts = []
        self.current = None
        self.workspace_changed = False
        self.unsaved = []  # one flag per open package


def square(x0, y0, size):
    return flContour([flNode(x0, y0), flNode(x0 + size, y0), flNode(x0 + size, y0 + size), flNode(x0, y0 + size)])


def sample_font(broken_path=False):
    a_layer = flLayer("Regular", [flShape([square(50, 0, 400)], name="A body")], width=500.0, bbox=QRectF(50, 0, 400, 400))
    a_mask = flLayer("Regular mask")
    curve = flContour([
        flNode(0, 0), flNode(10, 50, "off"), flNode(40, 100, "off"), flNode(100, 100, "curve", True), flNode(100, 0),
    ])
    o_layer = flLayer("Regular", [flShape([curve], name="", transform=QTransform(1, 0, 0.2, 1, 10, 0))], width=600.0, bbox=QRectF(0, 0, 100, 100))
    space = flLayer("Regular", [], width=250.0)
    b_layer = flLayer("Regular", [flShape([square(0, 0, 100), square(0, 200, 100)])], width=300.0, bbox=QRectF(0, 0, 100, 300))
    glyphs = [
        flGlyph("A", [a_layer, a_mask], [0x41]),
        flGlyph("o", [o_layer], [0x6F]),
        flGlyph("space", [space], [0x20]),
        flGlyph("B", [b_layer], [0x42]),
    ]
    cls = _BrokenPath if broken_path else flPackage
    return cls("Free Sans", "Regular", glyphs)


def install(state):
    """Put fake fontlab and fontgate modules into sys.modules; returns the fontlab module."""
    fontlab = types.ModuleType("fontlab")
    workspace = _Workspace(state)

    class flWorkspace(object):
        @staticmethod
        def instance():
            return workspace

    fontlab.flWorkspace = flWorkspace
    fontlab.CountFonts = lambda: len(state.fonts)
    fontlab.ApplicationPath = lambda: "FontLab 7 (fake)"
    sys.modules["fontlab"] = fontlab
    sys.modules["fontgate"] = types.ModuleType("fontgate")
    return fontlab


def uninstall():
    for name in ("fontlab", "fontgate"):
        sys.modules.pop(name, None)
