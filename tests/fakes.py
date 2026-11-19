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
    _settable = ()  # members the listener may assign to

    def __getattribute__(self, name):
        if name.startswith("__") or name.startswith("_f_") or name in ("_members", "_settable"):
            return object.__getattribute__(self, name)
        if name not in object.__getattribute__(self, "_members"):
            raise SpyViolation("%s.%s is not a member the listener may use" % (type(self).__name__, name))
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not name.startswith("_f_") and name not in object.__getattribute__(self, "_settable"):
            raise SpyViolation("%s.%s is not a member the listener may set" % (type(self).__name__, name))
        object.__setattr__(self, name, value)

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


class QPointF(object):
    def __init__(self, x=0.0, y=0.0):
        self._v = (float(x), float(y))

    def x(self):
        return self._v[0]

    def y(self):
        return self._v[1]


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

    def translate(self, dx, dy):  # (a plain translation is all the listener builds)
        m11, m12, m21, m22, x, y = self._v
        return QTransform(m11, m12, m21, m22, x + dx, y + dy)


class flNode(Native):
    _members = ("x", "y", "type", "smooth")
    _settable = ("type", "smooth")  # (set on the nodes of a contour made from points: set_outline)

    def __init__(self, x, y, kind="on", smooth=False):
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "type", kind)
        object.__setattr__(self, "smooth", smooth)
        object.__setattr__(self, "_f_contour", None)

    def __setattr__(self, name, value):
        """FontLab keeps smooth only on a node on the outline with a handle beside it (live, 2026-11-19:
        canBeSmooth false, and smooth read back false, on a node between two lines, even a straight one)."""
        contour = object.__getattribute__(self, "_f_contour")
        if name == "smooth" and value and contour is not None:
            nodes = contour._f_nodes
            i = nodes.index(self)
            neighbors = [nodes[j % len(nodes)] for j in (i - 1, i + 1) if contour.closed or 0 <= j < len(nodes)]
            if self.type != "on" or all(n.type == "on" for n in neighbors):
                return
        Native.__setattr__(self, name, value)


class flContour(Native):
    _members = ("closed", "nodes", "clone")
    _settable = ("closed",)

    def __init__(self, nodes, closed=True):
        object.__setattr__(self, "_f_nodes", list(nodes))
        object.__setattr__(self, "closed", closed)
        for node in self._f_nodes:
            object.__setattr__(node, "_f_contour", self)

    def nodes(self):
        return list(self._f_nodes)

    def clone(self):
        return flContour([flNode(n.x, n.y, n.type, n.smooth) for n in self._f_nodes], self.closed)


def contour_of_points(points):
    """fontlab.flContour(list of QPointF): every node on the curve, the contour open (FontLab 7's reference)."""
    if not isinstance(points, list) or not all(type(p).__name__ == "QPointF" for p in points):
        raise TypeError("flContour takes a list of QPointF")
    return flContour([flNode(p.x(), p.y()) for p in points], False)


class flShapeData(Native):
    _members = ("componentGlyph",)

    def __init__(self, component_glyph=""):
        object.__setattr__(self, "componentGlyph", component_glyph)


class flShape(Native):
    _members = ("name", "isComponent", "transform", "contours", "shapeData", "clearContours", "addContours", "update")

    def __init__(self, contours, name="", transform=None, component=False, component_glyph=""):
        object.__setattr__(self, "_f_contours", list(contours))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "transform", transform or QTransform())
        object.__setattr__(self, "isComponent", component)
        object.__setattr__(self, "shapeData", flShapeData(component_glyph if component else ""))
        object.__setattr__(self, "_f_round", False)  # True: FontLab keeps whole coordinates
        object.__setattr__(self, "_f_drop", False)  # True: addContours keeps nothing
        object.__setattr__(self, "_f_updates", 0)

    @property
    def contours(self):
        return list(self._f_contours)

    def clearContours(self):
        object.__setattr__(self, "_f_contours", [])

    def addContours(self, contours, notify):
        if self._f_drop:
            return
        for contour in contours:
            if self._f_round:
                for node in contour.nodes():
                    object.__setattr__(node, "x", float(round(node.x)))
                    object.__setattr__(node, "y", float(round(node.y)))
            self._f_contours.append(contour)

    def update(self):
        object.__setattr__(self, "_f_updates", self._f_updates + 1)


class flPinPoint(Native):
    """An anchor: fontlab.flPinPoint(QPointF), with a settable name and point."""
    _members = ("name", "point")
    _settable = ("name", "point")

    def __init__(self, point, name=""):
        object.__setattr__(self, "point", point)
        object.__setattr__(self, "name", name)


class flLayer(Native):
    _members = ("name", "shapes", "advanceWidth", "boundingBox", "shapesCount", "nodesCount", "isNull", "inkBox",
                "anchors", "findAnchor", "addAnchor", "removeAnchor", "applyTransform", "metricsLeft", "metricsRight",
                "metricsWidth", "setGlyphComponents", "addShape", "removeShape", "update")
    _settable = ("advanceWidth",)

    def __init__(self, name, shapes=(), width=500.0, bbox=None, anchors=(), ink=None):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "_f_shapes", list(shapes))
        object.__setattr__(self, "_f_width", float(width))
        object.__setattr__(self, "_f_round", False)  # FontLab keeps whole advance widths: set True to act so
        object.__setattr__(self, "boundingBox", bbox or QRectF(0, 0, 0, 0))
        object.__setattr__(self, "_f_ink", ink)
        object.__setattr__(self, "_f_anchors", list(anchors))
        object.__setattr__(self, "isNull", False)
        object.__setattr__(self, "metricsLeft", "")
        object.__setattr__(self, "metricsRight", "")
        object.__setattr__(self, "metricsWidth", "")
        object.__setattr__(self, "_f_package", None)
        object.__setattr__(self, "_f_refuse_width", False)  # True: advanceWidth writes are not kept
        object.__setattr__(self, "_f_raise_transform", False)  # True: applyTransform raises
        object.__setattr__(self, "_f_anchor_drift", 0.0)  # anchors added land this far off

    @property
    def shapes(self):
        return list(self._f_shapes)

    @property
    def shapesCount(self):
        return len(self._f_shapes)

    @property
    def anchors(self):
        return list(self._f_anchors)

    def _get_width(self):
        return self._f_width

    def _set_width(self, value):
        if self._f_refuse_width:
            return
        object.__setattr__(self, "_f_width", float(round(value)) if self._f_round else float(value))

    advanceWidth = property(_get_width, _set_width)

    def nodesCount(self):  # a method in FontLab 7's reference: int nodesCount()
        return sum(len(c.nodes()) for s in self._f_shapes for c in s.contours)

    def inkBox(self):
        """The on-curve extremes, each shape moved by its transform's offset (a stand-in for FontLab's ink box)."""
        if self._f_ink is not None:
            return self._f_ink
        xs, ys = [], []
        for shape in self._f_shapes:
            for contour in shape.contours:
                for node in contour.nodes():
                    if node.type != "off":
                        xs.append(node.x + shape.transform.dx())
                        ys.append(node.y + shape.transform.dy())
        if not xs:
            return QRectF(0, 0, 0, 0)
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def addShape(self, shape):
        self._f_shapes.append(shape)
        return shape

    def removeShape(self, shape, recursive=True):
        self._f_shapes.remove(shape)

    def update(self):
        pass

    def findAnchor(self, name):
        for anchor in self._f_anchors:
            if anchor.name == name:
                return anchor
        return None

    def addAnchor(self, anchor):
        if self._f_anchor_drift:
            anchor.point = QPointF(anchor.point.x() + self._f_anchor_drift, anchor.point.y())
        self._f_anchors.append(anchor)

    def removeAnchor(self, anchor):
        self._f_anchors.remove(anchor)

    def applyTransform(self, transform):
        """A translation, as FontLab applies it: outlines' nodes, components' offsets, and anchors move."""
        if self._f_raise_transform:
            raise RuntimeError("applyTransform failed")
        dx, dy = transform.dx(), transform.dy()
        object.__setattr__(self, "_f_ink", None if self._f_ink is None else QRectF(
            self._f_ink.x() + dx, self._f_ink.y() + dy, self._f_ink.width(), self._f_ink.height()))
        for shape in self._f_shapes:
            if shape.isComponent:
                object.__setattr__(shape, "transform", shape.transform.translate(dx, dy))
                continue
            for contour in shape.contours:
                for node in contour.nodes():
                    object.__setattr__(node, "x", node.x + dx)
                    object.__setattr__(node, "y", node.y + dy)
        for anchor in self._f_anchors:
            anchor.point = QPointF(anchor.point.x() + dx, anchor.point.y() + dy)

    def setGlyphComponents(self, components, width, package, colorize):
        """Components of the package's glyphs; FontLab keeps none of them where one's glyph has no such layer."""
        shapes = []
        for component in components:
            base = package._f_find(component._f_name)
            layer = None if base is None else base._f_layer(self.name)
            if layer is None:
                return False
            contours = [c for s in layer.shapes for c in s.contours]
            shapes.append(flShape(contours, transform=component.transform, component=True,
                                  component_glyph=component._f_name))
        if package._f_drop_components:
            shapes = shapes[:-1]
        object.__setattr__(self, "_f_shapes", shapes)
        self.advanceWidth = width
        return True


class fgGlyph(Native):
    _members = ("unicodes",)
    _settable = ("unicodes",)

    def __init__(self, unicodes, owner=None):
        object.__setattr__(self, "_f_unicodes", list(unicodes))
        object.__setattr__(self, "_f_owner", owner)
        object.__setattr__(self, "_f_layers_when_set", None)  # how many layers its glyph had when they were set

    def _get(self):
        return list(self._f_unicodes)

    def _set(self, values):
        object.__setattr__(self, "_f_unicodes", list(values))
        owner = self._f_owner
        object.__setattr__(self, "_f_layers_when_set", None if owner is None else len(owner._f_layers))

    unicodes = property(_get, _set)


class flGlyph(Native):
    _members = ("name", "layers", "activeLayer", "fgGlyph", "mark", "tags", "note", "isNull", "getLayerByName",
                "update", "currentHistoryState", "initialHistoryState")
    _settable = ("name",)

    def __init__(self, name, layers, unicodes=(), active=None):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "_f_layers", list(layers))
        object.__setattr__(self, "activeLayer", active if active is not None else (layers[0] if layers else None))
        object.__setattr__(self, "fgGlyph", fgGlyph(unicodes, self))
        object.__setattr__(self, "mark", 0)
        object.__setattr__(self, "tags", [])
        object.__setattr__(self, "note", "")
        object.__setattr__(self, "isNull", False)
        object.__setattr__(self, "_f_states", [0])
        object.__setattr__(self, "_f_snapshots", [])
        object.__setattr__(self, "_f_updates", 0)

    @property
    def layers(self):
        return list(self._f_layers) if isinstance(self._f_layers, list) else self._f_layers  # (a test's native stand-in)

    def _f_layer(self, name):
        for layer in self._f_layers:
            if layer.name == name:
                return layer
        return None

    def getLayerByName(self, name, create=False):
        layer = self._f_layer(name)
        if layer is None and create:
            layer = flLayer(name, width=0.0)
            self._f_layers.append(layer)
        return layer

    def update(self):
        object.__setattr__(self, "_f_updates", self._f_updates + 1)

    def currentHistoryState(self):
        return self._f_states[-1]

    def initialHistoryState(self):
        return 0


class fgFeatures(Native):
    """fgPackage.features: the feature code, by tag, in FontLab's order, and its prefix."""
    _members = ("keys", "get_feature", "set_feature", "get_prefix", "set_prefix", "remove")

    def __init__(self, features=(), prefix=""):
        object.__setattr__(self, "_f_features", list(features))
        object.__setattr__(self, "_f_prefix", prefix)
        object.__setattr__(self, "_f_refuse", None)
        object.__setattr__(self, "_f_garble_skip", None)  # code stored right even when garbling (a restore)

    def keys(self):
        return [tag for tag, _code in self._f_features]

    def get_feature(self, tag):
        for key, code in self._f_features:
            if key == tag:
                return code
        return ""

    def set_feature(self, tag, code):
        """Stored as given, ending with a line break (FontLab adds one, live); _f_refuse: "drop" keeps
        nothing, "garble" keeps something else."""
        if self._f_refuse == "drop":
            return
        if self._f_refuse == "garble" and code != self._f_garble_skip:
            code = code + "# garbled"
        elif not code.endswith("\n"):
            code = code + "\n"
        for index, (key, _code) in enumerate(self._f_features):
            if key == tag:
                self._f_features[index] = (tag, code)
                return
        self._f_features.append((tag, code))

    def _f_class_lines(self):
        owner = getattr(self, "_f_owner", None)
        return [u"@%s = [%s];" % (name, u" ".join(glyphs)) for name, glyphs in (owner._f_classes if owner else [])]

    def get_prefix(self):
        """The prefix after one line per class of the Classes panel (live, 2026-11-19)."""
        lines = self._f_class_lines()
        return u"".join(line + u"\n" for line in lines) + self._f_prefix

    def set_prefix(self, code):
        """Given back with the Classes panel's lines at its start, or without them, the prefix is the same
        (live): FontLab writes those lines from the panel."""
        lines = self._f_class_lines()
        head = u"".join(line + u"\n" for line in lines)
        if lines and code.startswith(head):
            code = code[len(head):]
        object.__setattr__(self, "_f_prefix", code)

    def remove(self, tag):
        object.__setattr__(self, "_f_features", [(key, code) for key, code in self._f_features if key != tag])


class fgFont(Native):
    _members = ("keys", "id", "features", "rebuildMaps", "save")

    def __init__(self, names, package=None, font_id=1, features=None):
        object.__setattr__(self, "_f_names", list(names))
        object.__setattr__(self, "_f_package", package)
        object.__setattr__(self, "id", font_id)
        object.__setattr__(self, "features", features or fgFeatures())
        object.__setattr__(self, "_f_rebuilds", 0)
        object.__setattr__(self, "_f_saves", [])

    def save(self, path, save_vfb=False):
        """fgFont.save: a VFB file, or a UFO folder."""
        import os
        self._f_saves.append((path, save_vfb))
        if save_vfb:
            with open(path, "wb") as handle:
                handle.write(b"vfb")
            return
        if not os.path.isdir(path):
            os.mkdir(path)
        with open(os.path.join(path, "metainfo.plist"), "wb") as handle:
            handle.write(b"ufo %d" % len(self._f_saves))

    def keys(self):
        if self._f_package is not None:
            return [g.name for g in self._f_package._f_glyphs]
        return list(self._f_names)

    def rebuildMaps(self):
        object.__setattr__(self, "_f_rebuilds", self._f_rebuilds + 1)
        if self._f_package is not None:
            self._f_package._f_rebuild()


class fgKerningObject(Native):
    _members = ("id", "mode")

    def __init__(self, ident, mode):
        object.__setattr__(self, "id", ident)
        object.__setattr__(self, "mode", mode)


class fgKerningObjectPair(Native):
    _members = ("left", "right")

    def __init__(self, left, right):
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "right", right)


class fgKerningGroups(Native):
    _members = ("asDict",)

    def __init__(self, groups):
        object.__setattr__(self, "_f_groups", groups)
        object.__setattr__(self, "_f_refuse", False)  # True: class writes are not kept

    def asDict(self):
        return dict((name, (list(members), side)) for name, (members, side) in self._f_groups.items())

    def __setitem__(self, name, value):
        """groups[name] = (members, side): makes a class, or replaces one and keeps its pairs (live, 2026-11-08)."""
        if self._f_refuse:
            return
        members, side = value
        self._f_groups[name] = (list(members), side)


class fgKerning(Native):
    """flPackage.kerning(master). A name that is also a class's is taken as the class, as FontLab does
    (live, 2026-11-08): pairs are keyed (name, mode) on each side."""
    _members = ("keys", "items", "get", "remove", "groups", "getPlainPairsDict")

    def getPlainPairsDict(self):
        """Every kerned glyph pair, classes flattened, the most specific entry winning (as FontLab, live)."""
        if self._f_plain == "raise":
            raise RuntimeError("getPlainPairsDict failed")
        plain = {}

        def members(name, mode):
            return list(self._f_groups[name][0]) if mode == "groupMode" else [name]
        def rank(entry):  # (@class @class, then @class glyph, glyph @class, glyph glyph: the last one wins)
            return (entry[0][1] == "glyphMode", entry[1][1] == "glyphMode")
        for left, right, value in sorted(self._f_pairs, key=rank):
            for l in members(*left):
                for r in members(*right):
                    plain[(l, r)] = value + (self._f_plain or 0.0)
        return plain

    def __init__(self, groups=None, pairs=()):
        object.__setattr__(self, "_f_groups", dict(groups or {}))
        object.__setattr__(self, "groups", fgKerningGroups(self._f_groups))
        object.__setattr__(self, "_f_pairs", [])  # [((left, mode), (right, mode), value)]
        object.__setattr__(self, "_f_drop", False)  # True: writes are not kept
        object.__setattr__(self, "_f_round", False)  # True: values kept in whole units, as FontLab does
        object.__setattr__(self, "_f_skew", 0.0)  # every value written lands this far off (restores too)
        object.__setattr__(self, "_f_plain", None)  # "raise": getPlainPairsDict raises; a number: its values that far off
        for left, right, value in pairs:
            self._f_pairs.append((left, right, float(value)))

    def _f_key(self, name):
        return (name, "groupMode" if name in self._f_groups else "glyphMode")

    def _f_find_pair(self, pair):
        left, right = self._f_key(pair[0]), self._f_key(pair[1])
        for index, (l, r, _v) in enumerate(self._f_pairs):
            if l == left and r == right:
                return index
        return None

    def keys(self):
        return [fgKerningObjectPair(fgKerningObject(*l), fgKerningObject(*r)) for l, r, _v in self._f_pairs]

    def items(self):
        return [(fgKerningObjectPair(fgKerningObject(*l), fgKerningObject(*r)), v) for l, r, v in self._f_pairs]

    def get(self, pair):
        index = self._f_find_pair(pair)
        return None if index is None else self._f_pairs[index][2]

    def __setitem__(self, pair, value):
        if self._f_drop:
            return
        index = self._f_find_pair(pair)
        value = value + self._f_skew
        entry = (self._f_key(pair[0]), self._f_key(pair[1]), float(round(value) if self._f_round else value))
        if index is None:
            self._f_pairs.append(entry)
        else:
            self._f_pairs[index] = entry

    def remove(self, pair):
        index = self._f_find_pair(pair)
        if index is not None and not self._f_drop:
            del self._f_pairs[index]


class flPackage(Native):
    _members = (
        "tfn", "tsn", "psn", "ffn", "path", "upm", "glyphsCount", "master", "mastersCount", "masters",
        "versionMajor", "versionMinor", "hasChanges", "fgPackage", "findName", "findUnicode", "isNull",
        "kerning", "getAllGroups", "renameGlyph", "addGlyph", "deleteGlyph", "setFlag", "currentHistoryState",
        "initialHistoryState",
    )

    def __init__(self, family, style, glyphs, master="Regular", path="fixtures/Test.vfc", masters=None, font_id=1,
                 kerning=None, classes=(), features=None):
        object.__setattr__(self, "_f_glyphs", list(glyphs))
        object.__setattr__(self, "tfn", family)
        object.__setattr__(self, "tsn", style)
        object.__setattr__(self, "psn", (family + "-" + style).replace(" ", ""))
        object.__setattr__(self, "ffn", family + " " + style)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "upm", 1000.0)
        object.__setattr__(self, "master", master)
        object.__setattr__(self, "masters", list(masters or [master]))
        object.__setattr__(self, "mastersCount", len(self.masters))
        object.__setattr__(self, "versionMajor", 1)
        object.__setattr__(self, "versionMinor", 0)
        object.__setattr__(self, "hasChanges", False)
        object.__setattr__(self, "fgPackage", fgFont([g.name for g in glyphs], self, font_id, features))
        object.__setattr__(self.fgPackage.features, "_f_owner", self)
        object.__setattr__(self, "isNull", False)
        object.__setattr__(self, "_f_kerning", dict(kerning or {}))
        object.__setattr__(self, "_f_classes", list(classes))  # feature classes: (name, glyph names)
        object.__setattr__(self, "_f_flags", [])
        object.__setattr__(self, "_f_states", [0])
        object.__setattr__(self, "_f_snapshots", [])
        object.__setattr__(self, "_f_drop_components", False)
        object.__setattr__(self, "_f_refuse_delete", False)
        object.__setattr__(self, "_f_rename_ignored", False)
        object.__setattr__(self, "_f_unicode_map", {})
        self._f_rebuild()

    @property
    def glyphsCount(self):
        return len(self._f_glyphs)

    def _f_rebuild(self):
        """FontLab's code point lookup: rebuilt by fgPackage.rebuildMaps(), not when a glyph's values change."""
        mapping = {}
        for glyph in self._f_glyphs:
            for value in glyph.fgGlyph._f_unicodes:
                mapping.setdefault(value, glyph)
        object.__setattr__(self, "_f_unicode_map", mapping)
        object.__setattr__(self, "_f_name_map", dict((g.name, g) for g in self._f_glyphs))

    def _f_find(self, name):
        for glyph in self._f_glyphs:
            if glyph.name == name:
                return glyph
        return None

    def findName(self, name):
        """FontLab's name lookup, from a map rebuildMaps() rebuilds: after a rename it still finds the glyph
        under its old name and not under its new one (the stale lookup a write must not trust)."""
        found = self._f_name_map.get(name)
        return found if found is not None and found in self._f_glyphs else None

    def findUnicode(self, codepoint):
        return self._f_unicode_map.get(codepoint)

    def kerning(self, master):
        if master not in self._f_kerning:
            self._f_kerning[master] = fgKerning()
        return self._f_kerning[master]

    def getAllGroups(self):
        groups = [(False, name, i, 0, list(members)) for i, (name, members) in enumerate(self._f_classes)]
        first = self._f_kerning.get(self.masters[0])
        if first is not None:
            for name, (members, _side) in sorted(first._f_groups.items()):
                groups.append((True, name, 100 + len(groups), 0, list(members)))
        return groups

    def renameGlyph(self, glyph, name, rename):
        """Renames the glyph whose fgGlyph is given; with rename, the components that use it follow."""
        if self._f_rename_ignored:
            return True  # (says it renamed, and did not)
        for item in self._f_glyphs:
            if item.fgGlyph is glyph:
                old = item.name
                object.__setattr__(item, "name", name)
                if rename:
                    for other in self._f_glyphs:
                        for layer in other._f_layers:
                            for shape in layer._f_shapes:
                                if shape.isComponent and shape.shapeData.componentGlyph == old:
                                    object.__setattr__(shape.shapeData, "componentGlyph", name)
                return True
        return False

    def addGlyph(self, glyph):
        """FontLab adds a copy of the glyph, without its layers: they are made on the font's glyph."""
        self._f_glyphs.append(flGlyph(glyph.name, [], glyph.fgGlyph._f_unicodes))
        self._f_name_map[glyph.name] = self._f_glyphs[-1]

    def deleteGlyph(self, glyph):
        if self._f_refuse_delete:
            return False
        self._f_glyphs.remove(glyph)
        return True

    def setFlag(self, flag, value=True):
        self._f_flags.append((flag, value))

    def currentHistoryState(self):
        return self._f_states[-1]

    def initialHistoryState(self):
        return 0


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
    """What flWorkspace.packages() returns in FontLab 7: generic flObject items, which fontlab.flPackage
    wraps.

    unsaved: True (hasChanges), "history" (only hasHistoryStates: an edit, as FontLab 7.2 reports one
    live), or False."""
    _members = ("hasChanges", "hasHistoryStates")

    def __init__(self, unsaved, font=None):
        object.__setattr__(self, "_f_unsaved", unsaved)
        object.__setattr__(self, "_f_font", font)

    def __getattribute__(self, name):
        if name == "hasChanges":
            return object.__getattribute__(self, "_f_unsaved") is True
        if name == "hasHistoryStates":
            return object.__getattribute__(self, "_f_unsaved") is not False
        return Native.__getattribute__(self, name)


class _Workspace(Native):
    _members = ("currentPackage", "hasChanges", "packages", "exportFont")

    def exportFont(self, package, control):
        """flWorkspace.exportFont(package, ExportControl): writes the font into the control's folder, then shows
        its Font Exported message (DlgSavePackage), which the export waits on (live)."""
        state = object.__getattribute__(self, "_f_state")
        state.exports.append((package, dict(control._f_values)))
        folder = control._f_values.get("destinationFolder")
        if folder and state.export_writes:
            with open(folder + package.psn + ".ttf", "wb") as handle:
                handle.write(b"exported")
        state.ask(u"Font Exported", u"DlgSavePackage", [u"OK"], u"The font was exported.")

    def __init__(self, state):
        object.__setattr__(self, "_f_state", state)

    def __getattribute__(self, name):
        state = object.__getattribute__(self, "_f_state")
        if name == "currentPackage":
            return state.current
        if name == "hasChanges":
            return state.workspace_changed
        if name == "packages":  # a method in FontLab 7: flWorkspace.packages()
            if state.unsaved is not None:
                fonts = list(state.fonts) + [None] * len(state.unsaved)
                return lambda: [_PackageObject(unsaved, fonts[i]) for i, unsaved in enumerate(state.unsaved)]
            return lambda: [_PackageObject(False, font) for font in state.fonts]
        return Native.__getattribute__(self, name)


class FontLabState(object):
    def __init__(self):
        self.fonts = []
        self.current = None
        self.workspace_changed = False
        self.unsaved = None  # one flag per open package; None: each open font, none unsaved
        self.steps = []  # (title, recorded objects) of every notifyChangesApplied
        self.content_updates = []  # notifyPackageContentUpdated's ids
        self.fail_history = None  # "before", "step", or "always": notifyChangesApplied raises then
        self.undone = 0  # Undo triggers
        self.undo_moves = True  # False: Undo does nothing (a step FontLab refused to take back)
        self.history = []  # FontLab's History: (title, objects) steps, last on top
        self.redo = []
        # FontLab's windows (install_qt): window tabs, the Output panel, the Features panel, File > Close Font
        self.tabs = None  # (text, font) of each window tab; None: one tab per open font, named by its full name
        self.output = u""  # the Output panel's text
        self.compiler = lambda font: u""  # what a compile prints for the font in front
        self.compiled = []  # the fonts compiled, in order
        self.features_panel = True  # False: the Features panel was never made (no Compile button)
        self.timers = []  # (delay in ms, callback) of every QTimer.singleShot, for the test to fire
        self.loads = []  # paths requestLoadingFont was given
        self.asks_to_save = set()  # ids of fonts whose closing FontLab asks about
        self.prompt = None  # the font FontLab's Save question is about, while it is shown
        self.prompt_key = "ffn"  # the name the question quotes: ffn, or tfn (a font of several masters, live)
        self.answers = []  # the Save question's buttons clicked
        self.windows = []  # titles of other visible top-level windows
        self.grabs = []  # the windows grabbed
        self.hold_open = False  # True: requestLoadingFont stops on a question (Coordinates Rounding) instead
        self.question = None  # the title of a question FontLab shows (not the Save one)
        self.question_class = u"QDialog"
        self.question_buttons = [u"OK"]
        self.question_text = u"Round the coordinates to whole units?"
        self.question_then = None  # what FontLab does once the question is answered: called with the button's text
        self.output_panel = True  # False: the Output panel was never made (no textOutput)
        self.panels_chosen = []  # the Window > Panels actions triggered
        self.exports = []  # (package, the ExportControl's values) of every exportFont
        self.export_writes = True  # False: an export writes no file
        self.saved = []  # the fonts File > Save saved (the font in front)
        self.on_modal = None  # what the user does once a modal question shows (modal), then None
        # The window tools (widgetfakes): scripts' windows shown, the modal one among them FontLab waits on,
        # the main window's own widgets and dynamic properties, and its menu bar.
        self.qt_windows = []
        self.qt_modal = None
        self.main_children = []
        self.main_props = {}
        self.menu_bar = None

    def ask(self, title, kind=u"QDialog", buttons=(u"OK",), text=u"", then=None):
        """FontLab shows a question (a modal dialog other than the Save one)."""
        self.question, self.question_class, self.question_buttons = title, kind, list(buttons)
        self.question_text, self.question_then = text, then

    def fire_timers(self, up_to_ms=None):
        """Run the QTimer.singleShot callbacks due by up_to_ms (all, when None), in the order they are due. One
        that a modal loop inside an earlier one ran already (modal) is not run again; one added meanwhile waits
        for the next call."""
        due = sorted([t for t in self.timers if up_to_ms is None or t[0] <= up_to_ms], key=lambda t: t[0])
        for timer in due:
            if timer in self.timers:
                self.timers.remove(timer)
                timer[1]()

    def modal(self, showing):
        """FontLab's modal question loop (live: the call that shows it returns once it is answered, and timers
        fire inside it): on_modal (the user's doing, once), then the timers waiting, in the order they are
        due, while showing() holds. The fake cannot wait for a user: it returns when no timer is left."""
        if self.on_modal is not None and showing():
            hook, self.on_modal = self.on_modal, None
            hook()
        for _turn in range(200):
            if not showing() or not self.timers:
                return
            timer = sorted(self.timers, key=lambda t: t[0])[0]
            self.timers.remove(timer)
            timer[1]()

    def window_tabs(self):
        if self.tabs is not None:
            return self.tabs
        return [(u"Sketchboard", None)] + [(u" %s " % font.ffn, font) for font in self.fonts]

    def close_current(self):
        """File > Close Font: closes the font in front, or shows FontLab's Save question about it."""
        font = self.current
        if font is None:
            return
        if font.fgPackage.id in self.asks_to_save:
            self.prompt = font
            return
        self.drop(font)

    def drop(self, font):
        self.fonts.remove(font)
        if self.tabs is not None:
            self.tabs = [tab for tab in self.tabs if tab[1] is not font]
        self.current = self.fonts[0] if self.fonts else None

    def undo(self):
        """Edit > Undo: the top step's objects go back to their states before it, and Redo names it."""
        self.undone += 1
        if not self.undo_moves or not self.history:
            return
        title, items = self.history.pop()
        self.redo.append(title)
        for item in items:
            if len(item._f_states) > 1:
                item._f_states.pop()
            item._f_snapshots.pop()
            if item._f_snapshots:  # (nothing earlier recorded: it stays as written)
                _restore(item, item._f_snapshots[-1])

    def undo_text(self):  # (FontLab shows step titles in title case, with a mnemonic)
        return ("&Undo " + self.history[-1][0].title()) if self.history else "&Undo"

    def redo_text(self):
        return ("&Redo " + self.redo[-1].title()) if self.redo else "&Redo"


_COUNTER = [0]


def _notify(state, title, objects, recursive):
    before = title.endswith("(before)")
    if state.fail_history == "always" or state.fail_history == ("before" if before else "step"):
        raise RuntimeError("History refused")
    items = objects if isinstance(objects, list) else [objects]
    if isinstance(items[0], flPackage) and items[0] is not state.current:
        title = u""  # (FontLab titles a step only for the font in front: live, 2026-11-19)
    state.steps.append((title, list(items)))
    state.history.append((title, list(items)))
    state.redo = []
    _COUNTER[0] += 1
    for item in items:
        item._f_states.append(_COUNTER[0])
        item._f_snapshots.append(_snapshot(item))


# FontLab's History, by value: a step records each object as it is after the change; Undo puts each object
# back as the step before recorded it, and leaves one the History holds nothing earlier of as it is. The font
# recorded holds its glyph list, every layer, its kerning, and its feature code; a glyph its name and code points.
def _copy_layer(layer):
    copy = flLayer(layer.name, [_copy_shape(s) for s in layer._f_shapes], width=layer._f_width, bbox=layer.boundingBox,
                   anchors=[flPinPoint(QPointF(a.point.x(), a.point.y()), a.name) for a in layer._f_anchors],
                   ink=layer._f_ink)
    for key in ("metricsLeft", "metricsRight", "metricsWidth", "_f_round", "_f_refuse_width", "_f_raise_transform",
                "_f_anchor_drift"):
        object.__setattr__(copy, key, object.__getattribute__(layer, key))
    return copy


def _copy_shape(shape):
    contours = [flContour([flNode(n.x, n.y, n.type, n.smooth) for n in c.nodes()], c.closed) for c in shape.contours]
    copy = flShape(contours, shape.name, shape.transform, shape.isComponent, shape.shapeData.componentGlyph)
    return copy


def _snapshot(item):
    if isinstance(item, flPackage):
        return {"glyphs": list(item._f_glyphs),
                "layers": dict((id(g), [_copy_layer(l) for l in g._f_layers]) for g in item._f_glyphs),
                "kerning": dict((m, (list(k._f_pairs), dict(k._f_groups))) for m, k in item._f_kerning.items()),
                "features": (list(item.fgPackage.features._f_features), item.fgPackage.features._f_prefix)}
    return {"name": item.name, "unicodes": list(item.fgGlyph._f_unicodes)}


def _restore(item, snap):
    if isinstance(item, flPackage):
        object.__setattr__(item, "_f_glyphs", list(snap["glyphs"]))
        for glyph in item._f_glyphs:
            object.__setattr__(glyph, "_f_layers", [_copy_layer(l) for l in snap["layers"].get(id(glyph), [])])
        for master, (pairs, groups) in snap["kerning"].items():
            kerning = item.kerning(master)
            object.__setattr__(kerning, "_f_pairs", list(pairs))
            kerning._f_groups.clear()
            kerning._f_groups.update(groups)
        features = item.fgPackage.features
        object.__setattr__(features, "_f_features", list(snap["features"][0]))
        object.__setattr__(features, "_f_prefix", snap["features"][1])
    else:
        object.__setattr__(item, "name", snap["name"])
        object.__setattr__(item.fgGlyph, "_f_unicodes", list(snap["unicodes"]))


class _GlyphComponent(Native):
    _members = ("transform",)
    _settable = ("transform",)

    def __init__(self, name):
        object.__setattr__(self, "_f_name", name)
        object.__setattr__(self, "transform", QTransform())


def install(state):
    """Put fake fontlab and fontgate modules into sys.modules; returns the fontlab module."""
    fontlab = types.ModuleType("fontlab")
    workspace = _Workspace(state)

    class flWorkspace(object):
        @staticmethod
        def instance():
            return workspace

    class flItems(object):
        @staticmethod
        def notifyChangesApplied(title, objects, recursive):
            _notify(state, title, objects, recursive)

        @staticmethod
        def notifyPackageContentUpdated(font_id):
            state.content_updates.append(font_id)

        @staticmethod
        def requestLoadingFont(path):
            """File > Open: the font opens at once (FontLab gives it the file's path) and comes to the front."""
            state.loads.append(path)
            font_id = 100 + len(state.loads)

            def load(_button=None):
                font = sample_font(path=path, font_id=font_id)
                state.fonts.append(font)
                state.current = font
            if state.hold_open:  # (Coordinates Rounding: the font opens once it is answered, inside this call)
                state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"], text=u"Round the coordinates to whole units?",
                          then=lambda button: load() if button == u"OK" else None)
                state.modal(lambda: state.question is not None)
                return
            load()

    class ExportControl(Native):
        """fontlab.ExportControl: load() reads the user's last export settings; its fields are settable."""
        _members = ("load", "profileName", "destinationMode", "destinationFolder")
        _settable = ("profileName", "destinationMode", "destinationFolder")

        def __init__(self):
            object.__setattr__(self, "_f_values", {})

        def load(self):
            self._f_values.update({"profileName": u"OpenType TT", "destinationMode": 1, "destinationFolder": u""})

        def __getattribute__(self, name):
            if name in ("profileName", "destinationMode", "destinationFolder"):
                return object.__getattribute__(self, "_f_values").get(name)
            return Native.__getattribute__(self, name)

        def __setattr__(self, name, value):
            Native.__setattr__(self, name, value)
            object.__getattribute__(self, "_f_values")[name] = value

    def wrap(item):
        font = object.__getattribute__(item, "_f_font")
        if font is None:
            raise RuntimeError("flPackage: not a font")
        return font

    fontlab.flWorkspace = flWorkspace
    fontlab.flItems = flItems
    fontlab.flPackage = wrap
    fontlab.flGlyph = lambda: flGlyph("", [])
    fontlab.GlyphComponent = _GlyphComponent
    fontlab.flPinPoint = flPinPoint
    fontlab.flContour = contour_of_points
    fontlab.flShape = lambda: flShape([])
    fontlab.ExportControl = ExportControl
    fontlab.CountFonts = lambda: len(state.fonts)
    fontlab.ApplicationPath = lambda: "FontLab 7 (fake)"
    sys.modules["fontlab"] = fontlab
    sys.modules["fontgate"] = types.ModuleType("fontgate")
    return fontlab


def uninstall():
    for name in ("fontlab", "fontgate"):
        sys.modules.pop(name, None)


QT_MODULES = ("PythonQt", "PythonQt.QtCore", "PythonQt.QtGui")


def install_qt(state):
    """Fake PythonQt: QPointF, QTransform, and FontLab's main window with its Edit > Undo action. Returns
    what it replaced, for uninstall_qt."""
    import widgetfakes  # (here: it imports this module)
    saved = dict((name, sys.modules.get(name)) for name in QT_MODULES)

    class QAction(object):
        pass

    class Undo(QAction):
        objectName = "actionUndo"

        @property
        def enabled(self):
            return bool(state.history)

        @property
        def text(self):
            return state.undo_text()

        def trigger(self):
            state.undo()

    class Redo(QAction):
        objectName = "actionRedo"

        @property
        def enabled(self):
            return bool(state.redo)

        @property
        def text(self):
            return state.redo_text()

    class CloseFonts(QAction):
        objectName = "actionClose_Fonts"

        def trigger(self):  # (returns once FontLab's Save question is answered: live)
            state.close_current()
            state.modal(lambda: state.prompt is not None)

    class Panel(QAction):
        """Window > Panels > a panel: checked while it is shown; choosing it makes it."""

        def __init__(self, name, flag):
            self.objectName, self.flag = name, flag

        @property
        def checked(self):
            return getattr(state, self.flag)

        def trigger(self):
            state.panels_chosen.append(self.objectName)
            setattr(state, self.flag, not getattr(state, self.flag))

    class Save(QAction):  # (File > Save Font, live 2026-11-19)
        objectName = "actionSaveFont"
        text = u"&Save Font..."

        def trigger(self):
            state.saved.append(state.current)

    class Meta(object):
        def __init__(self, name):
            self.name = name

        def className(self):
            return self.name

    class Widget(object):
        """A Qt widget: its class name, objectName, and the kinds of Qt class it inherits."""
        kinds = ()
        class_name = "QWidget"
        objectName = ""
        windowTitle = u""
        enabled = True
        modal = False
        toolTip = u""
        accessibleName = u""
        top = False  # a top-level window

        def metaObject(self):
            return Meta(self.class_name)

        def inherits(self, name):
            return name in self.kinds

        def isVisible(self):
            return True

        def findChildren(self, kind):
            return []

        def children(self):
            return []

        def isWidgetType(self):
            return True

        def isWindow(self):
            return self.top

        def property(self, name):
            return getattr(self, "dynamic", {}).get(name)

        def setProperty(self, name, value):  # (the listener numbers windows of one name: freelabMcpWindow)
            if "dynamic" not in self.__dict__:
                self.dynamic = {}
            self.dynamic[name] = value

        def parentWidget(self):
            return None

        def layout(self):
            return None

        def grab(self):
            state.grabs.append(self.windowTitle)
            return QPixmap(2000, 1000)

    class QTabWidget(Widget):
        pass

    class Container(Widget):
        objectName = "YDropTargetContainer"

    class WindowTabs(QTabWidget):
        """The window tabs (YTabWidget in a YDropTargetContainer): choosing one brings its font to the front."""
        class_name = "YTabWidget"

        def parentWidget(self):
            return Container()

        @property
        def count(self):
            return len(state.window_tabs())

        @property
        def currentIndex(self):
            for index, (_text, font) in enumerate(state.window_tabs()):
                if font is state.current:
                    return index
            return 0

        def tabText(self, index):
            return state.window_tabs()[index][0]

        def setCurrentIndex(self, index):
            state.current = state.window_tabs()[index][1]

    class PanelTabs(QTabWidget):
        """A panel's tab widget (Features, Source): not a window tab, never chosen."""
        class_name = "YTabWidget"
        count = 2

        def parentWidget(self):
            return Widget()

        def tabText(self, index):
            return [u"Features", u"Source"][index]

        def setCurrentIndex(self, index):
            raise AssertionError("a panel's tab was chosen")

    class QAbstractButton(Widget):
        kinds = ("QAbstractButton",)
        class_name = "QPushButton"
        checkable = False
        checked = False

    class QLabel(Widget):
        kinds = ("QLabel",)
        class_name = "QLabel"
        textFormat = 2

        def __init__(self, text):
            self.text = text

        def buddy(self):
            return None

    class QTextEdit(Widget):
        objectName = "textOutput"

        def __init__(self, shown):
            self.shown = shown

        def isVisible(self):
            return self.shown

        def toPlainText(self):
            return state.output if self.shown else u""

    class CompileButton(QAbstractButton):
        """The Features panel's Compile button: FontLab clears the Output panel, then compiles in the click."""
        objectName = "buttonCompile"
        toolTip = u"Compile features (\u2303\u21a9 / Ctrl+Enter)"
        enabled = True

        def click(self):
            state.compiled.append(state.current)
            state.output = state.compiler(state.current)

    class OtherCompileButton(QAbstractButton):
        objectName = "buttonCompile"
        toolTip = u"Compile code (\u2303+\u21b5)"

        def click(self):
            raise AssertionError("the Scripting panel's button was clicked")

    class Answer(QAbstractButton):
        def __init__(self, text):
            self.text = text

        def click(self):
            state.answers.append(self.text)
            font, state.prompt = state.prompt, None
            if self.text == u"&Don\u2019t Save":
                state.drop(font)

    class SavePrompt(Widget):
        class_name = "DlgClosePackage"
        windowTitle = u"FontLab Warning"
        top = modal = True

        def findChildren(self, kind):
            if kind is QLabel:
                return [QLabel(u'<b>Save changes to  "%s" before closing?</b>' % getattr(state.prompt, state.prompt_key)),
                        QLabel(u"If you don't save, your changes will be lost.")]
            if kind is QAbstractButton:
                return [Answer(u"&Save"), Answer(u"&Don\u2019t Save"), Answer(u"Cancel")]
            return []

        def children(self):
            return self.findChildren(QLabel) + self.findChildren(QAbstractButton)

    class QuestionAnswer(QAbstractButton):
        def __init__(self, text):
            self.text = text

        def click(self):
            state.answers.append(self.text)
            then = state.question_then
            state.question, state.question_then = None, None
            if then is not None:
                then(self.text)

    class Question(Widget):
        top = modal = True

        @property
        def class_name(self):
            return state.question_class

        @property
        def windowTitle(self):
            return state.question

        def children(self):
            shown = (state.question, state.question_text, tuple(state.question_buttons))
            if self.parts is None or self.parts[0] != shown:  # (the same buttons while the same question shows)
                self.parts = (shown, [QLabel(state.question_text)] + [QuestionAnswer(t) for t in state.question_buttons])
            return list(self.parts[1])

        parts = None

    class Window(Widget):
        top = True

        def __init__(self, title):
            self.windowTitle = title

    class MainWindow(Widget):
        class_name = "MainWindow"
        windowTitle = u"FontLab 7"
        top = True

        def findChildren(self, kind):
            if kind is QAction:
                return [Undo(), Redo(), CloseFonts(), Panel("actionFeaturesPanel", "features_panel"),
                        Panel("actionOutputPanel", "output_panel"), Save()]
            if kind is QTabWidget:
                return [PanelTabs(), WindowTabs()]
            if kind is QAbstractButton:
                return [OtherCompileButton()] + ([CompileButton()] if state.features_panel else [])
            if kind is QTextEdit:
                return [QTextEdit(False), QTextEdit(True)] if state.output_panel else []
            if kind is widgetfakes.QMenuBar:
                return [state.menu_bar] if state.menu_bar is not None else []
            return []

        def children(self):
            return list(state.main_children)

        def property(self, name):
            return state.main_props.get(name)

    # One wrapper per window, as PythonQt keeps one per Qt object while it is held.
    main, prompt, question, plain = MainWindow(), SavePrompt(), Question(), {}

    class QApplication(object):
        @staticmethod
        def instance():
            return QApplication()

        def topLevelWidgets(self):
            # (a window per title listed: two listed alike are two windows, the same ones at each call)
            keys = [(title, state.windows[:n].count(title)) for n, title in enumerate(state.windows)]
            for key in keys:
                if key not in plain:
                    plain[key] = Window(key[0])
            return [main] + [plain[key] for key in keys] + list(state.qt_windows) + \
                ([prompt] if state.prompt is not None else []) + ([question] if state.question else [])

        def activeModalWidget(self):
            if state.prompt is not None:
                return prompt
            if state.question:
                return question
            return state.qt_modal

    class QTimer(object):
        @staticmethod
        def singleShot(delay_ms, callback):
            state.timers.append((delay_ms, callback))

    class QByteArray(object):
        def __init__(self):
            self.bytes = b""

        def data(self):
            return self.bytes

    class QBuffer(object):
        def __init__(self, array):
            self.array = array

        def open(self, mode):
            assert mode == QIODevice.WriteOnly
            return True

    class QIODevice(object):
        WriteOnly = 2

    class QPixmap(object):
        def __init__(self, width, height):
            self.size = (width, height)

        def width(self):
            return self.size[0]

        def height(self):
            return self.size[1]

        def scaledToWidth(self, width, mode):
            return QPixmap(width, self.size[1] * width // self.size[0])

        def save(self, buffer, kind):
            buffer.array.bytes = b"\x89PNG\r\n\x1a\n" + ("%dx%d" % self.size).encode("ascii")
            return kind == "PNG"

    core = types.ModuleType("PythonQt.QtCore")
    core.QPointF = QPointF
    core.QTimer, core.QByteArray, core.QBuffer, core.QIODevice = QTimer, QByteArray, QBuffer, QIODevice
    core.QUrl, core.QModelIndex = widgetfakes.QUrl, widgetfakes.QModelIndex
    core.QPersistentModelIndex = widgetfakes.QPersistentModelIndex
    gui = types.ModuleType("PythonQt.QtGui")
    gui.QTransform, gui.QApplication, gui.QAction = QTransform, QApplication, QAction
    gui.QTabWidget, gui.QAbstractButton, gui.QLabel, gui.QTextEdit = QTabWidget, QAbstractButton, QLabel, QTextEdit
    gui.QProgressBar, gui.QMenuBar = widgetfakes.QProgressBar, widgetfakes.QMenuBar
    root = types.ModuleType("PythonQt")
    root.QtCore, root.QtGui = core, gui
    sys.modules.update({"PythonQt": root, "PythonQt.QtCore": core, "PythonQt.QtGui": gui})
    return saved


def uninstall_qt(saved):
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def square(x0, y0, size):
    return flContour([flNode(x0, y0), flNode(x0 + size, y0), flNode(x0 + size, y0 + size), flNode(x0, y0 + size)])


def sample_font(broken_path=False, path="fixtures/Test.vfc", font_id=1):
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
    return cls("Free Sans", "Regular", glyphs, path=path, font_id=font_id)


def write_font(font_id=7, family="Write Sans", path="fixtures/Write.vfc"):
    """A two-master font for the write tools: kerning with classes (one named like its glyph, as FontLab
    names them), feature code, an anchor, a composite, and a glyph drawn in one master only."""
    def layer(master, shapes, width, anchors=(), ink=None):
        return flLayer(master, shapes, width=width, anchors=anchors, ink=ink)

    glyphs = [
        flGlyph("A", [layer("Regular", [flShape([square(50, 0, 400)])], 500.0, [flPinPoint(QPointF(250, 400), "top")]),
                      layer("Bold", [flShape([square(40, 0, 420)])], 520.0)], [0x41]),
        flGlyph("B", [layer("Regular", [flShape([square(60, 0, 380)])], 500.0),
                      layer("Bold", [flShape([square(50, 0, 400)])], 510.0)], [0x42]),
        flGlyph("V", [layer("Regular", [flShape([square(20, 0, 460)])], 500.0),
                      layer("Bold", [flShape([square(10, 0, 480)])], 500.0)], [0x56]),
        flGlyph("O", [layer("Regular", [flShape([square(40, 0, 420)])], 500.0, ink=QRectF(40.4, 0, 419.2, 420)),
                      layer("Bold", [flShape([square(30, 0, 440)])], 500.0)], [0x4F]),
        flGlyph("acute", [layer("Regular", [flShape([square(0, 500, 100)])], 0.0)], [0xB4]),
        flGlyph("Aacute", [layer("Regular", [flShape([square(50, 0, 400)], component=True, component_glyph="A"),
                                             flShape([square(0, 500, 100)], transform=QTransform(1, 0, 0, 1, 200, 0),
                                                     component=True, component_glyph="acute")], 500.0)], [0xC1]),
        flGlyph("space", [layer("Regular", [], 250.0), layer("Bold", [], 260.0)], [0x20]),
        flGlyph("Q", [layer("Regular", [flShape([square(40, 0, 420)])], 500.0)], [0x51]),
    ]
    regular = fgKerning({"V": (["V"], "KernLeft"), "round": (["O", "Q"], "KernRight")},
                        [(("A", "glyphMode"), ("B", "glyphMode"), -20), (("V", "groupMode"), ("A", "glyphMode"), -50),
                         (("A", "glyphMode"), ("round", "groupMode"), -10)])
    bold = fgKerning({"V": (["V"], "KernLeft"), "round": (["O", "Q"], "KernRight")},
                     [(("A", "glyphMode"), ("B", "glyphMode"), -25)])
    features = fgFeatures([("liga", u"feature liga {\n  sub A B by V;\n} liga;\n"),
                           ("ss01", u"feature ss01 {\n  sub Q by O;\n} ss01;\n")],
                          u"languagesystem DFLT dflt;\n@caps = [A B];\n")
    return flPackage(family, "Regular", glyphs, masters=["Regular", "Bold"], font_id=font_id, path=path,
                     kerning={"Regular": regular, "Bold": bold}, classes=[("caps", ["A", "B"])], features=features)
