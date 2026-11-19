# -*- coding: utf-8 -*-
"""Spy fakes of the Qt widgets the window tools touch (Python 2.7 and 3).

As in fakes.py, each fake allows only the members the listener is documented to use (docs/FONTLAB7-API.md):
any other member raises SpyViolation. A member is a method when the fake has m_<name>, else a Qt property,
read as a value (PythonQt gives Qt properties as values). Signals the listener emits by calling them, as
PythonQt lets it, are recorded in emitted, with the slots a test connected (on) called in turn.
"""

from __future__ import absolute_import, division, print_function

from fakes import SpyViolation

PARENTS = {
    "QObject": None, "QWidget": "QObject", "QDialog": "QWidget", "QMainWindow": "QWidget", "QFrame": "QWidget",
    "QAbstractButton": "QWidget", "QPushButton": "QAbstractButton", "QToolButton": "QAbstractButton",
    "QCheckBox": "QAbstractButton", "QRadioButton": "QAbstractButton", "QComboBox": "QWidget",
    "QAbstractSpinBox": "QWidget", "QSpinBox": "QAbstractSpinBox", "QDoubleSpinBox": "QAbstractSpinBox",
    "QDateTimeEdit": "QAbstractSpinBox", "QLineEdit": "QWidget", "QAbstractScrollArea": "QFrame",
    "QTextEdit": "QAbstractScrollArea", "QTextBrowser": "QTextEdit", "QPlainTextEdit": "QAbstractScrollArea",
    "QLabel": "QFrame", "QTabWidget": "QWidget", "QTabBar": "QWidget", "QAbstractItemView": "QAbstractScrollArea",
    "QListView": "QAbstractItemView", "QListWidget": "QListView", "QTreeView": "QAbstractItemView",
    "QTreeWidget": "QTreeView", "QTableView": "QAbstractItemView", "QHeaderView": "QAbstractItemView",
    "QProgressBar": "QWidget", "QAbstractSlider": "QWidget", "QSlider": "QAbstractSlider",
    "QScrollBar": "QAbstractSlider", "QGroupBox": "QWidget", "QDockWidget": "QWidget", "QStackedWidget": "QFrame",
    "QScrollArea": "QAbstractScrollArea", "QMenuBar": "QWidget", "QMenu": "QWidget", "QMessageBox": "QDialog",
}

MEMBERS = {
    "QObject": ("children", "isWidgetType", "inherits", "metaObject", "objectName", "property", "setProperty"),
    "QWidget": ("isVisible", "isWindow", "parentWidget", "windowTitle", "enabled", "modal", "layout", "accessibleName",
                "toolTip", "findChildren"),
    "QAbstractButton": ("text", "checkable", "checked", "click"),
    "QPushButton": ("menu",),
    "QToolButton": ("menu", "popupMode", "defaultAction"),
    "QCheckBox": ("tristate", "checkState"),
    "QComboBox": ("count", "itemText", "currentIndex", "currentText", "setCurrentIndex", "activated"),
    "QAbstractSpinBox": ("readOnly", "editingFinished"),
    "QSpinBox": ("value", "minimum", "maximum", "setValue"),
    "QDoubleSpinBox": ("value", "minimum", "maximum", "setValue"),
    "QLineEdit": ("text", "readOnly", "setText", "textEdited", "editingFinished"),
    "QTextEdit": ("toPlainText", "toHtml", "readOnly", "setPlainText"),
    "QTextBrowser": ("anchorClicked",),
    "QPlainTextEdit": ("toPlainText", "readOnly", "setPlainText"),
    "QLabel": ("text", "textFormat", "buddy", "linkActivated", "openExternalLinks"),
    "QTabWidget": ("count", "tabText", "currentIndex", "setCurrentIndex", "widget", "isTabEnabled"),
    "QTabBar": ("count", "tabText", "currentIndex", "setCurrentIndex", "isTabEnabled"),
    "QAbstractItemView": ("model", "selectionModel", "setCurrentIndex", "clicked", "doubleClicked", "activated"),
    "QTreeView": ("isExpanded",),
    "QProgressBar": ("value", "minimum", "maximum"),
    "QAbstractSlider": ("value", "minimum", "maximum", "setValue"),
    "QGroupBox": ("title",),
    "QMenuBar": ("actions",),
    "QMenu": ("actions",),
}

DEFAULTS = {"windowTitle": u"", "enabled": True, "modal": False, "accessibleName": u"", "toolTip": u"", "text": u"",
            "checkable": False, "checked": False, "readOnly": False, "textFormat": 2, "openExternalLinks": False,
            "popupMode": 1, "tristate": False, "minimum": 0, "maximum": 99, "value": 0, "title": u"", "currentIndex": 0}


def lineage(cls):
    found = []
    while cls is not None:
        found.append(cls)
        cls = PARENTS[cls]
    return found


class _Meta(object):
    def __init__(self, name):
        self.name = name

    def className(self):
        return self.name


class Strict(object):
    """The spy: only members its classes list; m_<name> is a method, anything else a property value."""
    _f_members = ()

    def __getattribute__(self, name):
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        if name not in object.__getattribute__(self, "_f_members"):
            raise SpyViolation("%s.%s is not a member the listener may use" % (
                object.__getattribute__(self, "_f_class"), name))
        try:
            return object.__getattribute__(self, "m_" + name)
        except AttributeError:
            return object.__getattribute__(self, "_f_values").get(name)

    def __setattr__(self, name, value):
        if not name.startswith("_f_"):
            raise SpyViolation("%s.%s is not a member the listener may set" % (type(self).__name__, name))
        object.__setattr__(self, name, value)

    def __getitem__(self, key):
        raise SpyViolation("subscript on a Qt fake")

    def __iter__(self):
        raise SpyViolation("iteration over a Qt fake")


class Widget(Strict):
    """A Qt widget of class cls (base: the Qt class it derives from, for FontLab's own classes), with children,
    a layout, dynamic properties (props), and property values."""

    def __init__(self, cls, children=(), name=u"", base=None, props=None, on=None, **values):
        object.__setattr__(self, "_f_class", cls)
        classes = lineage(base or cls)
        if base:
            classes = [cls] + classes
        object.__setattr__(self, "_f_classes", classes)
        object.__setattr__(self, "_f_members", frozenset(m for c in classes for m in MEMBERS.get(c, ())))
        object.__setattr__(self, "_f_values", dict(DEFAULTS, objectName=name, **values))
        object.__setattr__(self, "_f_children", [])
        object.__setattr__(self, "_f_parent", None)
        object.__setattr__(self, "_f_props", dict(props or {}))
        object.__setattr__(self, "_f_on", dict(on or {}))
        object.__setattr__(self, "_f_emitted", [])
        object.__setattr__(self, "_f_shown", True)
        object.__setattr__(self, "_f_window", False)
        object.__setattr__(self, "_f_layout", None)
        object.__setattr__(self, "_f_menu", None)
        object.__setattr__(self, "_f_default_action", None)
        object.__setattr__(self, "_f_buddy", None)
        object.__setattr__(self, "_f_dead", False)
        for child in children:
            self._f_add(child)

    # -- test helpers ----------------------------------------------------------
    def _f_add(self, child):
        child._f_parent = self
        self._f_children.append(child)
        return child

    def _f_set(self, **values):
        self._f_values.update(values)
        return self

    def _f_signal(self, name, *args):
        self._f_emitted.append((name,) + args)
        handler = self._f_on.get(name)
        if handler is not None:
            handler(*args)

    def _f_visible(self):
        if self._f_dead or not self._f_shown:
            return False
        return self._f_parent is None or self._f_window or self._f_parent._f_visible()

    def _f_walk(self):
        for child in self._f_children:
            yield child
            for inner in child._f_walk():
                yield inner

    def _f_find(self, name):
        for widget in self._f_walk():
            if widget._f_values.get("objectName") == name:
                return widget
        raise KeyError(name)

    # -- QObject, QWidget --------------------------------------------------------
    def m_children(self):
        return list(self._f_children)

    def m_isWidgetType(self):
        return True

    def m_inherits(self, name):
        return name in self._f_classes

    def m_metaObject(self):
        return _Meta(self._f_class)

    def m_property(self, name):
        return self._f_props.get(name)

    def m_setProperty(self, name, value):
        self._f_props[name] = value

    def m_isVisible(self):
        return self._f_visible()

    def m_isWindow(self):
        return self._f_window

    def m_parentWidget(self):
        return self._f_parent

    def m_layout(self):
        return self._f_layout

    def m_findChildren(self, kind):
        return [w for w in self._f_walk() if getattr(kind, "__name__", None) in w._f_classes]

    # -- buttons ---------------------------------------------------------------------
    def m_click(self):
        values = self._f_values
        if "QRadioButton" in self._f_classes:
            for sibling in self._f_parent._f_children if self._f_parent is not None else ():
                if sibling is not self and "QRadioButton" in sibling._f_classes:
                    sibling._f_values["checked"] = False
            values["checked"] = True
        elif values.get("tristate"):
            values["_state"] = (values.get("_state", 0) + 1) % 3
            values["checked"] = values["_state"] != 0
        elif values.get("checkable") or "QCheckBox" in self._f_classes:
            values["checked"] = not values.get("checked")
        self._f_signal("clicked")

    def m_menu(self):
        return self._f_menu

    def m_defaultAction(self):
        return self._f_default_action

    def m_checkState(self):
        return self._f_values.get("_state", 2 if self._f_values.get("checked") else 0)

    # -- dropdowns, spin boxes, fields, text ---------------------------------------
    def m_count(self):
        return len(self._f_values.get("items", self._f_values.get("tabs", ())))

    def m_itemText(self, index):
        return self._f_values["items"][index]

    def m_currentText(self):
        items, index = self._f_values.get("items", ()), self._f_values.get("currentIndex", 0)
        return items[index] if 0 <= index < len(items) else u""

    def m_setCurrentIndex(self, index):
        if "QAbstractItemView" in self._f_classes:  # (a view's current row: selected, as a press selects it)
            self._f_values["selected"] = [index._f_row]
            self._f_signal("currentChanged", index._f_row.texts[0])
            return
        if index != self._f_values.get("currentIndex"):
            self._f_values["currentIndex"] = index
            pages = self._f_values.get("pages")
            if pages:
                for number, page in enumerate(pages):
                    page._f_shown = number == index
            self._f_signal("currentChanged" if "items" not in self._f_values else "currentIndexChanged", index)

    def m_activated(self, index):
        self._f_signal("activated", index if not hasattr(index, "_f_row") else index._f_row.texts[0])

    def m_setValue(self, value):
        if value != self._f_values.get("value"):
            self._f_values["value"] = value
            self._f_signal("valueChanged", value)

    def m_editingFinished(self):
        self._f_signal("editingFinished")

    def m_setText(self, text):
        if text != self._f_values.get("text"):
            self._f_values["text"] = text
            self._f_signal("textChanged", text)

    def m_textEdited(self, text):
        self._f_signal("textEdited", text)

    def m_toPlainText(self):
        return self._f_values.get("plain", u"")

    def m_toHtml(self):
        return self._f_values.get("html", u"")

    def m_setPlainText(self, text):
        self._f_values["plain"] = text
        self._f_signal("textChanged")

    def m_anchorClicked(self, url):
        self._f_signal("anchorClicked", url.toString())

    def m_buddy(self):
        return self._f_buddy

    def m_linkActivated(self, href):
        self._f_signal("linkActivated", href)

    # -- tabs ------------------------------------------------------------------------
    def m_tabText(self, index):
        return self._f_values["tabs"][index]

    def m_widget(self, index):
        return self._f_values["pages"][index]

    def m_isTabEnabled(self, index):
        return index not in self._f_values.get("disabled_tabs", ())

    # -- item views ------------------------------------------------------------------
    def m_model(self):
        return self._f_values["model"]

    def m_selectionModel(self):
        return _Selection(self)

    def m_clicked(self, index):
        self._f_signal("clicked", index._f_row.texts[0])

    def m_doubleClicked(self, index):
        self._f_signal("doubleClicked", index._f_row.texts[0])

    def m_isExpanded(self, index):
        return index._f_row.expanded

    # -- menus -----------------------------------------------------------------------
    def m_actions(self):
        return list(self._f_values.get("actions", ()))


def window(cls, title, children=(), **kw):
    """A top-level window."""
    widget = Widget(cls, children, windowTitle=title, **kw)
    widget._f_window = True
    return widget


class Layout(Strict):
    """A QLayout: its items, each a widget or a nested layout."""

    def __init__(self, *items):
        object.__setattr__(self, "_f_class", "QLayout")
        object.__setattr__(self, "_f_members", frozenset(["count", "itemAt"]))
        object.__setattr__(self, "_f_values", {})
        object.__setattr__(self, "_f_items", list(items))
        object.__setattr__(self, "_f_reads", [0])  # (itemAt calls: a listing reads each layout once)

    def m_count(self):
        return len(self._f_items)

    def m_itemAt(self, index):
        self._f_reads[0] += 1
        return _LayoutItem(self._f_items[index])


class _LayoutItem(Strict):
    def __init__(self, thing):
        object.__setattr__(self, "_f_class", "QLayoutItem")
        object.__setattr__(self, "_f_members", frozenset(["widget", "layout"]))
        object.__setattr__(self, "_f_values", {})
        object.__setattr__(self, "_f_thing", thing)

    def m_widget(self):
        return self._f_thing if isinstance(self._f_thing, Widget) else None

    def m_layout(self):
        return self._f_thing if isinstance(self._f_thing, Layout) else None


class Action(Strict):
    """A QAction: a menu entry, or a submenu's (menu)."""

    def __init__(self, text, name=u"", menu=None, separator=False, on=None, props=None, **values):
        object.__setattr__(self, "_f_class", "QAction")
        object.__setattr__(self, "_f_members", frozenset(["text", "enabled", "checkable", "checked", "visible",
                                                          "isSeparator", "menu", "trigger", "objectName", "property",
                                                          "toolTip"]))
        object.__setattr__(self, "_f_values", dict({"text": text, "objectName": name, "enabled": True,
                                                    "checkable": False, "checked": False, "visible": True,
                                                    "toolTip": u""}, **values))
        object.__setattr__(self, "_f_menu", menu)
        object.__setattr__(self, "_f_separator", separator)
        object.__setattr__(self, "_f_on", on)
        object.__setattr__(self, "_f_props", dict(props or {}))
        object.__setattr__(self, "_f_triggered", 0)

    def m_isSeparator(self):
        return self._f_separator

    def m_menu(self):
        return self._f_menu

    def m_property(self, name):
        return self._f_props.get(name)

    def m_trigger(self):
        self._f_triggered += 1
        if self._f_values.get("checkable"):
            self._f_values["checked"] = not self._f_values["checked"]
        if self._f_on is not None:
            self._f_on()


def menu(*actions):
    return Widget("QMenu", actions=list(actions))


class Row(object):
    """A row of an item model: its texts per column, check state (None: no box), children, flags."""

    def __init__(self, *texts, **kw):
        self.texts = list(texts)
        self.check = kw.get("check")
        self.children = list(kw.get("children", ()))
        self.expanded = kw.get("expanded", False)


class QModelIndex(Strict):
    """QtCore.QModelIndex: QModelIndex() is the root; the model makes the others."""

    def __init__(self, row=None, number=-1, column=0, parent=None):
        object.__setattr__(self, "_f_class", "QModelIndex")
        object.__setattr__(self, "_f_members", frozenset(["isValid", "row", "parent"]))
        object.__setattr__(self, "_f_values", {})
        object.__setattr__(self, "_f_row", row)
        object.__setattr__(self, "_f_number", number)
        object.__setattr__(self, "_f_column", column)
        object.__setattr__(self, "_f_parent_index", parent)

    def m_isValid(self):
        return self._f_row is not None

    def m_row(self):
        return self._f_number

    def m_parent(self):
        return self._f_parent_index if self._f_parent_index is not None else QModelIndex()


class QPersistentModelIndex(Strict):
    """QtCore.QPersistentModelIndex: it follows its row as rows move (here: the very Row object), and is invalid
    once its row is gone or its model holds other rows."""

    def __init__(self, index):
        object.__setattr__(self, "_f_class", "QPersistentModelIndex")
        object.__setattr__(self, "_f_members", frozenset(["isValid", "row"]))
        object.__setattr__(self, "_f_values", {})
        object.__setattr__(self, "_f_row", index._f_row)
        object.__setattr__(self, "_f_siblings", index._f_values.get("siblings"))

    def _f_place(self):
        siblings = self._f_siblings
        for number, row in enumerate(siblings if siblings is not None else ()):
            if row is self._f_row:
                return number
        return -1

    def m_isValid(self):
        return self._f_row is not None and self._f_place() >= 0

    def m_row(self):
        return self._f_place()


class Model(Strict):
    """A QAbstractItemModel over Rows, with column headers."""

    def __init__(self, rows, headers=(u"",)):
        object.__setattr__(self, "_f_class", "QAbstractItemModel")
        object.__setattr__(self, "_f_members", frozenset(["rowCount", "columnCount", "index", "data", "headerData",
                                                          "setData", "flags"]))
        object.__setattr__(self, "_f_values", {})
        object.__setattr__(self, "_f_rows", list(rows))
        object.__setattr__(self, "_f_headers", list(headers))
        object.__setattr__(self, "_f_changed", [])
        object.__setattr__(self, "_f_calls", [0])

    def _f_under(self, parent):
        return self._f_rows if parent._f_row is None else parent._f_row.children

    def m_rowCount(self, parent):
        self._f_calls[0] += 1
        return len(self._f_under(parent))

    def m_columnCount(self, parent):
        return len(self._f_headers)

    def m_index(self, row, column, parent):
        siblings = self._f_under(parent)
        index = QModelIndex(siblings[row], row, column, parent if parent._f_row is not None else None)
        index._f_values["siblings"] = siblings
        return index

    # (a test may give a model its own flags, or a setData that refuses: _f_flags, _f_refuse)
    def m_flags(self, index):
        flags = getattr(self, "_f_flags", None)
        if flags is not None:
            return flags
        return 1 | 32 | (16 if index._f_row.check is not None else 0)

    def m_data(self, index, role):
        if role == 10:
            return None if index._f_column else index._f_row.check
        assert role == 0
        texts = index._f_row.texts
        return texts[index._f_column] if index._f_column < len(texts) else None

    def m_headerData(self, section, orientation, role):
        assert (orientation, role) == (1, 0)
        return self._f_headers[section]

    def m_setData(self, index, value, role):
        assert role == 10 and index._f_row.check is not None
        if getattr(self, "_f_refuse", False):
            return False
        index._f_row.check = value
        self._f_changed.append((index._f_row.texts[0], value))
        return True


class _Selection(Strict):
    def __init__(self, view):
        object.__setattr__(self, "_f_class", "QItemSelectionModel")
        object.__setattr__(self, "_f_members", frozenset(["isSelected"]))
        object.__setattr__(self, "_f_values", {})
        object.__setattr__(self, "_f_view", view)

    def m_isSelected(self, index):
        return any(row is index._f_row for row in self._f_view._f_values.get("selected", ()))


class QUrl(object):
    """QtCore.QUrl, as the listener builds one for anchorClicked."""

    def __init__(self, text):
        self.text = text

    def toString(self):
        return self.text


class QProgressBar(object):
    """QtGui.QProgressBar and QtGui.QMenuBar, as findChildren's argument: matched by class name."""


class QMenuBar(object):
    pass


def view(cls, rows, headers=(u"",), **kw):
    return Widget(cls, model=Model(rows, headers), **kw)
