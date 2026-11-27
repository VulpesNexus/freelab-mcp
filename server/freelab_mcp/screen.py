"""Pictures of FontLab's windows taken from outside FontLab, as Windows draws them (screenshot source="screen").

FontLab's own Qt picture (screenshot source="qt", QWidget.grab) shows what Qt draws; this one asks Windows
for the window through user32's PrintWindow with PW_RENDERFULLCONTENT, so it shows what Qt's picture may
not (native title bars, a window FontLab's main thread is too busy to draw for a grab). FontLab is found by
the process id its listener records in its state file. Windows only; the PNG is written with zlib and struct.
Proven live at 100 % and 150 % display scaling (docs/FONTLAB7-API.md).
"""

from __future__ import annotations

import struct
import sys
import zlib
from typing import Any, Callable

PW_RENDERFULLCONTENT = 2
GW_OWNER = 4
SRCCOPY = 0x00CC0020
HALFTONE = 4
DIB_RGB_COLORS = 0
DPI_PER_MONITOR_AWARE_V2 = -4
MAX_PIXELS = 64 * 1024 * 1024  # a window bigger than this is refused rather than captured


class CaptureError(RuntimeError):
    """A picture that could not be taken: code as the tools report it ([code] message)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def png(width: int, height: int, rgb: bytes) -> bytes:
    """A PNG (8-bit RGB, no interlace) of rows of RGB bytes, top row first."""
    stride = width * 3
    if len(rgb) != stride * height:
        raise ValueError("%d bytes are not %d rows of %d pixels" % (len(rgb), height, width))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def bgra_to_rgb(data: bytes) -> bytes:
    """A 32-bit Windows bitmap's pixels (blue, green, red, unused) as RGB bytes."""
    rgb = bytearray(len(data) // 4 * 3)
    rgb[0::3] = data[2::4]
    rgb[1::3] = data[1::4]
    rgb[2::3] = data[0::4]
    return bytes(rgb)


def choose_window(windows: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
    """The window to take: the one titled title (exactly, else the only one whose title contains it, case
    ignored), or without title the largest window no other window owns (FontLab's main window).
    windows: [{"hwnd", "title", "owned", "width", "height"}] of FontLab's visible top-level windows."""
    shown = [w for w in windows if w["width"] > 0 and w["height"] > 0]
    if not shown:
        raise CaptureError("not_found", "FontLab shows no window (is it minimized, or still starting?).")
    title = title.strip()
    if not title:
        main = [w for w in shown if not w["owned"]] or shown
        return max(main, key=lambda w: w["width"] * w["height"])
    found = [w for w in shown if w["title"] == title] or [w for w in shown if title.lower() in w["title"].lower()]
    if not found:
        titles = "; ".join(w["title"] for w in shown if w["title"]) or "none titled"
        raise CaptureError("not_found", f"No FontLab window is titled '{title}'. Its windows: {titles}.")
    if len(found) > 1:
        raise CaptureError("ambiguous_window", f"{len(found)} FontLab windows match '{title}': "
                           f"{'; '.join(w['title'] for w in found)}. Give the whole title.")
    return found[0]


def capture(pid: int, title: str = "", max_width: int = 0,
            _api: Callable[[], Any] | None = None) -> tuple[bytes, dict[str, Any]]:
    """(PNG bytes, {window, width, height, source}) of one of the process's windows, drawn by Windows."""
    if sys.platform != "win32" and _api is None:
        raise CaptureError("unavailable", "source=\"screen\" works on Windows only; use source=\"qt\".")
    api = (_api or _Win32)()
    previous = api.dpi_aware()  # (window sizes in real pixels: FontLab's Qt is DPI-aware, this server may not be)
    try:
        window = choose_window(api.windows(pid), title)
        if api.minimized(window["hwnd"]):
            raise CaptureError("unavailable", f"FontLab's window '{window['title']}' is minimized: Windows draws "
                               "nothing for it. Restore it, or use source=\"qt\".")
        width, height = window["width"], window["height"]
        if width * height > MAX_PIXELS:
            raise CaptureError("too_large", f"The window is {width}x{height} pixels, too large to capture.")
        out_width = width if not max_width or width <= max_width else max_width
        out_height = max(1, round(height * out_width / width))
        bgra = api.print_window(window["hwnd"], width, height, out_width, out_height)
    finally:
        api.restore_dpi(previous)
    return png(out_width, out_height, bgra_to_rgb(bgra)), {
        "window": window["title"], "width": out_width, "height": out_height, "source": "screen"}


def user_activity(pid: int, _api: Callable[[], Any] | None = None) -> dict[str, Any] | None:
    """{"idle_s", "fontlab_in_front"}: seconds since the last keyboard or mouse input in this session, and
    whether the window in front is one of the process's. None off Windows. The window in front is told only
    by its process: its title, which may be any program's (a browser's page), is not read."""
    if sys.platform != "win32" and _api is None:
        return None
    api = (_api or _Win32)()
    return {"idle_s": round(api.idle_ms() / 1000.0, 1), "fontlab_in_front": api.foreground_pid() == pid}


class _Win32:
    """The user32 and gdi32 calls, through ctypes (loaded only on Windows)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        HWND, HDC, HBITMAP, HGDIOBJ = wintypes.HWND, wintypes.HDC, wintypes.HBITMAP, wintypes.HGDIOBJ
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)
        for name, args, result in (
            (user32.EnumWindows, [self.enum_proc, wintypes.LPARAM], wintypes.BOOL),
            (user32.GetWindowThreadProcessId, [HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (user32.IsWindowVisible, [HWND], wintypes.BOOL),
            (user32.IsIconic, [HWND], wintypes.BOOL),
            (user32.GetWindow, [HWND, wintypes.UINT], HWND),
            (user32.GetWindowTextLengthW, [HWND], ctypes.c_int),
            (user32.GetWindowTextW, [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (user32.GetWindowRect, [HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (user32.PrintWindow, [HWND, HDC, wintypes.UINT], wintypes.BOOL),
            (user32.GetDC, [HWND], HDC),
            (user32.ReleaseDC, [HWND, HDC], ctypes.c_int),
            (gdi32.CreateCompatibleDC, [HDC], HDC),
            (gdi32.CreateCompatibleBitmap, [HDC, ctypes.c_int, ctypes.c_int], HBITMAP),
            (gdi32.CreateDIBSection, [HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                      wintypes.HANDLE, wintypes.DWORD], HBITMAP),
            (gdi32.SelectObject, [HDC, HGDIOBJ], HGDIOBJ),
            (gdi32.SetStretchBltMode, [HDC, ctypes.c_int], ctypes.c_int),
            (gdi32.SetBrushOrgEx, [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p], wintypes.BOOL),
            (gdi32.StretchBlt, [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, HDC, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL),
            (gdi32.GdiFlush, [], wintypes.BOOL),
            (gdi32.DeleteObject, [HGDIOBJ], wintypes.BOOL),
            (gdi32.DeleteDC, [HDC], wintypes.BOOL),
        ):
            name.argtypes, name.restype = args, result
        try:
            self.set_dpi = user32.SetThreadDpiAwarenessContext
            self.set_dpi.argtypes, self.set_dpi.restype = [ctypes.c_void_p], ctypes.c_void_p
        except AttributeError:  # (before Windows 10 1607)
            self.set_dpi = None

    def dpi_aware(self) -> Any:
        return self.set_dpi(self.ctypes.c_void_p(DPI_PER_MONITOR_AWARE_V2)) if self.set_dpi else None

    def restore_dpi(self, previous: Any) -> None:
        if self.set_dpi and previous:
            self.set_dpi(self.ctypes.c_void_p(previous))

    def windows(self, pid: int) -> list[dict[str, Any]]:
        ctypes, wintypes, user32 = self.ctypes, self.wintypes, self.user32
        found: list[dict[str, Any]] = []

        def visit(hwnd: Any, _param: Any) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            text = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, text, length + 1)
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            found.append({"hwnd": hwnd, "title": text.value, "owned": bool(user32.GetWindow(hwnd, GW_OWNER)),
                          "width": rect.right - rect.left, "height": rect.bottom - rect.top})
            return True

        user32.EnumWindows(self.enum_proc(visit), 0)
        return found

    def minimized(self, hwnd: Any) -> bool:
        return bool(self.user32.IsIconic(hwnd))

    def idle_ms(self) -> int:
        """Milliseconds since the session's last input (GetLastInputInfo), the tick count's wrap included."""
        ctypes, wintypes = self.ctypes, self.wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        self.user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        self.user32.GetLastInputInfo.restype = wintypes.BOOL
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError("GetLastInputInfo failed")
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetTickCount.restype = wintypes.DWORD
        return (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF

    def foreground_pid(self) -> int | None:
        """The process of the window in front, or None when there is none."""
        ctypes, wintypes, user32 = self.ctypes, self.wintypes, self.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return owner.value or None

    def print_window(self, hwnd: Any, width: int, height: int, out_width: int, out_height: int) -> bytes:
        """The window drawn by PrintWindow into a bitmap of its size, then scaled (GDI's halftone) into a
        top-down 32-bit DIB of out_width x out_height; its pixels as BGRA bytes."""
        ctypes, user32, gdi32 = self.ctypes, self.user32, self.gdi32
        screen = user32.GetDC(None)
        if not screen:
            raise CaptureError("failed", "Windows gave no device context for the screen.")
        # Each object is registered as soon as it exists; at the end every bitmap is selected out of its DC
        # before anything is deleted (a bitmap still selected cannot be deleted, and leaked when PrintWindow
        # failed: Codex, 2026-11-19), then the bitmaps, then the DCs.
        bitmaps: list[Any] = []
        dcs: list[Any] = []
        selected: list[tuple[Any, Any]] = []  # (dc, the object it held before)
        try:
            full_dc = gdi32.CreateCompatibleDC(screen)
            if full_dc:
                dcs.append(full_dc)
            full = gdi32.CreateCompatibleBitmap(screen, width, height)
            if full:
                bitmaps.append(full)
            if not full_dc or not full:
                raise CaptureError("failed", "Windows could not make a bitmap of the window's size.")
            selected.append((full_dc, gdi32.SelectObject(full_dc, full)))
            if not user32.PrintWindow(hwnd, full_dc, PW_RENDERFULLCONTENT):
                raise CaptureError("failed", "Windows did not draw the window (PrintWindow failed, error %d)." %
                                   ctypes.get_last_error())
            header = struct.pack("<IiiHHIIiiII", 40, out_width, -out_height, 1, 32, 0, 0, 0, 0, 0, 0)
            info = ctypes.create_string_buffer(header, 40 + 16)  # (BITMAPINFO: the header, then room for colors)
            bits = ctypes.c_void_p()
            out_dc = gdi32.CreateCompatibleDC(screen)
            if out_dc:
                dcs.append(out_dc)
            out = gdi32.CreateDIBSection(out_dc, info, DIB_RGB_COLORS, ctypes.byref(bits), None, 0) if out_dc else None
            if out:
                bitmaps.append(out)
            if not out_dc or not out or not bits.value:
                raise CaptureError("failed", "Windows could not make the picture's bitmap.")
            selected.append((out_dc, gdi32.SelectObject(out_dc, out)))
            gdi32.SetStretchBltMode(out_dc, HALFTONE)
            gdi32.SetBrushOrgEx(out_dc, 0, 0, None)
            ok = gdi32.StretchBlt(out_dc, 0, 0, out_width, out_height, full_dc, 0, 0, width, height, SRCCOPY)
            gdi32.GdiFlush()
            if not ok:
                raise CaptureError("failed", "Windows could not copy the window's picture (StretchBlt failed).")
            return ctypes.string_at(bits.value, out_width * out_height * 4)
        finally:
            for dc, previous in reversed(selected):
                if previous:
                    gdi32.SelectObject(dc, previous)
            for bitmap in reversed(bitmaps):
                gdi32.DeleteObject(bitmap)
            for dc in reversed(dcs):
                gdi32.DeleteDC(dc)
            user32.ReleaseDC(None, screen)
