# SPDX-FileCopyrightText: 2026 LiDARgeek
# SPDX-License-Identifier: GPL-2.0-or-later

####################################
#                                  #
# Terrain Viewer - QGIS Plugin Core v1.3.0 #
# Terrain Viewer    #
####################################

import math
import time
import traceback
import ctypes
import ctypes.util
import sys
from pathlib import Path
from collections import OrderedDict

import numpy as np

# QGIS ships GDAL with its Python environment.  This plugin intentionally uses
# that built-in raster backend so users do not need to install Rasterio/Affine.
GDAL_IMPORT_ERROR = None
try:
    from osgeo import gdal
except Exception as exc:
    gdal = None
    GDAL_IMPORT_ERROR = str(exc)

from qgis.PyQt.QtCore import Qt, QEvent, QTimer, pyqtSignal, QSize
from qgis.PyQt.QtGui import (
    QColor,
    QMatrix4x4,
    QVector3D,
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QSurfaceFormat,
    QImage,
)
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QWidget,
    QSplitter,
    QLabel,
    QProgressBar,
    QFormLayout,
    QGroupBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QOpenGLWidget,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QAbstractItemView,
    QScrollArea,
    QGridLayout,
    QTabWidget,
    QApplication,
)
from qgis.core import (
    QgsRectangle,
    QgsWkbTypes,
    QgsPointXY,
    QgsGeometry,
    QgsMessageLog,
    Qgis,
    QgsTask,
    QgsApplication,
    QgsProject,
    QgsCoordinateTransform,
    QgsProviderRegistry,
    QgsUnitTypes,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsFeatureRequest,
    QgsMapSettings,
    QgsMapRendererParallelJob,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsMapToolEmitPoint
from qgis.utils import iface


# --- Performance tuning ------------------------------------------------------
HARD_READ_CAP_CELLS = 300_000_000
PREVIEW_TARGET_POINTS = 2_000_000
MAX_RENDER_POINTS = 2_000_000
SOFT_WARN_CELLS = 5_000_000
TILE_SIZE = 512
MAX_CS_TOTAL_SAMPLES = 500_000
MAX_CS_DISTANCE_SAMPLES = 100_000
MAX_CS_WIDTH_SAMPLES = 513

# Total point budget across all raster surfaces. The raster is reduced before
# XYZ generation using a regular grid, which preserves terrain/building shape
# much better than randomly throwing rendered points away.
DETAIL_POINT_BUDGETS = {
    "Auto": 2_000_000,
    "High": 3_500_000,
    "Ultra": 6_000_000,
    "Maximum": 10_000_000,
}
VISUAL_DENSITY_FACTORS = {
    "Native": 1,
    "Dense 2×": 2,
    "Dense 3×": 3,
}
# Conservative estimate of total temporary + render working memory per point.
# Dense display scatter is still constrained by the same final budget.
DETAIL_EST_BYTES_PER_POINT = 96

# Display-quality tuning. Imagery is rendered once per current 3D footprint,
# then sampled in memory; these caps keep WMS/XYZ requests and RAM bounded.
IMAGERY_MAX_DIM_AUTO = 2048
IMAGERY_MAX_DIM_HIGH = 3072
IMAGERY_MAX_PIXELS = 8_000_000
IMAGERY_SAMPLE_CHUNK = 250_000
RELIEF_MAX_DIM = 1536
RELIEF_SAMPLE_CHUNK = 350_000
RELIEF_LIGHT_AZIMUTH_DEG = 315.0
RELIEF_LIGHT_ELEVATION_DEG = 45.0


# --- Minimal Rasterio-compatible GDAL adapter --------------------------------
# The viewer core originally used a small subset of Rasterio.  Keeping that
# interface here lets the proven extraction/task code stay simple while the
# distributable plugin depends only on GDAL, which QGIS already includes.

class _Window:
    __slots__ = ("col_off", "row_off", "width", "height")

    def __init__(self, col_off, row_off, width, height):
        self.col_off = float(col_off)
        self.row_off = float(row_off)
        self.width = float(width)
        self.height = float(height)

    def round_offsets(self):
        # Expand to whole pixels instead of accidentally clipping the selected
        # edge when a rotated geotransform produces fractional pixel indices.
        x0 = math.floor(self.col_off)
        y0 = math.floor(self.row_off)
        x1 = math.ceil(self.col_off + self.width)
        y1 = math.ceil(self.row_off + self.height)
        return _Window(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def round_lengths(self):
        return _Window(
            int(round(self.col_off)),
            int(round(self.row_off)),
            max(0, int(round(self.width))),
            max(0, int(round(self.height))),
        )


class _Affine:
    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, a, b, c, d, e, f):
        self.a = float(a)
        self.b = float(b)
        self.c = float(c)
        self.d = float(d)
        self.e = float(e)
        self.f = float(f)

    def __mul__(self, other):
        if not isinstance(other, _Affine):
            return NotImplemented
        # 3x3 affine matrix composition (self @ other).
        return _Affine(
            self.a * other.a + self.b * other.d,
            self.a * other.b + self.b * other.e,
            self.a * other.c + self.b * other.f + self.c,
            self.d * other.a + self.e * other.d,
            self.d * other.b + self.e * other.e,
            self.d * other.c + self.e * other.f + self.f,
        )

    @staticmethod
    def scale(sx, sy=None):
        if sy is None:
            sy = sx
        return _Affine(float(sx), 0.0, 0.0, 0.0, float(sy), 0.0)


class _Bounds:
    __slots__ = ("left", "bottom", "right", "top")

    def __init__(self, left, bottom, right, top):
        self.left = float(left)
        self.bottom = float(bottom)
        self.right = float(right)
        self.top = float(top)


class _WindowsNamespace:
    Window = _Window


class _Resampling:
    nearest = 0


Affine = _Affine
Resampling = _Resampling


def _apply_gt(gt, col, row):
    return (
        gt[0] + col * gt[1] + row * gt[2],
        gt[3] + col * gt[4] + row * gt[5],
    )


def _inverse_gt(gt):
    # Invert GDAL's six-parameter affine geotransform directly.  Avoiding
    # gdal.InvGeoTransform also avoids binding-version return-shape differences.
    gt = tuple(float(v) for v in gt)
    det = gt[1] * gt[5] - gt[2] * gt[4]
    if abs(det) <= 1e-30:
        raise RuntimeError("Raster geotransform is not invertible.")
    inv1 = gt[5] / det
    inv2 = -gt[2] / det
    inv4 = -gt[4] / det
    inv5 = gt[1] / det
    inv0 = -(gt[0] * inv1 + gt[3] * inv2)
    inv3 = -(gt[0] * inv4 + gt[3] * inv5)
    return (inv0, inv1, inv2, inv3, inv4, inv5)


def _numpy_dtype_for_gdal_type(gdal_type):
    """Return a NumPy dtype without importing ``osgeo.gdal_array``.

    Some Windows QGIS installations can have a NumPy ABI mismatch specifically
    in GDAL's optional ``_gdal_array`` extension.  The core GDAL bindings still
    work, so the viewer deliberately reads packed raster bytes with ReadRaster
    and converts them with NumPy directly.
    """
    mapping = {}
    for name, dtype in (
        ("GDT_Byte", np.uint8),
        ("GDT_Int8", np.int8),
        ("GDT_UInt16", np.uint16),
        ("GDT_Int16", np.int16),
        ("GDT_UInt32", np.uint32),
        ("GDT_Int32", np.int32),
        ("GDT_UInt64", np.uint64),
        ("GDT_Int64", np.int64),
        ("GDT_Float16", np.float16),
        ("GDT_Float32", np.float32),
        ("GDT_Float64", np.float64),
    ):
        value = getattr(gdal, name, None)
        if value is not None:
            mapping[int(value)] = np.dtype(dtype)

    dtype = mapping.get(int(gdal_type))
    if dtype is None:
        try:
            name = gdal.GetDataTypeName(int(gdal_type))
        except Exception:
            name = str(gdal_type)
        raise RuntimeError(
            f"Unsupported elevation raster data type: {name}. "
            "Use a real-valued integer or floating-point raster band."
        )
    return dtype


def _read_band_array(band, xoff, yoff, xsize, ysize, out_shape=None, resample_alg=None):
    """Read a GDAL band without calling Band.ReadAsArray/gdal_array.

    ``ReadAsArray`` imports GDAL's compiled ``_gdal_array`` bridge.  On a few
    QGIS/Windows combinations that bridge is compiled against a different
    NumPy ABI and raises ``numpy.core.multiarray failed to import``.  ReadRaster
    returns ordinary packed bytes and avoids that fragile bridge entirely.
    """
    xoff = int(xoff)
    yoff = int(yoff)
    xsize = int(xsize)
    ysize = int(ysize)
    if out_shape is None:
        buf_ysize, buf_xsize = ysize, xsize
    else:
        buf_ysize = int(out_shape[0])
        buf_xsize = int(out_shape[1])

    if xsize <= 0 or ysize <= 0 or buf_xsize <= 0 or buf_ysize <= 0:
        return np.empty((max(0, buf_ysize), max(0, buf_xsize)), dtype=np.float32)

    gdal_type = int(band.DataType)
    dtype = _numpy_dtype_for_gdal_type(gdal_type)
    kwargs = {
        "buf_xsize": buf_xsize,
        "buf_ysize": buf_ysize,
        "buf_type": gdal_type,
    }
    if resample_alg is not None:
        kwargs["resample_alg"] = int(resample_alg)

    raw = band.ReadRaster(xoff, yoff, xsize, ysize, **kwargs)
    if raw is None:
        raise RuntimeError("GDAL failed to read raster data.")

    expected = int(buf_xsize) * int(buf_ysize)
    arr = np.frombuffer(raw, dtype=dtype, count=expected)
    if int(arr.size) != expected:
        raise RuntimeError(
            f"GDAL returned {int(arr.size):,} values; expected {expected:,}."
        )
    # Own the memory independently of GDAL/Python's temporary byte buffer.
    return arr.reshape((buf_ysize, buf_xsize)).copy()


class _GdalRasterDataset:
    SAMPLE_TILE = 256
    SAMPLE_CACHE_TILES = 32

    def __init__(self, path):
        if gdal is None:
            raise RuntimeError("GDAL is unavailable: " + (GDAL_IMPORT_ERROR or "unknown error"))
        self.path = str(path)
        self._ds = gdal.OpenEx(self.path, gdal.OF_RASTER | gdal.OF_READONLY)
        if self._ds is None:
            raise RuntimeError(f"GDAL could not open raster: {self.path}")
        if int(self._ds.RasterCount) < 1:
            raise RuntimeError("Raster has no bands.")
        self._band = self._ds.GetRasterBand(1)
        self._mask_band = None
        try:
            self._mask_band = self._band.GetMaskBand()
        except Exception:
            self._mask_band = None
        self._gt = tuple(float(v) for v in self._ds.GetGeoTransform())
        self._inv_gt = _inverse_gt(self._gt)
        self._sample_cache = OrderedDict()

        self.width = int(self._ds.RasterXSize)
        self.height = int(self._ds.RasterYSize)
        self.nodata = self._band.GetNoDataValue()

        # Pixel-axis vector lengths; correct for north-up and rotated rasters.
        self.res = (
            float(math.hypot(self._gt[1], self._gt[4])),
            float(math.hypot(self._gt[2], self._gt[5])),
        )
        corners = [
            _apply_gt(self._gt, 0, 0),
            _apply_gt(self._gt, self.width, 0),
            _apply_gt(self._gt, 0, self.height),
            _apply_gt(self._gt, self.width, self.height),
        ]
        xs = [p[0] for p in corners]
        ys = [p[1] for p in corners]
        self.bounds = _Bounds(min(xs), min(ys), max(xs), max(ys))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        self._sample_cache.clear()
        self._mask_band = None
        self._band = None
        self._ds = None

    def window(self, xmin, ymin, xmax, ymax):
        # Convert all four world-space corners to pixel coordinates.  This also
        # works for rotated rasters, unlike north-up-only arithmetic.
        corners = [
            (float(xmin), float(ymin)),
            (float(xmin), float(ymax)),
            (float(xmax), float(ymin)),
            (float(xmax), float(ymax)),
        ]
        cols = []
        rows = []
        for x, y in corners:
            c, r = _apply_gt(self._inv_gt, x, y)
            cols.append(c)
            rows.append(r)
        c0 = max(0.0, min(cols))
        r0 = max(0.0, min(rows))
        c1 = min(float(self.width), max(cols))
        r1 = min(float(self.height), max(rows))
        return _Window(c0, r0, max(0.0, c1 - c0), max(0.0, r1 - r0))

    def window_transform(self, window):
        col = float(window.col_off)
        row = float(window.row_off)
        x0, y0 = _apply_gt(self._gt, col, row)
        return _Affine(
            self._gt[1], self._gt[2], x0,
            self._gt[4], self._gt[5], y0,
        )

    def read(self, band_index, window=None, out_shape=None, resampling=None, masked=False):
        if int(band_index) != 1:
            raise ValueError("This viewer currently reads raster band 1 only.")
        if window is None:
            xoff = yoff = 0
            xsize, ysize = self.width, self.height
        else:
            w = window.round_offsets().round_lengths()
            xoff, yoff = int(w.col_off), int(w.row_off)
            xsize, ysize = int(w.width), int(w.height)
        if xsize <= 0 or ysize <= 0:
            return np.ma.array(np.empty((0, 0), dtype=np.float32), mask=True) if masked else np.empty((0, 0), dtype=np.float32)

        arr = _read_band_array(
            self._band,
            xoff, yoff, xsize, ysize,
            out_shape=out_shape,
            resample_alg=(gdal.GRIORA_NearestNeighbour if out_shape is not None else None),
        )
        if not masked:
            return arr

        invalid = np.zeros(arr.shape, dtype=bool)
        if self._mask_band is not None:
            try:
                mask_arr = _read_band_array(
                    self._mask_band,
                    xoff, yoff, xsize, ysize,
                    out_shape=out_shape,
                    resample_alg=(gdal.GRIORA_NearestNeighbour if out_shape is not None else None),
                )
                invalid |= mask_arr == 0
            except Exception:
                pass
        invalid |= ~np.isfinite(arr)
        if self.nodata is not None:
            try:
                nd = float(self.nodata)
                if np.isfinite(nd) and np.issubdtype(arr.dtype, np.floating):
                    invalid |= np.isclose(arr, nd, rtol=0.0, atol=1e-6)
                else:
                    invalid |= arr == self.nodata
            except Exception:
                pass
        return np.ma.array(arr, mask=invalid, copy=False)

    def validity_mask(self, window=None, out_shape=None, conservative=False):
        """Return a boolean validity grid for display densification.

        When ``conservative`` is True and the read is decimated, GDAL's MIN
        resampling is used on the raster mask. A display sample is therefore
        considered valid only when the corresponding source block contains no
        masked/NoData cells. This keeps native-Z scatter away from voids, water
        masks, missing tiles, and raster edges.
        """
        if window is None:
            xoff = yoff = 0
            xsize, ysize = self.width, self.height
        else:
            w = window.round_offsets().round_lengths()
            xoff, yoff = int(w.col_off), int(w.row_off)
            xsize, ysize = int(w.width), int(w.height)
        if xsize <= 0 or ysize <= 0:
            shape = tuple(out_shape) if out_shape is not None else (0, 0)
            return np.zeros(shape, dtype=bool)

        if self._mask_band is None:
            # GDAL normally supplies an all-valid mask even without explicit
            # NoData.  Fall back to sampled data validity if it does not.
            arr = self.read(1, window=window, out_shape=out_shape, masked=True)
            return ~np.asarray(np.ma.getmaskarray(arr), dtype=bool)

        resample_alg = None
        if out_shape is not None:
            if conservative:
                resample_alg = getattr(gdal, "GRIORA_Min", gdal.GRIORA_NearestNeighbour)
            else:
                resample_alg = gdal.GRIORA_NearestNeighbour
        try:
            mask_arr = _read_band_array(
                self._mask_band,
                xoff, yoff, xsize, ysize,
                out_shape=out_shape,
                resample_alg=resample_alg,
            )
        except Exception:
            mask_arr = None
        if mask_arr is None:
            arr = self.read(1, window=window, out_shape=out_shape, masked=True)
            return ~np.asarray(np.ma.getmaskarray(arr), dtype=bool)
        return np.asarray(mask_arr) != 0

    def _tile(self, tx, ty):
        key = (int(tx), int(ty))
        cached = self._sample_cache.get(key)
        if cached is not None:
            self._sample_cache.move_to_end(key)
            return cached
        xsize = min(self.SAMPLE_TILE, self.width - int(tx))
        ysize = min(self.SAMPLE_TILE, self.height - int(ty))
        data = _read_band_array(
            self._band, int(tx), int(ty), int(xsize), int(ysize)
        )
        valid = np.isfinite(data)
        if self._mask_band is not None:
            try:
                marr = _read_band_array(
                    self._mask_band, int(tx), int(ty), int(xsize), int(ysize)
                )
                valid &= marr != 0
            except Exception:
                pass
        if self.nodata is not None:
            try:
                nd = float(self.nodata)
                if np.isfinite(nd) and np.issubdtype(data.dtype, np.floating):
                    valid &= ~np.isclose(data, nd, rtol=0.0, atol=1e-6)
                else:
                    valid &= data != self.nodata
            except Exception:
                pass
        cached = (data, valid)
        self._sample_cache[key] = cached
        while len(self._sample_cache) > self.SAMPLE_CACHE_TILES:
            self._sample_cache.popitem(last=False)
        return cached

    def sample(self, coords, masked=False):
        results = []
        for x, y in coords:
            colf, rowf = _apply_gt(self._inv_gt, float(x), float(y))
            col = int(math.floor(colf))
            row = int(math.floor(rowf))
            ok = 0 <= col < self.width and 0 <= row < self.height
            value = np.nan
            if ok:
                tx = (col // self.SAMPLE_TILE) * self.SAMPLE_TILE
                ty = (row // self.SAMPLE_TILE) * self.SAMPLE_TILE
                data, valid = self._tile(tx, ty)
                rr = row - ty
                cc = col - tx
                ok = bool(valid[rr, cc])
                if ok:
                    value = data[rr, cc]
            if masked:
                results.append(np.ma.array([value], mask=[not ok]))
            else:
                results.append(np.asarray([value]))
        return results


class _RasterioCompat:
    windows = _WindowsNamespace()

    @staticmethod
    def open(path):
        return _GdalRasterDataset(path)


rasterio = _RasterioCompat() if gdal is not None else None


# --- Built-in Qt OpenGL replacement for pyqtgraph/PyOpenGL ------------------
# Only the tiny API surface used by this viewer is implemented.  This keeps the
# plugin self-contained and removes two external Python packages from install.

GL_POINTS = 0x0000
GL_LINES = 0x0001
GL_TRIANGLES = 0x0004
GL_FLOAT = 0x1406
GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_DEPTH_TEST = 0x0B71
GL_PROGRAM_POINT_SIZE = 0x8642
GL_COLOR_BUFFER_BIT = 0x00004000
GL_DEPTH_BUFFER_BIT = 0x00000100


class _NativeGL:
    """
    Tiny cross-platform resolver for the OpenGL 1.1 entry points used here.

    QGIS 3.44/PyQt5 does not expose QOpenGLContext.functions() or
    QOpenGLFunctions consistently across Windows builds.  The calls below are
    core OpenGL 1.1 functions, so they can be resolved directly from the system
    OpenGL library without PyOpenGL or any pip-installed package.
    """

    def __init__(self):
        self._lib = self._load_library()
        self.glClearColor = self._bind("glClearColor", None, [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float])
        self.glClear = self._bind("glClear", None, [ctypes.c_uint])
        self.glEnable = self._bind("glEnable", None, [ctypes.c_uint])
        self.glBlendFunc = self._bind("glBlendFunc", None, [ctypes.c_uint, ctypes.c_uint])
        self.glDrawArrays = self._bind("glDrawArrays", None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int])
        self.glLineWidth = self._bind("glLineWidth", None, [ctypes.c_float])

    @staticmethod
    def _load_library():
        if sys.platform.startswith("win"):
            return ctypes.WinDLL("opengl32.dll")
        if sys.platform == "darwin":
            return ctypes.CDLL("/System/Library/Frameworks/OpenGL.framework/OpenGL")
        name = ctypes.util.find_library("GL") or "libGL.so.1"
        return ctypes.CDLL(name)

    def _bind(self, name, restype, argtypes):
        fn = getattr(self._lib, name)
        fn.restype = restype
        fn.argtypes = argtypes
        return fn


class _MiniGLItem:
    def __init__(self):
        self._view = None
        self._pos_vbo = None
        self._color_vbo = None
        self._gpu_dirty = True

    def _mark_dirty(self):
        self._gpu_dirty = True
        if self._view is not None:
            self._view.update()

    def _destroy_buffers(self):
        for buf in (self._pos_vbo, self._color_vbo):
            try:
                if buf is not None and buf.isCreated():
                    buf.destroy()
            except Exception:
                pass
        self._pos_vbo = None
        self._color_vbo = None


class _MiniGLScatterPlotItem(_MiniGLItem):
    def __init__(self, pxMode=True):
        super().__init__()
        self.pos = np.empty((0, 3), dtype=np.float32)
        self.color = np.empty((0, 4), dtype=np.float32)
        self.size = 2.0
        self.z_scale = 1.0
        self.visible = True

    def setData(self, pos=None, color=None, size=None, pxMode=True, **kwargs):
        if pos is not None:
            p = np.asarray(pos, dtype=np.float32)
            if p.size == 0:
                p = np.empty((0, 3), dtype=np.float32)
            p = np.ascontiguousarray(p.reshape((-1, 3)), dtype=np.float32)
            self.pos = p
        if color is not None:
            c = np.asarray(color, dtype=np.float32)
            if c.ndim == 1:
                c = np.tile(c.reshape((1, -1)), (self.pos.shape[0], 1))
            if c.shape[1] == 3:
                c = np.column_stack([c, np.ones((c.shape[0],), dtype=np.float32)])
            self.color = np.ascontiguousarray(c.reshape((-1, 4)), dtype=np.float32)
        elif self.color.shape[0] != self.pos.shape[0]:
            self.color = np.ones((self.pos.shape[0], 4), dtype=np.float32)
        if size is not None:
            self.size = float(size)
        self._mark_dirty()

    def resetTransform(self):
        self.z_scale = 1.0
        if self._view is not None:
            self._view.update()

    def scale(self, x, y, z):
        self.z_scale *= float(z)
        if self._view is not None:
            self._view.update()

    def setVisible(self, visible):
        self.visible = bool(visible)
        if self._view is not None:
            self._view.update()


class _MiniGLLinePlotItem(_MiniGLItem):
    """Simple colored 3D line-segment renderer used for vector overlays and measurements."""

    def __init__(self, pos=None, color=None, width=1.5):
        super().__init__()
        self.pos = np.empty((0, 3), dtype=np.float32)
        self.color = np.empty((0, 4), dtype=np.float32)
        self.width = float(width)
        self.z_scale = 1.0
        self.visible = True
        if pos is not None:
            self.setData(pos=pos, color=color, width=width)

    def setData(self, pos=None, color=None, width=None, **kwargs):
        if pos is not None:
            p = np.asarray(pos, dtype=np.float32)
            if p.size == 0:
                p = np.empty((0, 3), dtype=np.float32)
            self.pos = np.ascontiguousarray(p.reshape((-1, 3)), dtype=np.float32)
        if color is not None:
            c = np.asarray(color, dtype=np.float32)
            if c.ndim == 1:
                c = np.tile(c.reshape((1, -1)), (self.pos.shape[0], 1))
            if c.shape[1] == 3:
                c = np.column_stack([c, np.ones((c.shape[0],), dtype=np.float32)])
            self.color = np.ascontiguousarray(c.reshape((-1, 4)), dtype=np.float32)
        elif self.color.shape[0] != self.pos.shape[0]:
            self.color = np.ones((self.pos.shape[0], 4), dtype=np.float32)
        if width is not None:
            self.width = max(1.0, float(width))
        self._mark_dirty()

    def resetTransform(self):
        self.z_scale = 1.0
        if self._view is not None:
            self._view.update()

    def scale(self, x, y, z):
        self.z_scale *= float(z)
        if self._view is not None:
            self._view.update()

    def setVisible(self, visible):
        self.visible = bool(visible)
        if self._view is not None:
            self._view.update()


class _MiniGLMeshItem(_MiniGLItem):
    """Triangle-list raster surface renderer.

    The mesh is intentionally non-indexed: every three vertices form one
    triangle. This costs some memory but keeps the OpenGL path extremely
    portable across the QGIS 3.x / Qt5 builds supported by the plugin.
    """

    def __init__(self, pos=None, color=None):
        super().__init__()
        self.pos = np.empty((0, 3), dtype=np.float32)
        self.color = np.empty((0, 4), dtype=np.float32)
        self.z_scale = 1.0
        self.visible = True
        self.pickable = True
        if pos is not None:
            self.setData(pos=pos, color=color)

    def setData(self, pos=None, color=None, size=None, pxMode=None, **kwargs):
        if pos is not None:
            p = np.asarray(pos, dtype=np.float32)
            if p.size == 0:
                p = np.empty((0, 3), dtype=np.float32)
            p = np.ascontiguousarray(p.reshape((-1, 3)), dtype=np.float32)
            # Keep complete triangles if a caller passes a malformed count.
            n = (int(p.shape[0]) // 3) * 3
            self.pos = p[:n]
        if color is not None:
            c = np.asarray(color, dtype=np.float32)
            if c.ndim == 1:
                c = np.tile(c.reshape((1, -1)), (self.pos.shape[0], 1))
            if c.shape[1] == 3:
                c = np.column_stack([c, np.ones((c.shape[0],), dtype=np.float32)])
            c = np.ascontiguousarray(c.reshape((-1, 4)), dtype=np.float32)
            self.color = c[:self.pos.shape[0]]
        elif self.color.shape[0] != self.pos.shape[0]:
            self.color = np.ones((self.pos.shape[0], 4), dtype=np.float32)
        self._mark_dirty()

    def resetTransform(self):
        self.z_scale = 1.0
        if self._view is not None:
            self._view.update()

    def scale(self, x, y, z):
        self.z_scale *= float(z)
        if self._view is not None:
            self._view.update()

    def setVisible(self, visible):
        self.visible = bool(visible)
        if self._view is not None:
            self._view.update()


class _MiniGLGridItem(_MiniGLItem):
    def __init__(self):
        super().__init__()
        self.size_x = 100.0
        self.size_y = 100.0
        self.spacing_x = 10.0
        self.spacing_y = 10.0
        self.z = 0.0
        self.visible = True
        self.pos = np.empty((0, 3), dtype=np.float32)
        self.color = np.empty((0, 4), dtype=np.float32)

    def setSize(self, x, y=None):
        self.size_x = float(x)
        self.size_y = float(x if y is None else y)
        self._rebuild()

    def setSpacing(self, x, y=None):
        self.spacing_x = max(abs(float(x)), 1e-12)
        self.spacing_y = max(abs(float(x if y is None else y)), 1e-12)
        self._rebuild()

    def resetTransform(self):
        self.z = 0.0
        self._rebuild()

    def translate(self, x, y, z):
        self.z += float(z)
        self._rebuild()

    def setVisible(self, visible):
        self.visible = bool(visible)
        if self._view is not None:
            self._view.update()

    def _rebuild(self):
        # Keep the visual grid bounded even if callers request extreme spacing.
        nx = max(1, min(101, int(round(self.size_x / self.spacing_x))))
        ny = max(1, min(101, int(round(self.size_y / self.spacing_y))))
        xs = np.linspace(-self.size_x / 2.0, self.size_x / 2.0, nx + 1)
        ys = np.linspace(-self.size_y / 2.0, self.size_y / 2.0, ny + 1)
        verts = []
        for x in xs:
            verts.extend([(x, -self.size_y / 2.0, self.z), (x, self.size_y / 2.0, self.z)])
        for y in ys:
            verts.extend([(-self.size_x / 2.0, y, self.z), (self.size_x / 2.0, y, self.z)])
        self.pos = np.ascontiguousarray(np.asarray(verts, dtype=np.float32))
        self.color = np.tile(np.asarray([[0.42, 0.42, 0.42, 0.70]], dtype=np.float32), (len(verts), 1))
        self._mark_dirty()


class _MiniGLViewWidget(QOpenGLWidget):
    pointPicked = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.opts = {
            "distance": 50.0,
            "azimuth": 45.0,
            "elevation": 25.0,
            "center": QVector3D(0.0, 0.0, 0.0),
        }
        self._bg = (0.0, 0.0, 0.0, 1.0)
        self._program = None
        self._gl_funcs = None
        self._last_mouse_pos = None
        self._press_mouse_pos = None
        self._pick_mode = False
        self._cursor_zoom = True
        self._pick_radius_px = 45.0
        # Screen-space point fill: slightly enlarge points when zoomed out and
        # shrink them back near the reference inspection distance. Geometry is
        # untouched; this only changes gl_PointSize.
        self._adaptive_point_size = True
        self._adaptive_ref_distance = 50.0
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(320, 240)

    def setBackgroundColor(self, color):
        if isinstance(color, str):
            if color.lower() in ("k", "black"):
                self._bg = (0.0, 0.0, 0.0, 1.0)
            elif color.lower() in ("w", "white"):
                self._bg = (1.0, 1.0, 1.0, 1.0)
        self.update()

    def addItem(self, item):
        if item not in self.items:
            item._view = self
            self.items.append(item)
            if isinstance(item, _MiniGLGridItem):
                item._rebuild()
            self.update()

    def removeItem(self, item):
        if item in self.items:
            try:
                self.makeCurrent()
                item._destroy_buffers()
                self.doneCurrent()
            except Exception:
                pass
            self.items.remove(item)
            item._view = None
            self.update()

    def setCameraPosition(self, pos=None, distance=None, azimuth=None, elevation=None, **kwargs):
        if pos is not None:
            if isinstance(pos, QVector3D):
                self.opts["center"] = QVector3D(pos)
            else:
                try:
                    self.opts["center"] = QVector3D(float(pos[0]), float(pos[1]), float(pos[2]))
                except Exception:
                    pass
        if distance is not None:
            self.opts["distance"] = max(float(distance), 1e-6)
        if azimuth is not None:
            self.opts["azimuth"] = float(azimuth)
        if elevation is not None:
            self.opts["elevation"] = max(-89.5, min(89.5, float(elevation)))
        self.update()

    def initializeGL(self):
        # Resolve only the OpenGL 1.1 functions we need directly from the
        # platform OpenGL library. This avoids PyQt wrapper differences between
        # QGIS builds and requires no external Python package.
        self._gl_funcs = _NativeGL()
        fmt = self.context().format()
        major = int(fmt.majorVersion())
        minor = int(fmt.minorVersion())
        core_profile = fmt.profile() == QSurfaceFormat.CoreProfile
        if core_profile and (major > 3 or (major == 3 and minor >= 3)):
            vertex = """#version 330 core\nin vec3 a_position;\nin vec4 a_color;\nuniform mat4 u_mvp;\nuniform float u_pointSize;\nout vec4 v_color;\nvoid main(){ gl_Position=u_mvp*vec4(a_position,1.0); gl_PointSize=u_pointSize; v_color=a_color; }"""
            fragment = """#version 330 core\nin vec4 v_color;\nout vec4 fragColor;\nvoid main(){ if(v_color.a <= 0.001) discard; fragColor=v_color; }"""
        elif core_profile and major >= 3:
            # OpenGL 3.2 core maps to GLSL 1.50; this matters on older
            # integrated GPUs where requesting GLSL 3.30 would fail.
            vertex = """#version 150\nin vec3 a_position;\nin vec4 a_color;\nuniform mat4 u_mvp;\nuniform float u_pointSize;\nout vec4 v_color;\nvoid main(){ gl_Position=u_mvp*vec4(a_position,1.0); gl_PointSize=u_pointSize; v_color=a_color; }"""
            fragment = """#version 150\nin vec4 v_color;\nout vec4 fragColor;\nvoid main(){ if(v_color.a <= 0.001) discard; fragColor=v_color; }"""
        else:
            # Compatibility/legacy contexts: GLSL 1.20 works on the broadest
            # range of hardware supported by QGIS 3.x.
            vertex = """#version 120\nattribute vec3 a_position;\nattribute vec4 a_color;\nuniform mat4 u_mvp;\nuniform float u_pointSize;\nvarying vec4 v_color;\nvoid main(){ gl_Position=u_mvp*vec4(a_position,1.0); gl_PointSize=u_pointSize; v_color=a_color; }"""
            fragment = """#version 120\nvarying vec4 v_color;\nvoid main(){ if(v_color.a <= 0.001) discard; gl_FragColor=v_color; }"""
        program = QOpenGLShaderProgram(self)
        if not program.addShaderFromSourceCode(QOpenGLShader.Vertex, vertex):
            raise RuntimeError("OpenGL vertex shader failed: " + program.log())
        if not program.addShaderFromSourceCode(QOpenGLShader.Fragment, fragment):
            raise RuntimeError("OpenGL fragment shader failed: " + program.log())
        if not program.link():
            raise RuntimeError("OpenGL shader link failed: " + program.log())
        self._program = program

    def _upload_item(self, item):
        item._destroy_buffers()
        if item.pos is None or int(item.pos.shape[0]) == 0:
            item._gpu_dirty = False
            return
        pos = np.ascontiguousarray(item.pos, dtype=np.float32)
        col = np.ascontiguousarray(item.color, dtype=np.float32)
        if col.shape[0] != pos.shape[0]:
            col = np.ones((pos.shape[0], 4), dtype=np.float32)
        pbuf = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        cbuf = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        if not pbuf.create() or not cbuf.create():
            raise RuntimeError("Could not allocate OpenGL vertex buffers.")
        pbuf.bind()
        pdata = pos.tobytes(order="C")
        pbuf.allocate(pdata, len(pdata))
        pbuf.release()
        cbuf.bind()
        cdata = col.tobytes(order="C")
        cbuf.allocate(cdata, len(cdata))
        cbuf.release()
        item._pos_vbo = pbuf
        item._color_vbo = cbuf
        item._gpu_dirty = False

    def _camera_matrices(self):
        w = max(1, int(self.width()))
        h = max(1, int(self.height()))
        distance = max(float(self.opts.get("distance", 50.0)), 1e-5)
        az = math.radians(float(self.opts.get("azimuth", 45.0)))
        el = math.radians(float(self.opts.get("elevation", 25.0)))
        center = self.opts.get("center", QVector3D(0.0, 0.0, 0.0))
        direction = QVector3D(
            math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el),
        )
        eye = center + direction * distance
        # A spherical-camera up vector avoids the abrupt 180-degree roll that
        # occurs when switching between global-Z and global-Y near top view.
        up = QVector3D(
            -math.sin(el) * math.cos(az),
            -math.sin(el) * math.sin(az),
            math.cos(el),
        )
        view = QMatrix4x4()
        view.lookAt(eye, center, up)
        proj = QMatrix4x4()
        near = max(distance / 5000.0, 1e-4)
        far = max(distance * 20.0, near * 1000.0)
        proj.perspective(45.0, float(w) / float(h), near, far)
        return proj, view

    def paintGL(self):
        funcs = self._gl_funcs
        if funcs is None:
            funcs = _NativeGL()
            self._gl_funcs = funcs
        funcs.glClearColor(*self._bg)
        funcs.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        funcs.glEnable(GL_DEPTH_TEST)
        funcs.glEnable(GL_BLEND)
        funcs.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        try:
            funcs.glEnable(GL_PROGRAM_POINT_SIZE)
        except Exception:
            pass
        if self._program is None:
            return
        proj, view = self._camera_matrices()
        program = self._program
        program.bind()
        pos_loc = program.attributeLocation("a_position")
        col_loc = program.attributeLocation("a_color")

        try:
            for item in list(self.items):
                if not getattr(item, "visible", True):
                    continue
                if getattr(item, "_gpu_dirty", True):
                    self._upload_item(item)
                if item._pos_vbo is None or int(item.pos.shape[0]) == 0:
                    continue

                model = QMatrix4x4()
                if isinstance(item, (_MiniGLGridItem, _MiniGLLinePlotItem)):
                    primitive = GL_LINES
                elif isinstance(item, _MiniGLMeshItem):
                    primitive = GL_TRIANGLES
                else:
                    primitive = GL_POINTS
                point_size = 1.0
                if isinstance(item, _MiniGLScatterPlotItem):
                    model.scale(1.0, 1.0, float(item.z_scale))
                    point_size = max(0.1, float(item.size) * self._adaptive_point_factor())
                elif isinstance(item, (_MiniGLLinePlotItem, _MiniGLMeshItem)):
                    model.scale(1.0, 1.0, float(item.z_scale))
                    if isinstance(item, _MiniGLLinePlotItem):
                        try:
                            funcs.glLineWidth(float(item.width))
                        except Exception:
                            pass

                mvp = proj * view * model
                program.setUniformValue("u_mvp", mvp)
                program.setUniformValue("u_pointSize", point_size)

                item._pos_vbo.bind()
                program.enableAttributeArray(pos_loc)
                program.setAttributeBuffer(pos_loc, GL_FLOAT, 0, 3, 0)
                item._color_vbo.bind()
                program.enableAttributeArray(col_loc)
                program.setAttributeBuffer(col_loc, GL_FLOAT, 0, 4, 0)
                funcs.glDrawArrays(primitive, 0, int(item.pos.shape[0]))
                program.disableAttributeArray(pos_loc)
                program.disableAttributeArray(col_loc)
                item._pos_vbo.release()
                item._color_vbo.release()
        finally:
            program.release()

    def setPickMode(self, enabled: bool):
        self._pick_mode = bool(enabled)
        try:
            self.setCursor(Qt.CrossCursor if self._pick_mode else Qt.ArrowCursor)
        except Exception:
            pass

    def setCursorZoomEnabled(self, enabled: bool):
        self._cursor_zoom = bool(enabled)

    def setAdaptivePointSizeEnabled(self, enabled: bool):
        self._adaptive_point_size = bool(enabled)
        self.update()

    def setAdaptivePointReference(self, distance: float):
        try:
            self._adaptive_ref_distance = max(float(distance), 1e-6)
        except Exception:
            self._adaptive_ref_distance = max(float(self.opts.get("distance", 50.0)), 1e-6)
        self.update()

    def _adaptive_point_factor(self) -> float:
        if not bool(getattr(self, "_adaptive_point_size", False)):
            return 1.0
        d = max(float(self.opts.get("distance", 50.0)), 1e-6)
        ref = max(float(getattr(self, "_adaptive_ref_distance", d)), 1e-6)
        # Very mild scaling: far views close visual gaps, close inspection keeps
        # points crisp. Clamp hard so the user's point-size setting remains the
        # dominant control.
        ratio = max(0.05, min(20.0, d / ref))
        return max(0.82, min(1.38, ratio ** 0.28))

    def _camera_basis(self):
        distance = max(float(self.opts.get("distance", 50.0)), 1e-6)
        az = math.radians(float(self.opts.get("azimuth", 45.0)))
        el = math.radians(float(self.opts.get("elevation", 25.0)))
        center_q = self.opts.get("center", QVector3D(0.0, 0.0, 0.0))
        center = np.array([center_q.x(), center_q.y(), center_q.z()], dtype=np.float64)
        direction = np.array([
            math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el),
        ], dtype=np.float64)
        eye = center + direction * distance
        forward = -direction
        up = np.array([
            -math.sin(el) * math.cos(az),
            -math.sin(el) * math.sin(az),
            math.cos(el),
        ], dtype=np.float64)
        up /= max(float(np.linalg.norm(up)), 1e-12)
        right = np.cross(forward, up)
        right /= max(float(np.linalg.norm(right)), 1e-12)
        up = np.cross(right, forward)
        up /= max(float(np.linalg.norm(up)), 1e-12)
        return eye, forward, right, up

    def _best_point_at(self, pos, max_px=None, sample_cap=300000):
        """Return the closest rendered DEM point to a screen position."""
        try:
            px = float(pos.x())
            py = float(pos.y())
        except Exception:
            return None
        w = max(1.0, float(self.width()))
        h = max(1.0, float(self.height()))
        eye, forward, right, up = self._camera_basis()
        tan_half = math.tan(math.radians(45.0) * 0.5)
        aspect = w / h
        best = None
        best_d2 = float("inf")

        for item in list(self.items):
            if (not isinstance(item, (_MiniGLScatterPlotItem, _MiniGLMeshItem)) or not getattr(item, "visible", True)
                    or not getattr(item, "pickable", True)):
                continue
            pick_pos = getattr(item, "pick_pos", None)
            if pick_pos is not None:
                pts_all = np.asarray(pick_pos, dtype=np.float32)
            else:
                pts_all = np.asarray(getattr(item, "pos", np.empty((0, 3))), dtype=np.float32)
            if pts_all.ndim != 2 or pts_all.shape[0] == 0:
                continue
            n_all = int(pts_all.shape[0])
            stride = max(1, int(math.ceil(n_all / float(max(1, int(sample_cap))))))
            sample_idx = np.arange(0, n_all, stride, dtype=np.int64) if stride > 1 else None
            pts = pts_all[sample_idx] if sample_idx is not None else pts_all
            # Projection must match the visual Z exaggeration. Float32 is enough
            # for screen picking and keeps wheel-focus responsive on large DEMs.
            x = pts[:, 0].astype(np.float32, copy=False)
            y = pts[:, 1].astype(np.float32, copy=False)
            z = pts[:, 2].astype(np.float32, copy=False) * float(getattr(item, "z_scale", 1.0))
            vx = x - eye[0]
            vy = y - eye[1]
            vz = z - eye[2]
            depth = vx * forward[0] + vy * forward[1] + vz * forward[2]
            valid = depth > 1e-9
            pick_alpha_all = np.asarray(getattr(item, "pick_alpha", np.empty((0,))), dtype=np.float32)
            if pick_alpha_all.ndim == 1 and pick_alpha_all.shape[0] == n_all:
                pick_alpha = pick_alpha_all[sample_idx] if sample_idx is not None else pick_alpha_all
                valid &= pick_alpha > 0.001
            elif pick_pos is None:
                colors_all = np.asarray(getattr(item, "color", np.empty((0, 4))))
                if colors_all.ndim == 2 and colors_all.shape[0] == n_all and colors_all.shape[1] >= 4:
                    colors = colors_all[sample_idx] if sample_idx is not None else colors_all
                    valid &= colors[:, 3] > 0.001
            if not np.any(valid):
                continue
            cam_x = vx * right[0] + vy * right[1] + vz * right[2]
            cam_y = vx * up[0] + vy * up[1] + vz * up[2]
            ndc_x = cam_x / np.maximum(depth * tan_half * aspect, 1e-12)
            ndc_y = cam_y / np.maximum(depth * tan_half, 1e-12)
            sx = (ndc_x * 0.5 + 0.5) * w
            sy = (0.5 - ndc_y * 0.5) * h
            d2 = (sx - px) ** 2 + (sy - py) ** 2
            d2[~valid] = np.inf
            idx = int(np.argmin(d2))
            value = float(d2[idx])
            if not np.isfinite(value) or value >= best_d2:
                continue
            best_d2 = value
            original_idx = int(sample_idx[idx]) if sample_idx is not None else idx
            center_world = np.asarray(getattr(item, "world_center", np.zeros(3)), dtype=np.float64)
            local = np.asarray(pts_all[original_idx], dtype=np.float64)
            display_local = np.array([local[0], local[1], local[2] * float(getattr(item, "z_scale", 1.0))], dtype=np.float64)
            best = {
                "world": local + center_world,
                "local": local,
                "display_local": display_local,
                "distance_px": math.sqrt(value),
                "item": item,
                "layer_id": getattr(item, "layer_key", ""),
                "layer_name": getattr(item, "layer_name", "Surface"),
            }

        if best is None:
            return None
        if max_px is not None and float(best["distance_px"]) > float(max_px):
            return None
        return best

    def focusPointAt(self, pos, max_px=180.0):
        pick = self._best_point_at(pos, max_px=max_px, sample_cap=300000)
        if pick is None:
            return False
        p = pick["display_local"]
        self.opts["center"] = QVector3D(float(p[0]), float(p[1]), float(p[2]))
        self.update()
        return True

    def mousePressEvent(self, event):
        self._last_mouse_pos = event.pos()
        self._press_mouse_pos = event.pos()
        self.setFocus()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._last_mouse_pos is None:
            self._last_mouse_pos = event.pos()
            return
        dx = event.pos().x() - self._last_mouse_pos.x()
        dy = event.pos().y() - self._last_mouse_pos.y()
        buttons = event.buttons()
        # In 3D measurement mode, a left click is reserved for point picking.
        if (buttons & Qt.LeftButton) and not self._pick_mode:
            self.opts["azimuth"] = float(self.opts.get("azimuth", 45.0)) - dx * 0.45
            self.opts["elevation"] = max(-89.5, min(89.5, float(self.opts.get("elevation", 25.0)) + dy * 0.35))
            self.update()
        elif buttons & (Qt.MiddleButton | Qt.RightButton):
            az = math.radians(float(self.opts.get("azimuth", 45.0)))
            el = math.radians(float(self.opts.get("elevation", 25.0)))
            right = np.array([-math.sin(az), math.cos(az), 0.0], dtype=float)
            screen_up = np.array([
                -math.sin(el) * math.cos(az),
                -math.sin(el) * math.sin(az),
                math.cos(el),
            ], dtype=float)
            scale = max(float(self.opts.get("distance", 50.0)), 1e-6) * 0.0018
            delta = (-dx * right + dy * screen_up) * scale
            center = self.opts.get("center", QVector3D(0.0, 0.0, 0.0))
            self.opts["center"] = QVector3D(
                center.x() + float(delta[0]),
                center.y() + float(delta[1]),
                center.z() + float(delta[2]),
            )
            self.update()
        self._last_mouse_pos = event.pos()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        press = self._press_mouse_pos
        release = event.pos()
        self._last_mouse_pos = None
        self._press_mouse_pos = None
        if self._pick_mode and event.button() == Qt.LeftButton and press is not None:
            moved = math.hypot(float(release.x() - press.x()), float(release.y() - press.y()))
            if moved <= 5.0:
                pick = self._best_point_at(release, max_px=self._pick_radius_px, sample_cap=750000)
                if pick is not None:
                    try:
                        self.pointPicked.emit(pick)
                    except Exception:
                        pass
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and not self._pick_mode:
            if self.focusPointAt(event.pos(), max_px=180.0):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        steps = float(event.angleDelta().y()) / 120.0
        if steps != 0.0:
            distance = max(float(self.opts.get("distance", 50.0)), 1e-6)
            factor = math.pow(0.82, steps)
            if steps > 0.0 and self._cursor_zoom:
                pick = self._best_point_at(event.pos(), max_px=180.0, sample_cap=150000)
                if pick is not None:
                    target = pick["display_local"]
                    center = self.opts.get("center", QVector3D(0.0, 0.0, 0.0))
                    c = np.array([center.x(), center.y(), center.z()], dtype=np.float64)
                    blend = min(0.35, max(0.10, 1.0 - factor))
                    c = c + (np.asarray(target, dtype=np.float64) - c) * blend
                    self.opts["center"] = QVector3D(float(c[0]), float(c[1]), float(c[2]))
            distance *= factor
            self.opts["distance"] = max(distance, 1e-6)
            self.update()
        event.accept()

    def closeEvent(self, event):
        try:
            self.makeCurrent()
            for item in self.items:
                item._destroy_buffers()
            self.doneCurrent()
        except Exception:
            pass
        super().closeEvent(event)


class _MiniPG:
    Vector = QVector3D


class _MiniGL:
    GLViewWidget = _MiniGLViewWidget
    GLScatterPlotItem = _MiniGLScatterPlotItem
    GLLinePlotItem = _MiniGLLinePlotItem
    GLMeshItem = _MiniGLMeshItem
    GLGridItem = _MiniGLGridItem


pg = _MiniPG()
gl = _MiniGL()


def log_exception(context: str, exc: Exception, level=Qgis.Warning) -> None:
    """Log failures with enough context to troubleshoot a shared plugin build."""
    try:
        detail = "{}: {}\n{}".format(context, exc, traceback.format_exc())
        QgsMessageLog.logMessage(detail, "Terrain Viewer", level=level)
    except Exception:
        pass


def is_raster_layer(layer) -> bool:
    if layer is None:
        return False
    try:
        return layer.type() == Qgis.LayerType.Raster
    except Exception:
        try:
            return layer.type() == layer.RasterLayer
        except Exception:
            return False


def is_line_vector_layer(layer) -> bool:
    if layer is None:
        return False
    try:
        is_vector = layer.type() == Qgis.LayerType.Vector
    except Exception:
        try:
            is_vector = layer.type() == layer.VectorLayer
        except Exception:
            is_vector = False
    if not is_vector:
        return False
    try:
        return QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.LineGeometry
    except Exception:
        return False


def selected_line_vector_layers():
    try:
        layers = iface.layerTreeView().selectedLayers() or []
    except Exception:
        layers = []
    return [lyr for lyr in layers if is_line_vector_layer(lyr)]


def vector_overlay_rgb(index: int):
    palette = [
        (1.00, 0.88, 0.10),
        (0.10, 1.00, 0.95),
        (1.00, 0.25, 0.80),
        (0.65, 1.00, 0.25),
        (1.00, 0.55, 0.10),
        (0.55, 0.65, 1.00),
    ]
    return palette[int(index) % len(palette)]



def raster_source_path(layer) -> str:
    """Return a GDAL-readable source for a QGIS GDAL raster layer."""
    if not is_raster_layer(layer):
        raise ValueError("Layer is not a raster layer.")

    provider_key = str(layer.providerType() or "")
    source = str(layer.source() or "")
    if provider_key and provider_key.lower() != "gdal":
        raise ValueError(
            f"Unsupported raster provider '{provider_key}'. "
            "This build supports GDAL/file-backed rasters."
        )

    try:
        parts = QgsProviderRegistry.instance().decodeUri(provider_key or "gdal", source) or {}
        for key in ("path", "url"):
            value = parts.get(key)
            if value:
                return str(value)
    except Exception as exc:
        log_exception("Could not decode raster URI", exc, Qgis.Info)

    return source


def canvas_to_layer_point(point: QgsPointXY, layer) -> QgsPointXY:
    canvas_crs = iface.mapCanvas().mapSettings().destinationCrs()
    layer_crs = layer.crs()
    if not canvas_crs.isValid() or not layer_crs.isValid() or canvas_crs == layer_crs:
        return QgsPointXY(point)
    transform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
    return transform.transform(point)


def canvas_to_layer_extent(extent: QgsRectangle, layer) -> QgsRectangle:
    canvas_crs = iface.mapCanvas().mapSettings().destinationCrs()
    layer_crs = layer.crs()
    if not canvas_crs.isValid() or not layer_crs.isValid() or canvas_crs == layer_crs:
        return QgsRectangle(extent)
    transform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
    return transform.transformBoundingBox(extent)


def crs_unit_label(layer) -> str:
    try:
        unit = layer.crs().mapUnits()
        label = QgsUnitTypes.toAbbreviatedString(unit)
        return str(label or "map units")
    except Exception:
        return "map units"


def ensure_same_raster_crs(layers) -> None:
    if len(layers) <= 1:
        return
    first = layers[0].crs()
    mismatched = [lyr.name() for lyr in layers[1:] if lyr.crs() != first]
    if mismatched:
        raise ValueError(
            "Multi-raster preview requires the selected rasters to use the same CRS. "
            "Reproject them first or preview them separately."
        )


def dependency_error_text() -> str:
    if gdal is None:
        return "QGIS GDAL Python bindings are unavailable: " + (GDAL_IMPORT_ERROR or "unknown error")
    return ""

class PointCloudPreview(QWidget):
    interaction_finished = pyqtSignal()
    point_picked = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self._center = np.zeros(3, dtype=np.float32)
        self._diag = 1.0
        self._base_pos = None
        self._base_rgba = None
        self._layer_base_pos = []
        self._layer_base_rgba = []
        self._layer_current_pos = []
        self._layer_keys = []
        self._current_size = 2.0
        self._current_zex = 1.0
        self._grid_base_z = 0.0
        self._grid_visible_requested = True
        self._world_spans = np.ones(3, dtype=np.float32)
        self._focus_z_range = None
        self._vector_items = {}
        self._annotation_items = []

        self._hint = QLabel("Draw a box or run cross-section to preview here.")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setFixedHeight(24)
        lay.addWidget(self._hint)

        if gl is None:
            detail = "Qt OpenGL renderer is unavailable"
            self._hint.setText(
                "3D preview renderer is unavailable.\n"
                + detail
            )
            self.view = None
            self.scatter = None
            self.grid = None
            return

        self._interacting = False

        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor('k')
        self.view.setCameraPosition(distance=50, azimuth=45, elevation=25)

        try:
            self.view.installEventFilter(self)
        except Exception:
            pass
        try:
            self.view.pointPicked.connect(self.point_picked.emit)
        except Exception:
            pass

        self.scatter = gl.GLScatterPlotItem(pxMode=True)
        self.view.addItem(self.scatter)
        self.scatters = [self.scatter]

        self.grid = gl.GLGridItem()
        self.grid.setSize(100, 100)
        self.grid.setSpacing(10, 10)
        self.view.addItem(self.grid)

        lay.addWidget(self.view)
        self._hint.setVisible(True)

    @staticmethod
    def _camera_angles(preset: str):
        p = (preset or "").lower().strip()
        if p == "top":
            return 0, 90
        if p == "profile_x":
            return 0, 0
        if p == "profile_y":
            return 90, 0
        return 45, 25

    @staticmethod
    def _rgba(colors_rgb: np.ndarray) -> np.ndarray:
        c = np.asarray(colors_rgb, dtype=np.float32)
        if c.ndim != 2 or c.shape[1] not in (3, 4):
            raise ValueError("colors must be Nx3 or Nx4")
        if c.shape[1] == 3:
            a = np.ones((c.shape[0], 1), dtype=np.float32)
            c = np.hstack([c, a])
        return np.clip(c, 0.0, 1.0)

    def _dbg_setdata(self):
        if not getattr(self, "_dbg_setpoints", False):
            return
        try:
            if not hasattr(self, "_dbg_t0"):
                self._dbg_t0 = time.time()
                self._dbg_n = 0
            self._dbg_n += 1
            if self._dbg_n % 30 == 0:
                print(
                    "scatter.setData calls:",
                    self._dbg_n,
                    "elapsed:",
                    round(time.time() - float(self._dbg_t0), 2),
                    "s",
                )
        except Exception:
            pass

    def reset_camera(self, preset: str = "iso", azimuth_deg: float = None, elevation_deg: float = None) -> None:
        if self.view is None:
            return
        try:
            if azimuth_deg is None or elevation_deg is None:
                az, el = self._camera_angles(preset)
            else:
                az, el = float(azimuth_deg), float(elevation_deg)

            diag = float(getattr(self, "_diag", 1.0) or 1.0)
            center_local = pg.Vector(0.0, 0.0, 0.0)
            focus = getattr(self, "_focus_z_range", None)
            if focus is not None and len(focus) == 2:
                z0, z1 = float(focus[0]), float(focus[1])
                if np.isfinite(z0) and np.isfinite(z1) and z1 > z0:
                    spans = np.asarray(getattr(self, "_world_spans", np.ones(3)), dtype=float)
                    zex = max(float(getattr(self, "_current_zex", 1.0)), 0.01)
                    robust_z_span = (z1 - z0) * zex
                    diag = float(np.linalg.norm([max(spans[0], 1e-6), max(spans[1], 1e-6), max(robust_z_span, 1e-6)]))
                    center_local = pg.Vector(0.0, 0.0, ((z0 + z1) * 0.5 - float(self._center[2])) * zex)

            diag = max(diag, 1.0)
            ref_distance = diag * 1.8
            self.view.setCameraPosition(
                pos=center_local,
                distance=ref_distance,
                azimuth=az,
                elevation=el,
            )
            try:
                self.view.setAdaptivePointReference(ref_distance)
            except Exception:
                pass
        except Exception:
            pass

    def set_focus_z_range(self, zmin=None, zmax=None) -> None:
        if zmin is None or zmax is None:
            self._focus_z_range = None
            return
        z0, z1 = float(zmin), float(zmax)
        self._focus_z_range = (z0, z1) if np.isfinite(z0) and np.isfinite(z1) and z1 > z0 else None

    def clear_layers(self) -> None:
        if self.view is None:
            return

        for s in list(getattr(self, "scatters", []) or []):
            if s is None:
                continue
            if getattr(self, "scatter", None) is not None and s is self.scatter:
                continue
            try:
                self.view.removeItem(s)
            except Exception:
                pass

        self.clear_vector_overlays()
        self.clear_annotations()
        self.scatters = [self.scatter] if getattr(self, "scatter", None) is not None else []
        self._base_pos = None
        self._base_rgba = None
        self._layer_base_pos = []
        self._layer_base_rgba = []
        self._layer_current_pos = []
        self._layer_keys = []
        self._focus_z_range = None
        try:
            if getattr(self, "scatter", None) is not None:
                self.scatter.pick_pos = np.empty((0, 3), dtype=np.float32)
                self.scatter.pick_alpha = np.empty((0,), dtype=np.float32)
        except Exception:
            pass

    def set_cloud_layers(
        self,
        layers,
        size_px: float = 2.0,
        reset_camera: bool = True,
        preset: str = "iso",
        azimuth_deg: float = None,
        elevation_deg: float = None,
    ) -> None:
        if self.view is None or gl is None:
            return

        self.clear_layers()

        try:
            self.scatter.resetTransform()
        except Exception:
            pass
        try:
            self.scatter.setData(pos=np.zeros((0, 3), dtype=np.float32))
        except Exception:
            pass

        self.scatters = []
        try:
            self.scatter.setVisible(False)
        except Exception:
            pass

        if not layers:
            self._hint.setVisible(True)
            try:
                self.grid.setVisible(False)
            except Exception:
                pass
            try:
                self.scatter.resetTransform()
            except Exception:
                pass
            try:
                self.scatter.setData(pos=np.zeros((0, 3), dtype=np.float32))
            except Exception:
                pass
            return

        mins = []
        maxs = []
        total_pts = 0
        for L in layers:
            xyz = np.asarray(L.get("xyz", np.empty((0, 3))), dtype=np.float32)
            if xyz.size == 0:
                continue
            mins.append(xyz.min(axis=0))
            maxs.append(xyz.max(axis=0))
            total_pts += int(xyz.shape[0])

        if total_pts == 0:
            self._hint.setVisible(True)
            try:
                self.grid.setVisible(False)
            except Exception:
                pass
            return

        mins = np.min(np.vstack(mins), axis=0)
        maxs = np.max(np.vstack(maxs), axis=0)
        center = (mins + maxs) / 2.0
        spans = (maxs - mins)
        diag = float(np.linalg.norm(spans))
        diag = max(diag, 1.0)

        self._center = center.astype(np.float32)
        self._diag = float(diag)
        self._world_spans = spans.astype(np.float32)
        self._hint.setVisible(False)

        self._layer_base_pos = []
        self._layer_base_rgba = []
        self._layer_current_pos = []
        self._layer_keys = []

        first_added = False
        for L in layers:
            xyz = np.asarray(L.get("xyz", np.empty((0, 3))), dtype=np.float32)
            if xyz.size == 0:
                continue
            rgba = np.asarray(L.get("rgba", np.ones((xyz.shape[0], 4), dtype=np.float32)), dtype=np.float32)
            if rgba.ndim != 2 or rgba.shape[1] != 4:
                raise ValueError("rgba must be Nx4")

            render_style = str(L.get("render_style", "points") or "points").lower().strip()
            if render_style == "surface":
                s = gl.GLMeshItem()
                self.view.addItem(s)
            elif not first_added and getattr(self, "scatter", None) is not None:
                s = self.scatter
                try:
                    s.setVisible(True)
                except Exception:
                    pass
                first_added = True
            else:
                s = gl.GLScatterPlotItem(pxMode=True)
                self.view.addItem(s)
                first_added = True

            self.scatters.append(s)

            pos = xyz - center
            self._dbg_setdata()
            s.setData(pos=pos, color=rgba, size=float(size_px), pxMode=True)
            s.world_center = np.asarray(center, dtype=np.float64)
            pick_xyz = np.asarray(L.get("pick_xyz", L.get("qc_xyz", xyz)), dtype=np.float32)
            if pick_xyz.ndim == 2 and pick_xyz.shape[1] == 3:
                s.pick_pos = np.ascontiguousarray(pick_xyz - center, dtype=np.float32)
                pick_alpha = np.asarray(L.get("pick_alpha", np.ones((pick_xyz.shape[0],), dtype=np.float32)), dtype=np.float32).reshape((-1,))
                if pick_alpha.shape[0] != pick_xyz.shape[0]:
                    pick_alpha = np.ones((pick_xyz.shape[0],), dtype=np.float32)
                s.pick_alpha = np.ascontiguousarray(pick_alpha, dtype=np.float32)
            else:
                s.pick_pos = None
                s.pick_alpha = np.empty((0,), dtype=np.float32)
            s.layer_key = str(L.get("layer_id", L.get("name", len(self._layer_keys))))
            s.layer_name = str(L.get("name", "Surface"))
            try:
                s.setVisible(bool(L.get("visible", True)))
            except Exception:
                pass
            self._layer_base_pos.append(pos)
            self._layer_base_rgba.append(rgba)
            self._layer_current_pos.append(pos)
            self._layer_keys.append(str(L.get("layer_id", L.get("name", len(self._layer_keys)))))

        if self._layer_base_pos:
            self._base_pos = self._layer_base_pos[0]
            self._base_rgba = self._layer_base_rgba[0]

        self._current_size = float(size_px)
        self._current_zex = 1.0

        try:
            grid_size = max(float(spans[0]), float(spans[1]), 1.0)
            self._grid_base_z = float(mins[2] - center[2])
            self.grid.resetTransform()
            self.grid.setSize(grid_size, grid_size)
            self.grid.setSpacing(grid_size / 10.0, grid_size / 10.0)
            self.grid.translate(0.0, 0.0, self._grid_base_z)
            self.grid.setVisible(bool(self._grid_visible_requested))
        except Exception as exc:
            log_exception("Could not update preview grid", exc, Qgis.Info)

        if reset_camera:
            self.reset_camera(preset=preset, azimuth_deg=azimuth_deg, elevation_deg=elevation_deg)

    def upload_cloud(
        self,
        xyz: np.ndarray,
        colors_rgb: np.ndarray = None,
        size_px: float = 2.0,
        reset_camera: bool = True,
        preset: str = "iso",
        azimuth_deg: float = None,
        elevation_deg: float = None,
    ) -> None:
        if self.view is None or self.scatter is None:
            return

        self.clear_layers()

        xyz = np.asarray(xyz, dtype=np.float32)
        if xyz.size == 0:
            self._hint.setVisible(True)
            try:
                self.grid.setVisible(False)
            except Exception:
                pass
            self._base_pos = None
            self._base_rgba = None
            self._layer_base_pos = []
            self._layer_base_rgba = []
            self._layer_current_pos = []
            self._current_size = float(size_px)
            self._current_zex = 1.0
            try:
                self.scatter.resetTransform()
            except Exception:
                pass
            self._dbg_setdata()
            self.scatter.setData(pos=np.zeros((0, 3), dtype=np.float32))
            return

        self._hint.setVisible(False)

        if colors_rgb is None:
            rgba = np.ones((xyz.shape[0], 4), dtype=np.float32)
        else:
            rgba = self._rgba(colors_rgb)

        mins = xyz.min(axis=0)
        maxs = xyz.max(axis=0)
        center = (mins + maxs) / 2.0
        spans = (maxs - mins)
        diag = float(np.linalg.norm(spans))
        diag = max(diag, 1.0)

        self._center = center.astype(np.float32)
        self._diag = float(diag)
        self._world_spans = spans.astype(np.float32)
        self._focus_z_range = None

        pos = xyz - center
        self._base_pos = pos
        self._base_rgba = rgba
        self._layer_base_pos = [pos]
        self._layer_base_rgba = [rgba]
        self._layer_current_pos = [pos]
        self._current_size = float(size_px)
        self._current_zex = 1.0

        try:
            self.scatter.resetTransform()
        except Exception:
            pass

        self._dbg_setdata()
        self.scatter.setData(pos=pos, color=rgba, size=float(size_px), pxMode=True)
        self.scatter.world_center = np.asarray(center, dtype=np.float64)
        self.scatter.pick_pos = np.ascontiguousarray(pos, dtype=np.float32)
        self.scatter.pick_alpha = np.ones((pos.shape[0],), dtype=np.float32)

        try:
            grid_size = max(float(spans[0]), float(spans[1]), 1.0)
            self._grid_base_z = float(mins[2] - center[2])
            self.grid.resetTransform()
            self.grid.setSize(grid_size, grid_size)
            self.grid.setSpacing(grid_size / 10.0, grid_size / 10.0)
            self.grid.translate(0.0, 0.0, self._grid_base_z)
            self.grid.setVisible(bool(self._grid_visible_requested))
        except Exception as exc:
            log_exception("Could not update preview grid", exc, Qgis.Info)

        if reset_camera:
            self.reset_camera(preset=preset, azimuth_deg=azimuth_deg, elevation_deg=elevation_deg)

    def set_z_exaggeration(self, z_exag: float) -> None:
        if self.scatter is None:
            return
        z = float(z_exag)
        z = max(0.01, min(z, 100.0))
        self._current_zex = z

        scatters = list(getattr(self, "scatters", []) or [])
        if not scatters and self.scatter is not None:
            scatters = [self.scatter]

        ok_transform = True
        try:
            for s in scatters:
                if s is None:
                    continue
                s.resetTransform()
                s.scale(1.0, 1.0, z)
        except Exception:
            ok_transform = False

        try:
            if self.grid is not None:
                self.grid.resetTransform()
                self.grid.translate(0.0, 0.0, float(self._grid_base_z) * z)
        except Exception:
            pass
        for item in list(getattr(self, "_vector_items", {}).values()) + list(getattr(self, "_annotation_items", []) or []):
            try:
                item.resetTransform()
                item.scale(1.0, 1.0, z)
            except Exception:
                pass

        if ok_transform:
            return

        if not self._layer_base_pos or not self._layer_base_rgba:
            return
        try:
            for i, s in enumerate(scatters):
                if s is None:
                    continue
                if i >= len(self._layer_base_pos):
                    continue
                base = np.asarray(self._layer_base_pos[i], dtype=np.float32)
                rgba = self._layer_base_rgba[i]
                pos = base.copy()
                pos[:, 2] *= z
                self._layer_current_pos[i] = pos
                s.setData(pos=pos, color=rgba, size=self._current_size, pxMode=True)
        except Exception:
            pass

    def set_point_size(self, size_px: float) -> None:
        if self.scatter is None:
            return
        s = float(size_px)
        s = max(0.1, min(s, 50.0))
        if abs(float(self._current_size) - s) <= 1e-6:
            return
        self._current_size = s

        scatters = list(getattr(self, "scatters", []) or [])
        if not scatters and self.scatter is not None:
            scatters = [self.scatter]

        if not self._layer_current_pos or not self._layer_base_rgba:
            return

        for i, sc in enumerate(scatters):
            if sc is None:
                continue
            if i >= len(self._layer_current_pos) or i >= len(self._layer_base_rgba):
                continue
            try:
                sc.setData(pos=self._layer_current_pos[i], color=self._layer_base_rgba[i], size=s, pxMode=True)
            except Exception:
                pass

    def set_colors(self, colors_rgb: np.ndarray) -> None:
        if self.scatter is None:
            return
        if self._base_pos is None:
            return
        rgba = self._rgba(colors_rgb)
        self._base_rgba = rgba
        if self._layer_base_rgba:
            self._layer_base_rgba[0] = rgba
        self._dbg_setdata()
        self.scatter.setData(pos=self._base_pos, color=rgba, size=self._current_size, pxMode=True)
        self.set_z_exaggeration(self._current_zex)

    def set_layer_visible(self, key, visible: bool) -> bool:
        key = str(key)
        for i, layer_key in enumerate(list(getattr(self, "_layer_keys", []) or [])):
            if str(layer_key) != key:
                continue
            scatters = list(getattr(self, "scatters", []) or [])
            if i < len(scatters) and scatters[i] is not None:
                try:
                    scatters[i].setVisible(bool(visible))
                    return True
                except Exception:
                    return False
        return False

    def set_layer_colors(self, rgba_layers) -> None:
        scatters = list(getattr(self, "scatters", []) or [])
        for i, rgba in enumerate(list(rgba_layers or [])):
            if i >= len(scatters) or i >= len(self._layer_current_pos):
                continue
            arr = self._rgba(rgba)
            if i < len(self._layer_base_rgba):
                self._layer_base_rgba[i] = arr
            try:
                scatters[i].setData(
                    pos=self._layer_current_pos[i],
                    color=arr,
                    size=self._current_size,
                    pxMode=True,
                )
            except Exception:
                pass

    def set_layer_pick_alpha(self, alpha_layers) -> None:
        scatters = list(getattr(self, "scatters", []) or [])
        for i, alpha in enumerate(list(alpha_layers or [])):
            if i >= len(scatters) or scatters[i] is None:
                continue
            arr = np.asarray(alpha, dtype=np.float32).reshape((-1,))
            pick_pos = getattr(scatters[i], "pick_pos", None)
            if pick_pos is not None and int(np.asarray(pick_pos).shape[0]) == int(arr.shape[0]):
                scatters[i].pick_alpha = np.ascontiguousarray(arr, dtype=np.float32)

    def set_cursor_zoom_enabled(self, enabled: bool) -> None:
        if self.view is not None:
            try:
                self.view.setCursorZoomEnabled(bool(enabled))
            except Exception:
                pass

    def set_adaptive_point_size_enabled(self, enabled: bool) -> None:
        if self.view is not None:
            try:
                self.view.setAdaptivePointSizeEnabled(bool(enabled))
                self.view.setAdaptivePointReference(max(float(self._diag) * 1.8, 1.0))
            except Exception:
                pass

    def set_pick_mode(self, enabled: bool) -> None:
        if self.view is not None:
            try:
                self.view.setPickMode(bool(enabled))
            except Exception:
                pass

    def clear_vector_overlays(self) -> None:
        if self.view is None:
            self._vector_items = {}
            return
        for item in list(getattr(self, "_vector_items", {}).values()):
            try:
                self.view.removeItem(item)
            except Exception:
                pass
        self._vector_items = {}

    def set_vector_overlays(self, overlays) -> None:
        self.clear_vector_overlays()
        if self.view is None:
            return
        center = np.asarray(getattr(self, "_center", np.zeros(3)), dtype=np.float32)
        for overlay in list(overlays or []):
            xyz = np.asarray(overlay.get("xyz", np.empty((0, 3))), dtype=np.float32)
            if xyz.ndim != 2 or xyz.shape[0] < 2:
                continue
            rgba = np.asarray(overlay.get("rgba", [1.0, 1.0, 0.0, 1.0]), dtype=np.float32)
            if rgba.ndim == 1:
                rgba = np.tile(rgba.reshape((1, -1)), (xyz.shape[0], 1))
            item = gl.GLLinePlotItem(pos=xyz - center, color=rgba, width=float(overlay.get("width", 1.5)))
            item.world_center = np.asarray(center, dtype=np.float64)
            item.scale(1.0, 1.0, float(self._current_zex))
            item.setVisible(bool(overlay.get("visible", True)))
            self.view.addItem(item)
            self._vector_items[str(overlay.get("layer_id", overlay.get("name", len(self._vector_items))))] = item

    def set_vector_visible(self, key, visible: bool) -> bool:
        item = getattr(self, "_vector_items", {}).get(str(key))
        if item is None:
            return False
        try:
            item.setVisible(bool(visible))
            return True
        except Exception:
            return False

    def clear_annotations(self) -> None:
        if self.view is None:
            self._annotation_items = []
            return
        for item in list(getattr(self, "_annotation_items", []) or []):
            try:
                self.view.removeItem(item)
            except Exception:
                pass
        self._annotation_items = []

    def show_measurement(self, points_world) -> None:
        self.clear_annotations()
        pts = np.asarray(points_world, dtype=np.float32).reshape((-1, 3))
        if self.view is None or pts.shape[0] == 0:
            return
        center = np.asarray(getattr(self, "_center", np.zeros(3)), dtype=np.float32)
        local = pts - center
        rgba_pts = np.tile(np.asarray([[0.0, 1.0, 1.0, 1.0]], dtype=np.float32), (pts.shape[0], 1))
        marker = gl.GLScatterPlotItem(pxMode=True)
        marker.setData(pos=local, color=rgba_pts, size=max(7.0, float(self._current_size) * 2.2), pxMode=True)
        marker.world_center = np.asarray(center, dtype=np.float64)
        marker.pickable = False
        marker.scale(1.0, 1.0, float(self._current_zex))
        self.view.addItem(marker)
        self._annotation_items.append(marker)
        if pts.shape[0] >= 2:
            line_xyz = np.asarray([pts[0], pts[1]], dtype=np.float32) - center
            line_rgba = np.tile(np.asarray([[0.0, 1.0, 1.0, 1.0]], dtype=np.float32), (2, 1))
            line = gl.GLLinePlotItem(pos=line_xyz, color=line_rgba, width=3.0)
            line.world_center = np.asarray(center, dtype=np.float64)
            line.scale(1.0, 1.0, float(self._current_zex))
            self.view.addItem(line)
            self._annotation_items.append(line)

    def set_points(
        self,
        xyz: np.ndarray,
        colors_rgb: np.ndarray = None,
        size_px: float = 2.0,
        reset_camera: bool = True,
        preset: str = "iso",
        azimuth_deg: float = None,
        elevation_deg: float = None,
        z_exag: float = 1.0,
    ) -> None:
        self.upload_cloud(
            xyz=xyz,
            colors_rgb=colors_rgb,
            size_px=size_px,
            reset_camera=reset_camera,
            preset=preset,
            azimuth_deg=azimuth_deg,
            elevation_deg=elevation_deg,
        )
        self.set_z_exaggeration(z_exag)

    def set_grid_visible(self, visible: bool) -> None:
        self._grid_visible_requested = bool(visible)
        if self.grid is None:
            return
        has_points = bool(self._layer_base_pos) or self._base_pos is not None
        try:
            self.grid.setVisible(bool(visible) and has_points)
        except Exception:
            pass

    def get_camera_state(self):
        """
        Capture the built-in OpenGL camera state.
        """
        if self.view is None:
            return None
        try:
            return dict(self.view.opts)
        except Exception:
            return None

    def restore_camera_state(self, state) -> None:
        if self.view is None or not state:
            return
        try:
            dist = state.get("distance", None)
            az = state.get("azimuth", None)
            el = state.get("elevation", None)
            center = state.get("center", None)
            if dist is None and az is None and el is None and center is None:
                return

            self.view.setCameraPosition(
                pos=center,
                distance=dist if dist is not None else self.view.opts.get("distance", 50),
                azimuth=az if az is not None else self.view.opts.get("azimuth", 45),
                elevation=el if el is not None else self.view.opts.get("elevation", 25),
            )
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        try:
            if obj is self.view:
                t = ev.type()
                if t == QEvent.MouseButtonPress:
                    self._interacting = True
                elif t == QEvent.MouseMove:
                    try:
                        if hasattr(ev, "buttons") and ev.buttons() != Qt.NoButton:
                            self._interacting = True
                    except Exception:
                        pass
                elif t == QEvent.MouseButtonRelease:
                    self._interacting = False
                    try:
                        self.interaction_finished.emit()
                    except Exception:
                        pass
        except Exception:
            pass
        return super().eventFilter(obj, ev)
# ---- Core helpers -----------------------------------------------------------


def height_to_rgb(norm: np.ndarray) -> np.ndarray:
    n = np.clip(norm.astype(float), 0.0, 1.0)
    r = np.clip(1.5 - np.abs(4.0 * n - 3.0), 0.0, 1.0)
    g = np.clip(1.5 - np.abs(4.0 * n - 2.0), 0.0, 1.0)
    b = np.clip(1.5 - np.abs(4.0 * n - 1.0), 0.0, 1.0)
    return np.vstack((r, g, b)).T


SURFACE_PALETTE = [
    (0.15, 0.85, 0.95),
    (1.00, 0.55, 0.10),
    (0.65, 0.45, 1.00),
    (0.95, 0.20, 0.70),
    (0.35, 0.95, 0.35),
    (1.00, 0.90, 0.20),
    (0.95, 0.35, 0.35),
    (0.25, 0.65, 1.00),
]


def surface_rgb(index: int):
    return SURFACE_PALETTE[int(index) % len(SURFACE_PALETTE)]


def cap_points_grid_order(xyz: np.ndarray, max_n: int) -> np.ndarray:
    """Deterministically thin an already grid-ordered cloud.

    Primary reduction happens during raster reads. This final safety cap only
    handles small tile/rounding overages, and keeps a stable regular pattern.
    """
    if xyz is None or getattr(xyz, "size", 0) == 0:
        return xyz
    max_n = int(max_n)
    n = int(xyz.shape[0])
    if max_n <= 0 or n <= max_n:
        return xyz
    step = max(1, int(math.ceil(n / float(max_n))))
    out = xyz[::step]
    if int(out.shape[0]) > max_n:
        out = out[:max_n]
    return np.ascontiguousarray(out)


def _grid_xyz_from_mask(arr_data, valid_mask, tf):
    """Convert valid raster-grid samples to XYZ at pixel centers."""
    arr_data = np.asarray(arr_data)
    valid = np.asarray(valid_mask, dtype=bool)
    if arr_data.ndim != 2 or valid.shape != arr_data.shape or not np.any(valid):
        return np.empty((0, 3), dtype=np.float64)
    idx = np.flatnonzero(valid.ravel())
    ncols = int(arr_data.shape[1])
    r = (idx // ncols).astype(np.float64) + 0.5
    c = (idx % ncols).astype(np.float64) + 0.5
    a, b, c0, d, e, f0 = tf.a, tf.b, tf.c, tf.d, tf.e, tf.f
    xs = (a * c) + (b * r) + c0
    ys = (d * c) + (e * r) + f0
    zs = arr_data.ravel()[idx].astype(np.float64, copy=False)
    return np.column_stack((xs, ys, zs))


def _native_z_scatter_fill_points(arr_data, valid_mask, tf, factor: int, max_points: int):
    """Create display-only density without inventing intermediate elevations.

    Each synthetic point is tied to one *native* valid raster cell.  Its XY is
    deterministically jittered inside that cell footprint while Z is copied
    exactly from the native cell value.  This deliberately avoids bilinear Z
    interpolation, which can create visually convincing but false mid-height
    points along discontinuities such as roof edges, powerlines and vegetation.

    The jitter is deterministic (same raster window -> same visual points), is
    bounded well inside the source pixel, never crosses NoData validity rules,
    and is always constrained by the existing render-point budget.
    """
    factor = int(max(1, factor))
    max_points = int(max(0, max_points))
    arr = np.asarray(arr_data, dtype=np.float64)
    valid = np.asarray(valid_mask, dtype=bool)
    if factor <= 1 or max_points <= 0 or arr.ndim != 2:
        return np.empty((0, 3), dtype=np.float64)
    if valid.shape != arr.shape or not np.any(valid):
        return np.empty((0, 3), dtype=np.float64)

    flat = np.flatnonzero(valid.ravel())
    if flat.size == 0:
        return np.empty((0, 3), dtype=np.float64)

    # 2x -> up to 3 extra display points per native cell; 3x -> up to 8.
    # If the render budget cannot hold all of them, spread the selected native
    # cells regularly across the tile instead of exhausting the first region.
    per_cell = max(1, factor * factor - 1)
    max_cells = max(1, int(math.ceil(max_points / float(per_cell))))
    if flat.size > max_cells:
        step = max(1, int(math.ceil(flat.size / float(max_cells))))
        flat = flat[::step][:max_cells]

    ncols = int(arr.shape[1])
    rr_i = (flat // ncols).astype(np.int64)
    cc_i = (flat % ncols).astype(np.int64)
    z_native = arr[rr_i, cc_i].astype(np.float64, copy=False)

    # Stable tile salt from affine origin/scale.  Do not use Python hash(),
    # whose randomized seed would make the scatter move between QGIS sessions.
    tf_bits = np.frombuffer(
        np.asarray([tf.a, tf.b, tf.c, tf.d, tf.e, tf.f], dtype=np.float64).tobytes(),
        dtype=np.uint64,
    )
    salt = np.uint64(0)
    with np.errstate(over='ignore', invalid='ignore'):
        for i, val in enumerate(tf_bits):
            salt ^= np.uint64(val + np.uint64((i + 1) * 0x9E3779B1))

    r64 = rr_i.astype(np.uint64, copy=False)
    c64 = cc_i.astype(np.uint64, copy=False)
    with np.errstate(over='ignore', invalid='ignore'):
        base = (r64 * np.uint64(0x9E3779B185EBCA87)) ^ (c64 * np.uint64(0xC2B2AE3D27D4EB4F)) ^ salt

    def mix64(x):
        x = np.asarray(x, dtype=np.uint64)
        with np.errstate(over='ignore', invalid='ignore'):
            x = x + np.uint64(0x9E3779B97F4A7C15)
            x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
            x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
            return x ^ (x >> np.uint64(31))

    # Keep synthetic samples well inside each pixel.  A smaller radius is much
    # safer at hard DSM discontinuities than filling the whole cell footprint.
    spread = 0.34 if factor == 2 else 0.40
    chunks = []
    a, b, c0, d, e, f0 = tf.a, tf.b, tf.c, tf.d, tf.e, tf.f
    denom = float(1 << 53)

    for k in range(per_cell):
        key1 = mix64(base ^ np.uint64(((k + 1) * 0xD6E8FEB86659FD93) & 0xFFFFFFFFFFFFFFFF))
        key2 = mix64(base ^ np.uint64(((k + 1) * 0xA5A3564E27F8862D) & 0xFFFFFFFFFFFFFFFF))
        u = ((key1 >> np.uint64(11)).astype(np.float64) / denom)
        v = ((key2 >> np.uint64(11)).astype(np.float64) / denom)
        dr = (u - 0.5) * (2.0 * spread)
        dc = (v - 0.5) * (2.0 * spread)

        r = rr_i.astype(np.float64) + 0.5 + dr
        c = cc_i.astype(np.float64) + 0.5 + dc
        x = (a * c) + (b * r) + c0
        y = (d * c) + (e * r) + f0
        # Critical QC rule: synthetic display points never interpolate Z.
        chunks.append(np.column_stack((x, y, z_native)))

    if not chunks:
        return np.empty((0, 3), dtype=np.float64)
    out = np.concatenate(chunks, axis=0)
    if int(out.shape[0]) > max_points:
        out = cap_points_grid_order(out, max_points)
    return np.ascontiguousarray(out, dtype=np.float64)

def _read_preview_grid(src, sub, stride: int, conservative_validity: bool = False):
    """Read one preview tile and return data, validity, and its display affine."""
    h = int(sub.height)
    ww = int(sub.width)
    local_stride = int(max(1, stride))
    if local_stride > 1 and (h < local_stride or ww < local_stride):
        local_stride = 1

    tf = src.window_transform(sub)
    out_shape = None
    if local_stride > 1:
        out_h = int(np.ceil(h / local_stride))
        out_w = int(np.ceil(ww / local_stride))
        out_shape = (out_h, out_w)
        arr = src.read(1, window=sub, out_shape=out_shape, resampling=Resampling.nearest, masked=True)
        scale_x = ww / float(out_w)
        scale_y = h / float(out_h)
        tf = tf * Affine.scale(scale_x, scale_y)
    else:
        arr = src.read(1, window=sub, masked=True)

    data = np.asarray(np.ma.getdata(arr))
    valid = ~np.asarray(np.ma.getmaskarray(arr), dtype=bool)
    valid &= np.isfinite(data)
    nodata = src.nodata
    if nodata is not None:
        try:
            if np.issubdtype(data.dtype, np.floating) and np.isfinite(float(nodata)):
                valid &= ~np.isclose(data, float(nodata), rtol=0.0, atol=1e-6)
            else:
                valid &= data != nodata
        except Exception:
            pass

    if conservative_validity and local_stride > 1:
        try:
            strict = src.validity_mask(sub, out_shape=out_shape, conservative=True)
            if strict.shape == valid.shape:
                valid &= strict
        except Exception:
            # Failure of the conservative mask must never turn invalid cells
            # into synthetic points; sampled validity still remains enforced.
            pass
    return data, valid, tf


def _mesh_vertices_from_grid(arr_data, valid_mask, tf):
    """Return a non-indexed triangle list for fully valid raster cells.

    Every 2x2 valid raster neighborhood produces two triangles. Any NoData or
    masked corner rejects the entire cell, so the mesh never bridges data voids.
    """
    arr = np.asarray(arr_data, dtype=np.float32)
    valid = np.asarray(valid_mask, dtype=bool)
    if arr.ndim != 2 or valid.shape != arr.shape or min(arr.shape) < 2:
        return np.empty((0, 3), dtype=np.float32)
    quads = valid[:-1, :-1] & valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
    flat = np.flatnonzero(quads.ravel())
    if flat.size == 0:
        return np.empty((0, 3), dtype=np.float32)
    ncols = int(quads.shape[1])
    rr = (flat // ncols).astype(np.float64)
    cc = (flat % ncols).astype(np.float64)
    a, b, c0, d, e, f0 = tf.a, tf.b, tf.c, tf.d, tf.e, tf.f

    def xyz_at(ro, co, z):
        r = rr + float(ro) + 0.5
        c = cc + float(co) + 0.5
        x = (a * c) + (b * r) + c0
        y = (d * c) + (e * r) + f0
        return np.column_stack((x, y, z)).astype(np.float32, copy=False)

    p00 = xyz_at(0, 0, arr[rr.astype(np.int64), cc.astype(np.int64)])
    p01 = xyz_at(0, 1, arr[rr.astype(np.int64), cc.astype(np.int64) + 1])
    p10 = xyz_at(1, 0, arr[rr.astype(np.int64) + 1, cc.astype(np.int64)])
    p11 = xyz_at(1, 1, arr[rr.astype(np.int64) + 1, cc.astype(np.int64) + 1])

    out = np.empty((flat.size * 6, 3), dtype=np.float32)
    out[0::6] = p00
    out[1::6] = p10
    out[2::6] = p11
    out[3::6] = p00
    out[4::6] = p11
    out[5::6] = p01
    return np.ascontiguousarray(out)


def window_to_surface_mesh(
    src_path: str,
    window,
    max_points: int,
    progress_cb,
    vertex_budget: int,
):
    """Build a continuous triangle surface plus native QC samples.

    ``vertex_budget`` is a hard render-vertex cap. The source raster is reduced
    on a regular grid before triangle creation when needed. NoData remains holes.
    QC/measurement points are native sampled raster cells, never triangle-derived.
    """
    budget = max(6, int(vertex_budget or PREVIEW_TARGET_POINTS))
    with rasterio.open(src_path) as src:
        w = window.round_offsets().round_lengths()
        width, height = int(w.width), int(w.height)
        est = width * height
        if est > int(max_points):
            raise ValueError(f"Selection too large ({est:,} cells). Draw smaller box.")
        # Two triangles / six submitted vertices per output grid cell.
        target_grid_cells = max(4, budget // 6)
        stride = 1
        if est > target_grid_cells:
            stride = int(math.ceil(math.sqrt(est / float(target_grid_cells))))
            stride = max(1, min(stride, 64))
        progress_cb(10.0)
        data, valid, tf = _read_preview_grid(src, w, stride, conservative_validity=True)
        progress_cb(45.0)
        native_xyz = _grid_xyz_from_mask(data, valid, tf).astype(np.float32, copy=False)
        mesh_xyz = _mesh_vertices_from_grid(data, valid, tf)
        progress_cb(85.0)
        # Safety fallback: keep complete triangles. Normally the stride estimate
        # already keeps us below budget.
        if int(mesh_xyz.shape[0]) > budget:
            tri_count = int(mesh_xyz.shape[0]) // 3
            keep_tri = max(1, budget // 3)
            step = max(1, int(math.ceil(tri_count / float(keep_tri))))
            tri = mesh_xyz.reshape((-1, 3, 3))[::step][:keep_tri]
            mesh_xyz = np.ascontiguousarray(tri.reshape((-1, 3)), dtype=np.float32)
        # Native QC samples may exceed the vertex budget only in very sparse
        # rasters; keep their own bounded regular sample for picking/statistics.
        if int(native_xyz.shape[0]) > budget:
            native_xyz = cap_points_grid_order(native_xyz, budget).astype(np.float32, copy=False)
        progress_cb(100.0)
        return mesh_xyz, np.ascontiguousarray(native_xyz)


def window_to_points_chunked(
    src_path: str,
    window,
    max_points: int,
    progress_cb,
    preview_point_budget: int = 0,
    visual_fill_factor: int = 1,
):
    """Build native/QC points plus optional display-only native-Z scatter.

    Returns ``(render_xyz, native_xyz)``. The first array may contain
    deterministic XY-jittered display points whose Z is copied exactly from
    native raster cells; the second contains original raster samples only and
    is the sole source used by QC statistics and 3D measurement picking.
    """
    factor = int(max(1, min(int(visual_fill_factor or 1), 3)))
    with rasterio.open(src_path) as src:
        w = window.round_offsets().round_lengths()
        width = int(w.width)
        height = int(w.height)
        est_points = width * height
        if est_points > max_points:
            raise ValueError(f"Selection too large ({est_points:,} cells). Draw smaller box.")

        budget = int(preview_point_budget)
        stride = 1
        if budget and est_points > budget:
            stride = int(np.ceil(np.sqrt(est_points / float(budget))))
            stride = max(1, min(stride, 32))

        tile = int(TILE_SIZE)
        total_tiles = ((width + tile - 1) // tile) * ((height + tile - 1) // tile)
        native_parts = []
        done_tiles = 0
        first_pass_pct = 70.0 if factor > 1 else 100.0

        # Pass 1: preserve the exact native raster samples selected by the
        # adaptive-detail grid.  Synthetic display points never replace these.
        for row0 in range(0, height, tile):
            for col0 in range(0, width, tile):
                h = min(tile, height - row0)
                ww = min(tile, width - col0)
                sub = rasterio.windows.Window(
                    col_off=w.col_off + col0,
                    row_off=w.row_off + row0,
                    width=ww,
                    height=h,
                )
                data, valid, tf = _read_preview_grid(src, sub, stride, conservative_validity=False)
                pts = _grid_xyz_from_mask(data, valid, tf)
                if pts.size:
                    native_parts.append(pts)
                done_tiles += 1
                progress_cb(first_pass_pct * done_tiles / max(1, total_tiles))

        if native_parts:
            native_xyz = np.concatenate(native_parts, axis=0)
        else:
            native_xyz = np.empty((0, 3), dtype=np.float64)

        if budget > 0 and int(native_xyz.shape[0]) > budget:
            native_xyz = cap_points_grid_order(native_xyz, budget)
        native_xyz = np.ascontiguousarray(native_xyz, dtype=np.float64)

        # The existing render budget is a hard cap.  Native samples always win;
        # dense display scatter can consume only budget that remains unused.
        if factor <= 1 or budget <= 0 or int(native_xyz.shape[0]) >= budget:
            progress_cb(100.0)
            return native_xyz, native_xyz

        remaining = int(budget - native_xyz.shape[0])
        if remaining <= 0:
            progress_cb(100.0)
            return native_xyz, native_xyz

        synthetic_parts = []
        done_tiles = 0
        # Pass 2: generate display-only native-Z scatter. Per-tile quotas spread
        # the remaining budget across the AOI rather than exhausting it in the
        # first dense tile. No synthetic point receives an interpolated Z.
        for row0 in range(0, height, tile):
            for col0 in range(0, width, tile):
                if remaining <= 0:
                    break
                h = min(tile, height - row0)
                ww = min(tile, width - col0)
                sub = rasterio.windows.Window(
                    col_off=w.col_off + col0,
                    row_off=w.row_off + row0,
                    width=ww,
                    height=h,
                )
                tiles_left = max(1, total_tiles - done_tiles)
                tile_quota = min(remaining, max(2048, int(math.ceil(remaining / float(tiles_left)))))
                data, valid, tf = _read_preview_grid(src, sub, stride, conservative_validity=True)
                synth = _native_z_scatter_fill_points(data, valid, tf, factor=factor, max_points=tile_quota)
                if synth.size:
                    synthetic_parts.append(synth)
                    remaining -= int(synth.shape[0])
                done_tiles += 1
                progress_cb(70.0 + 30.0 * done_tiles / max(1, total_tiles))
            if remaining <= 0:
                break

        if synthetic_parts:
            synthetic_xyz = np.concatenate(synthetic_parts, axis=0)
            spare = max(0, budget - int(native_xyz.shape[0]))
            if int(synthetic_xyz.shape[0]) > spare:
                synthetic_xyz = cap_points_grid_order(synthetic_xyz, spare)
            render_xyz = np.concatenate([native_xyz, synthetic_xyz], axis=0)
        else:
            render_xyz = native_xyz

        if budget > 0 and int(render_xyz.shape[0]) > budget:
            # Should be unreachable, but keep the public plugin fail-safe.
            synth_budget = max(0, budget - int(native_xyz.shape[0]))
            if synth_budget <= 0:
                render_xyz = native_xyz
            else:
                render_xyz = np.concatenate([
                    native_xyz,
                    cap_points_grid_order(render_xyz[int(native_xyz.shape[0]):], synth_budget),
                ], axis=0)
        progress_cb(100.0)
        return np.ascontiguousarray(render_xyz), native_xyz


class BuildPointCloudTask(QgsTask):
    def __init__(self, title: str, src_path: str, window, max_points: int, max_render_points: int = 0, preview_point_budget: int = 0, visual_fill_factor: int = 1, render_style: str = "points"):
        super().__init__(title, QgsTask.CanCancel)
        self.src_path = src_path
        self.window = window
        self.max_points = int(max_points)
        self.max_render_points = int(max_render_points)
        self.preview_point_budget = int(preview_point_budget or max_render_points or PREVIEW_TARGET_POINTS)
        self.visual_fill_factor = int(max(1, visual_fill_factor))
        self.render_style = str(render_style or "points").lower().strip()
        self.xyz = None
        self.qc_xyz = None
        self.error_msg = None
        self._result_ok = False

    def run(self) -> bool:
        try:
            def cb(p):
                if self.isCanceled():
                    raise RuntimeError("Canceled")
                self.setProgress(p)

            if self.render_style == "surface":
                self.xyz, self.qc_xyz = window_to_surface_mesh(
                    src_path=self.src_path,
                    window=self.window,
                    max_points=self.max_points,
                    progress_cb=cb,
                    vertex_budget=int(self.preview_point_budget),
                )
            else:
                self.xyz, self.qc_xyz = window_to_points_chunked(
                    src_path=self.src_path,
                    window=self.window,
                    max_points=self.max_points,
                    progress_cb=cb,
                    preview_point_budget=int(self.preview_point_budget),
                    visual_fill_factor=int(self.visual_fill_factor),
                )

            if self.max_render_points > 0 and self.xyz is not None and self.xyz.size != 0:
                n = int(self.xyz.shape[0])
                if n > self.max_render_points:
                    if self.render_style == "surface":
                        tri = np.asarray(self.xyz, dtype=np.float32).reshape((-1, 3, 3))
                        keep_tri = max(1, int(self.max_render_points) // 3)
                        step = max(1, int(math.ceil(tri.shape[0] / float(keep_tri))))
                        self.xyz = np.ascontiguousarray(tri[::step][:keep_tri].reshape((-1, 3)))
                    else:
                        # Preserve native/QC samples first; trim display-only points.
                        native_n = int(self.qc_xyz.shape[0]) if self.qc_xyz is not None else 0
                        if native_n >= self.max_render_points:
                            self.qc_xyz = cap_points_grid_order(self.qc_xyz, self.max_render_points)
                            self.xyz = self.qc_xyz
                        else:
                            synth = self.xyz[native_n:]
                            synth = cap_points_grid_order(synth, self.max_render_points - native_n)
                            self.xyz = np.concatenate([self.qc_xyz, synth], axis=0)

            self._result_ok = True
            return True
        except Exception as e:
            msg = str(e)
            if self.isCanceled() or msg.lower().startswith("canceled"):
                self.error_msg = "Canceled"
            else:
                self.error_msg = msg
            self._result_ok = False
            return False

    def finished(self, result: bool) -> None:
        self._result_ok = bool(result)


class BuildMultiPointCloudTask(QgsTask):
    def __init__(
        self,
        title: str,
        src_paths: list,
        windows: list,
        max_points: int,
        max_render_points_each: int = 0,
        preview_point_budget_total: int = 0,
        visual_fill_factor: int = 1,
        render_style: str = "points",
    ):
        super().__init__(title, QgsTask.CanCancel)
        self.src_paths = list(src_paths or [])
        self.windows = list(windows or [])
        self.max_points = int(max_points)
        self.max_render_each = int(max_render_points_each)
        self.preview_point_budget_total = int(preview_point_budget_total or PREVIEW_TARGET_POINTS)
        self.visual_fill_factor = int(max(1, visual_fill_factor))
        self.render_style = str(render_style or "points").lower().strip()
        self.xyz_list = []
        self.qc_xyz_list = []
        self.error_msg = None

    def run(self) -> bool:
        try:
            nsrc = max(1, min(len(self.src_paths), len(self.windows)))
            out = []
            qc_out = []

            for i in range(nsrc):
                if self.isCanceled():
                    raise RuntimeError("Canceled")

                src_path = self.src_paths[i]
                window = self.windows[i]

                def cb_inner(pct):
                    if self.isCanceled():
                        raise RuntimeError("Canceled")
                    self.setProgress((i * 100.0 / nsrc) + (float(pct) / nsrc))

                per_layer_budget = int(max(25_000, int(self.preview_point_budget_total) // nsrc))
                if self.render_style == "surface":
                    xyz, qc_xyz = window_to_surface_mesh(
                        src_path=src_path,
                        window=window,
                        max_points=self.max_points,
                        progress_cb=cb_inner,
                        vertex_budget=per_layer_budget,
                    )
                else:
                    xyz, qc_xyz = window_to_points_chunked(
                        src_path=src_path,
                        window=window,
                        max_points=self.max_points,
                        progress_cb=cb_inner,
                        preview_point_budget=per_layer_budget,
                        visual_fill_factor=int(self.visual_fill_factor),
                    )

                if self.max_render_each > 0 and xyz is not None and xyz.size != 0:
                    n = int(xyz.shape[0])
                    if n > self.max_render_each:
                        if self.render_style == "surface":
                            tri = np.asarray(xyz, dtype=np.float32).reshape((-1, 3, 3))
                            keep_tri = max(1, int(self.max_render_each) // 3)
                            step = max(1, int(math.ceil(tri.shape[0] / float(keep_tri))))
                            xyz = np.ascontiguousarray(tri[::step][:keep_tri].reshape((-1, 3)))
                        else:
                            native_n = int(qc_xyz.shape[0]) if qc_xyz is not None else 0
                            if native_n >= self.max_render_each:
                                qc_xyz = cap_points_grid_order(qc_xyz, self.max_render_each)
                                xyz = qc_xyz
                            else:
                                synth = cap_points_grid_order(xyz[native_n:], self.max_render_each - native_n)
                                xyz = np.concatenate([qc_xyz, synth], axis=0)

                out.append(xyz)
                qc_out.append(qc_xyz)

            self.xyz_list = out
            self.qc_xyz_list = qc_out
            return True

        except Exception as e:
            msg = str(e)
            if self.isCanceled() or msg.lower().startswith("canceled"):
                self.error_msg = "Canceled"
            else:
                self.error_msg = msg
            return False


class BuildCrossSectionTask(QgsTask):
    def __init__(
        self,
        title: str,
        src_path: str,
        p1,
        p2,
        width: float,
        dist_samples: int = 300,
        width_samples: int = 20,
    ):
        super().__init__(title, QgsTask.CanCancel)
        self.src_path = src_path
        # Copy QGIS point values to primitives before the task enters a worker thread.
        self.p1 = (float(p1.x()), float(p1.y()))
        self.p2 = (float(p2.x()), float(p2.y()))
        self.width = float(width)
        self.dist_samples = max(2, int(dist_samples))
        self.width_samples = max(1, int(width_samples))
        self.xyz = None
        self.error_msg = None

    def run(self) -> bool:
        try:
            p1x, p1y = self.p1
            p2x, p2y = self.p2
            dx = p2x - p1x
            dy = p2y - p1y
            L = float(np.hypot(dx, dy))
            if L == 0:
                self.xyz = np.empty((0, 3), dtype=float)
                return True

            ux, uy = dx / L, dy / L
            px, py = -uy, ux
            half = self.width / 2.0

            with rasterio.open(self.src_path) as src:
                nodata = src.nodata
                distances = np.linspace(0, L, self.dist_samples)
                if self.width_samples <= 1:
                    offsets = np.array([0.0], dtype=float)
                else:
                    offsets = np.linspace(-half, half, self.width_samples)

                pts = []
                total = max(1, len(distances))
                for i, d in enumerate(distances):
                    if self.isCanceled():
                        raise RuntimeError("Canceled")

                    base_x = p1x + ux * d
                    base_y = p1y + uy * d

                    coords = [(base_x + px * w, base_y + py * w) for w in offsets]
                    try:
                        vals = list(src.sample(coords, masked=True))
                    except Exception as exc:
                        raise RuntimeError(f"Raster sampling failed: {exc}") from exc

                    for (x, y), v in zip(coords, vals):
                        if np.ma.is_masked(v[0]):
                            continue
                        elev = float(v[0])
                        if not np.isfinite(elev):
                            continue
                        if nodata is not None and np.isfinite(nodata) and np.isclose(elev, float(nodata), rtol=0.0, atol=1e-6):
                            continue
                        pts.append((x, y, elev))

                    self.setProgress(100.0 * (i + 1) / total)

            if len(pts) == 0:
                self.xyz = np.empty((0, 3), dtype=float)
            else:
                self.xyz = np.array(pts, dtype=float)

            return True
        except Exception as e:
            msg = str(e)
            if self.isCanceled() or msg.lower().startswith("canceled"):
                self.error_msg = "Canceled"
            else:
                self.error_msg = msg
            return False


class BuildMultiCrossSectionTask(QgsTask):
    """Build the same cross-section through multiple DEM/DTM surfaces."""

    def __init__(self, title: str, src_paths: list, p1, p2, width: float, sample_specs: list, surface_mode: bool = False):
        super().__init__(title, QgsTask.CanCancel)
        self.src_paths = list(src_paths or [])
        self.p1 = (float(p1.x()), float(p1.y()))
        self.p2 = (float(p2.x()), float(p2.y()))
        self.width = float(width)
        self.sample_specs = list(sample_specs or [])
        self.surface_mode = bool(surface_mode)
        self.xyz_list = []
        self.qc_xyz_list = []
        self.error_msg = None

    def run(self) -> bool:
        try:
            p1x, p1y = self.p1
            p2x, p2y = self.p2
            dx = p2x - p1x
            dy = p2y - p1y
            length = float(np.hypot(dx, dy))
            if length <= 0:
                self.xyz_list = [np.empty((0, 3), dtype=float) for _ in self.src_paths]
                return True

            ux, uy = dx / length, dy / length
            px, py = -uy, ux
            half = self.width / 2.0
            nsrc = max(1, len(self.src_paths))
            out = []
            qc_out = []

            for layer_i, src_path in enumerate(self.src_paths):
                if self.isCanceled():
                    raise RuntimeError("Canceled")
                dist_samples, width_samples = self.sample_specs[layer_i]
                dist_samples = max(2, int(dist_samples))
                width_samples = max(1, int(width_samples))
                distances = np.linspace(0, length, dist_samples)
                offsets = np.array([0.0], dtype=float) if width_samples <= 1 else np.linspace(-half, half, width_samples)
                grid = np.full((dist_samples, width_samples, 3), np.nan, dtype=np.float32)
                valid_grid = np.zeros((dist_samples, width_samples), dtype=bool)
                qc_pts = []

                with rasterio.open(src_path) as src:
                    nodata = src.nodata
                    total = max(1, len(distances))
                    for i, d in enumerate(distances):
                        if self.isCanceled():
                            raise RuntimeError("Canceled")
                        base_x = p1x + ux * d
                        base_y = p1y + uy * d
                        coords = [(base_x + px * w, base_y + py * w) for w in offsets]
                        vals = list(src.sample(coords, masked=True))
                        for j, ((x, y), v) in enumerate(zip(coords, vals)):
                            if np.ma.is_masked(v[0]):
                                continue
                            elev = float(v[0])
                            if not np.isfinite(elev):
                                continue
                            if nodata is not None and np.isfinite(nodata) and np.isclose(elev, float(nodata), rtol=0.0, atol=1e-6):
                                continue
                            grid[i, j] = (float(x), float(y), elev)
                            valid_grid[i, j] = True
                            qc_pts.append((float(x), float(y), elev))
                        local_pct = 100.0 * (i + 1) / total
                        self.setProgress((layer_i * 100.0 / nsrc) + (local_pct / nsrc))

                qc_arr = np.asarray(qc_pts, dtype=np.float32) if qc_pts else np.empty((0, 3), dtype=np.float32)
                if self.surface_mode and width_samples >= 2 and dist_samples >= 2:
                    q = valid_grid[:-1, :-1] & valid_grid[1:, :-1] & valid_grid[1:, 1:] & valid_grid[:-1, 1:]
                    rr, cc = np.nonzero(q)
                    if rr.size:
                        p00 = grid[rr, cc]
                        p10 = grid[rr + 1, cc]
                        p11 = grid[rr + 1, cc + 1]
                        p01 = grid[rr, cc + 1]
                        mesh = np.empty((rr.size * 6, 3), dtype=np.float32)
                        mesh[0::6], mesh[1::6], mesh[2::6] = p00, p10, p11
                        mesh[3::6], mesh[4::6], mesh[5::6] = p00, p11, p01
                        render_arr = np.ascontiguousarray(mesh)
                    else:
                        render_arr = np.empty((0, 3), dtype=np.float32)
                else:
                    render_arr = qc_arr
                out.append(render_arr)
                qc_out.append(qc_arr)

            self.xyz_list = out
            self.qc_xyz_list = qc_out
            return True
        except Exception as e:
            msg = str(e)
            self.error_msg = "Canceled" if self.isCanceled() or msg.lower().startswith("canceled") else msg
            return False




def cap_points_with_colors(xyz: np.ndarray, colors: np.ndarray, max_n: int):
    max_n = int(max_n)
    if max_n <= 0 or xyz is None or xyz.size == 0:
        return xyz, colors
    n = int(xyz.shape[0])
    if n <= max_n:
        return xyz, colors
    idx = np.random.choice(n, size=max_n, replace=False)
    return xyz[idx], colors[idx]


def solid_rgba(n: int, r: float, g: float, b: float, a: float) -> np.ndarray:
    n = int(max(0, n))
    rgba = np.empty((n, 4), dtype=np.float32)
    rgba[:, 0] = float(r)
    rgba[:, 1] = float(g)
    rgba[:, 2] = float(b)
    rgba[:, 3] = float(a)
    return rgba


def selected_raster_layers():
    layers = []
    try:
        layers = iface.layerTreeView().selectedLayers() or []
    except Exception:
        layers = []

    return [lyr for lyr in layers if is_raster_layer(lyr)]


def is_supported_surface_layer(layer) -> bool:
    """Return True only for file/GDAL-backed elevation rasters.

    WMS/WMTS/XYZ rasters are allowed as imagery sources, but they are not
    elevation surfaces and must never be passed to the GDAL DEM reader.
    """
    if not is_raster_layer(layer):
        return False
    try:
        return str(layer.providerType() or "").lower() == "gdal"
    except Exception:
        return False


def selected_surface_layers():
    """Return selected layers that can actually be sampled as DEM/DTM/DSM."""
    return [lyr for lyr in selected_raster_layers() if is_supported_surface_layer(lyr)]

# ---- Map tools --------------------------------------------------------------

class Rectangle3DTool(QgsMapTool):
    """
    Drag a rectangle on the map; when finished, extract DEM and open 3D view.
    """

    def __init__(self, canvas, dialog: QDialog):
        super().__init__(canvas)
        self.canvas = canvas
        self.dialog = dialog
        self.enabled = False
        self._active_task = None
        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber.setColor(Qt.red)
        self.rubber.setFillColor(Qt.transparent)
        self.rubber.setWidth(2)
        self.start_pt = None

    def enable(self):
        self.enabled = True
        self.start_pt = None
        self.rubber.hide()

    def clear(self):
        self.enabled = False
        self.rubber.hide()

    def cancel_active_task(self) -> bool:
        if self._active_task is None:
            return False
        try:
            self._active_task.cancel()
        except Exception:
            pass
        return True

    def canvasPressEvent(self, event):
        if not self.enabled:
            return

        self.start_pt = self.toMapCoordinates(event.pos())
        self.rubber.reset(QgsWkbTypes.PolygonGeometry)
        self.rubber.addPoint(self.start_pt, False)
        self.rubber.show()

    def canvasMoveEvent(self, event):
        if not self.enabled or self.start_pt is None:
            return

        cur = self.toMapCoordinates(event.pos())
        pts = [
            QgsPointXY(self.start_pt.x(), self.start_pt.y()),
            QgsPointXY(cur.x(), self.start_pt.y()),
            QgsPointXY(cur.x(), cur.y()),
            QgsPointXY(self.start_pt.x(), cur.y()),
            QgsPointXY(self.start_pt.x(), self.start_pt.y()),
        ]
        geom = QgsGeometry.fromPolygonXY([pts])
        self.rubber.setToGeometry(geom, None)

    def canvasReleaseEvent(self, event):
        if not self.enabled or self.start_pt is None:
            return

        end_pt = self.toMapCoordinates(event.pos())
        extent = QgsRectangle(self.start_pt, end_pt)
        self.enabled = False
        self.start_pt = None
        self.handle_extent(extent)

    def handle_extent(
        self,
        extent: QgsRectangle,
        remember_extent: bool = True,
        preserve_camera: bool = False,
    ):
        if rasterio is None:
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                dependency_error_text(),
                level=Qgis.Critical,
                duration=7,
            )
            return

        rasters = self.dialog.get_surface_layers(fallback=True)
        if not rasters:
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                "Add at least one DEM/DTM surface to the viewer first.",
                level=Qgis.Warning,
                duration=4,
            )
            return

        try:
            ensure_same_raster_crs(rasters)
        except Exception as exc:
            iface.messageBar().pushMessage(
                "Terrain Viewer", str(exc), level=Qgis.Warning, duration=7
            )
            return

        if remember_extent:
            try:
                self.dialog.remember_box_extent(extent)
            except Exception as exc:
                log_exception("Could not remember 3D box extent", exc, Qgis.Info)

        if self._active_task is not None:
            self.cancel_active_task()

        windows = []
        used_paths = []
        used_layers = []
        total_est_points = 0
        total_area_units2 = 0.0
        skipped = 0
        resolutions = []

        for lyr in rasters:
            try:
                pth = raster_source_path(lyr)
                lyr_extent = canvas_to_layer_extent(extent, lyr)

                xmin0 = min(lyr_extent.xMinimum(), lyr_extent.xMaximum())
                xmax0 = max(lyr_extent.xMinimum(), lyr_extent.xMaximum())
                ymin0 = min(lyr_extent.yMinimum(), lyr_extent.yMaximum())
                ymax0 = max(lyr_extent.yMinimum(), lyr_extent.yMaximum())

                with rasterio.open(pth) as src:
                    resx, resy = src.res
                    resolutions.append((abs(float(resx)), abs(float(resy))))

                    xmin = max(float(xmin0), float(src.bounds.left))
                    xmax = min(float(xmax0), float(src.bounds.right))
                    ymin = max(float(ymin0), float(src.bounds.bottom))
                    ymax = min(float(ymax0), float(src.bounds.top))

                    if xmin >= xmax or ymin >= ymax:
                        skipped += 1
                        continue

                    w = src.window(xmin, ymin, xmax, ymax).round_offsets().round_lengths()
                    est = int(w.width) * int(w.height)
                    if est <= 0:
                        skipped += 1
                        continue
                    if est > int(HARD_READ_CAP_CELLS):
                        raise ValueError(
                            f"Selection is too large for '{lyr.name()}' ({est:,} cells). "
                            "Draw a smaller box."
                        )

                    windows.append(w)
                    used_paths.append(pth)
                    used_layers.append(lyr)
                    total_est_points += int(est)
                    total_area_units2 += float(est) * abs(float(resx) * float(resy))

                    if total_est_points > int(HARD_READ_CAP_CELLS):
                        raise ValueError(
                            f"Combined selection is too large ({total_est_points:,} cells). "
                            f"Hard cap: {int(HARD_READ_CAP_CELLS):,}. Draw a smaller box."
                        )

            except Exception as exc:
                log_exception("Raster preparation failed", exc, Qgis.Critical)
                iface.messageBar().pushMessage(
                    "Terrain Viewer",
                    str(exc) or "Could not prepare the selected raster.",
                    level=Qgis.Critical,
                    duration=7,
                )
                return

        if not used_paths:
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                "Selection is outside the selected raster bounds.",
                level=Qgis.Warning,
                duration=5,
            )
            return

        unit_label = crs_unit_label(used_layers[0]) if used_layers else "map units"
        area_str = ""
        try:
            area_str = f" (~{total_area_units2:,.0f} {unit_label}²"
            if resolutions:
                rx, ry = resolutions[0]
                if all(abs(a - rx) < 1e-9 and abs(b - ry) < 1e-9 for a, b in resolutions):
                    area_str += f" @ {rx:.4g} x {ry:.4g} {unit_label}"
                else:
                    area_str += "; mixed raster resolutions"
            area_str += ")"
        except Exception:
            area_str = ""

        max_render_points = int(self.dialog.detail_point_budget())
        detail_name = self.dialog.detail_level_name()
        render_style = self.dialog.render_style_key()
        visual_fill_factor = 1 if render_style == "surface" else int(self.dialog.visual_density_factor())
        visual_density_name = "Surface mesh" if render_style == "surface" else self.dialog.visual_density_name()
        preview_est = min(int(total_est_points), int(max_render_points))
        estimated_mb = (preview_est * int(DETAIL_EST_BYTES_PER_POINT)) / (1024.0 * 1024.0)

        if total_est_points > int(max_render_points):
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                f"{detail_name} detail: {total_est_points:,} source cells{area_str} -> "
                f"up to {max_render_points:,} structured preview points "
                f"(~{estimated_mb:,.0f} MB estimated working set; {visual_density_name}).",
                level=Qgis.Info,
                duration=6,
            )
        elif total_est_points > int(SOFT_WARN_CELLS):
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                f"Large full-resolution selection ({total_est_points:,} cells){area_str}. "
                f"Estimated working set ~{estimated_mb:,.0f} MB.",
                level=Qgis.Info,
                duration=6,
            )

        per_layer_render = max(1, max_render_points // max(1, len(used_paths)))

        self.dialog.progress_start("Building raster surface" if render_style == "surface" else "Building point cloud")

        if skipped:
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                f"Skipped {int(skipped)} selected raster(s) with no overlap.",
                level=Qgis.Info,
                duration=4,
            )

        if len(used_paths) == 1:
            task = BuildPointCloudTask(
                title="Build 3D DEM point cloud",
                src_path=used_paths[0],
                window=windows[0],
                max_points=int(HARD_READ_CAP_CELLS),
                max_render_points=max_render_points,
                preview_point_budget=max_render_points,
                visual_fill_factor=visual_fill_factor,
                render_style=render_style,
            )
        else:
            task = BuildMultiPointCloudTask(
                title="Build 3D point clouds (multi raster)",
                src_paths=used_paths,
                windows=windows,
                max_points=int(HARD_READ_CAP_CELLS),
                max_render_points_each=int(per_layer_render),
                preview_point_budget_total=max_render_points,
                visual_fill_factor=visual_fill_factor,
                render_style=render_style,
            )
        self._active_task = task

        def _progress_changed(pct):
            if task is self._active_task:
                self.dialog.progress_set(pct)

        def _finished(result_ok: bool):
            if task is not self._active_task:
                return
            try:
                if not result_ok:
                    msg = task.error_msg or "Task failed"
                    level = Qgis.Info if msg == "Canceled" else Qgis.Critical
                    iface.messageBar().pushMessage("Terrain Viewer", msg, level=level, duration=5)
                    return

                if isinstance(task, BuildPointCloudTask):
                    xyz_list = [task.xyz]
                    qc_xyz_list = [task.qc_xyz]
                else:
                    xyz_list = getattr(task, "xyz_list", None) or []
                    qc_xyz_list = getattr(task, "qc_xyz_list", None) or []

                draw_layers = []
                for idx, xyz in enumerate(xyz_list):
                    if xyz is None or xyz.size == 0 or idx >= len(used_layers):
                        continue
                    lyr = used_layers[idx]
                    rgb = self.dialog.surface_rgb_for_layer(str(lyr.id()), idx)
                    n = int(xyz.shape[0])
                    qc_xyz = qc_xyz_list[idx] if idx < len(qc_xyz_list) else xyz
                    qc_xyz = np.asarray(qc_xyz) if qc_xyz is not None else np.empty((0, 3), dtype=float)
                    native_n = int(qc_xyz.shape[0]) if qc_xyz.ndim == 2 else 0
                    draw_layers.append({
                        "xyz": np.asarray(xyz),
                        "qc_xyz": qc_xyz,
                        "pick_xyz": qc_xyz,
                        "native_count": native_n,
                        "synthetic_count": 0 if render_style == "surface" else max(0, n - native_n),
                        "visual_fill_factor": visual_fill_factor,
                        "render_style": render_style,
                        "rgba": solid_rgba(n, rgb[0], rgb[1], rgb[2], 0.85),
                        "base_rgb": rgb,
                        "layer_id": str(lyr.id()),
                        "name": str(lyr.name()),
                        "source_path": used_paths[idx],
                        "visible": True,
                    })

                if not draw_layers:
                    iface.messageBar().pushMessage(
                        "Terrain Viewer",
                        "No valid DEM cells were found in the selected rasters.",
                        level=Qgis.Warning,
                        duration=5,
                    )
                    return

                self.dialog._last_preview_crs = used_layers[0].crs() if used_layers else None
                self.dialog._last_preview_unit = crs_unit_label(used_layers[0]) if used_layers else "map units"
                self.dialog._preview_mode = "box"
                self.dialog._last_cs_p1 = None
                self.dialog._last_cs_p2 = None
                self.dialog._last_cs_width = None
                self.dialog._last_cs_canvas_p1 = None
                self.dialog._last_cs_canvas_p2 = None
                if preserve_camera:
                    preset = self.dialog._last_preset
                    az = self.dialog._last_az
                    el = self.dialog._last_el
                else:
                    preset, az, el = "top", None, None
                self.dialog.set_preview_cloud_layers(
                    layers=draw_layers,
                    preset=preset,
                    az=az,
                    el=el,
                    reset_camera=not bool(preserve_camera),
                )
            finally:
                self._active_task = None
                self.dialog.progress_end()

        task.progressChanged.connect(_progress_changed)
        task.taskCompleted.connect(lambda: _finished(True))
        task.taskTerminated.connect(lambda: _finished(False))
        QgsApplication.taskManager().addTask(task)


class CrossSectionTool(QgsMapToolEmitPoint):
    """Capture a two-point transect and keep the selected surface set fixed."""

    def __init__(self, canvas, width: float, layers, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.width = float(width)
        self.layers = list(layers or [])
        self.callback = callback
        self.points = []
        self.rubber = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.rubber.setColor(Qt.yellow)
        self.rubber.setWidth(4)
        self.rubber.hide()

    def clear(self):
        self.points = []
        try:
            self.rubber.reset(QgsWkbTypes.LineGeometry)
            self.rubber.hide()
        except Exception:
            pass

    def canvasMoveEvent(self, event):
        if len(self.points) != 1:
            return
        cur = self.toMapCoordinates(event.pos())
        try:
            geom = QgsGeometry.fromPolylineXY([self.points[0], cur])
            self.rubber.setToGeometry(geom, None)
            self.rubber.show()
        except Exception:
            pass

    def canvasReleaseEvent(self, event):
        pt = self.toMapCoordinates(event.pos())
        self.points.append(QgsPointXY(pt))

        if len(self.points) == 1:
            iface.mainWindow().statusBar().showMessage(
                "Cross-section: move the mouse to preview the transect, then click the second point."
            )
            return

        if len(self.points) == 2:
            p1, p2 = self.points
            try:
                geom = QgsGeometry.fromPolylineXY([p1, p2])
                self.rubber.setToGeometry(geom, None)
                self.rubber.show()
            except Exception:
                pass
            self.points = []
            try:
                self.callback(p1, p2, self.width, self.layers)
            finally:
                self.canvas.unsetMapTool(self)
                iface.mainWindow().statusBar().clearMessage()




# ---- Main dialog ------------------------------------------------------------

class TerrainViewerDialog(QWidget):
    """
    Main dialog with controls for:
      - 3D box view
      - 3D cross-section
    """

    def __init__(self, parent=None):
        super().__init__(parent or iface.mainWindow())

        self.setObjectName("TerrainViewerDialog_Instance")
        self.setWindowTitle("Terrain Viewer v1.3.0")
        self.resize(420, 860)
        self.setMinimumSize(290, 520)
        self.setStyleSheet(
            "QGroupBox { font-weight: bold; margin-top: 6px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 6px; padding: 0 2px; } "
            "QPushButton { min-height: 22px; padding: 1px 5px; } "
            "QDoubleSpinBox, QComboBox { min-height: 21px; }"
        )

        self.rect_tool = Rectangle3DTool(iface.mapCanvas(), self)
        self.cs_tool = None
        self.measure_tool = None
        self._active_cs_task = None
        self._dependencies_ok = rasterio is not None
        self._color_controls_available = True
        self._surface_layer_ids = []
        self._surface_color_counter = 0
        self._vector_layer_ids = []
        self._vector_color_counter = 0
        self._last_preview_crs = None
        self._last_preview_unit = "map units"
        self._measure_points_3d = []
        self._measure_pick_meta = []
        self._measure_active = False
        self._cs_width_user_set = False
        self._preview_mode = "none"
        self._last_cs_p1 = None
        self._last_cs_p2 = None
        self._last_cs_width = None
        self._last_cs_canvas_p1 = None
        self._last_cs_canvas_p2 = None

        # Last user-drawn 3D box. Detail/density changes rebuild this same AOI
        # automatically so before/after settings are directly comparable.
        self._last_box_extent = None
        self._last_box_canvas_crs = None
        self._pending_box_rebuild_reason = ""

        # --- Aerial / imagery RGB cache ------------------------------------
        # Imagery is rendered once for the current 3D footprint using the
        # top visible non-surface raster/XYZ/WMTS layer in QGIS, then sampled
        # vectorially at the DEM/DSM point XY locations. No per-point network
        # requests and no additional Python packages are required.
        self._imagery_cache_key = None
        self._imagery_cache_rgba = None
        self._imagery_cache_extent = None
        self._imagery_cache_source_id = None
        self._imagery_cache_source_name = ""
        self._imagery_missing_warned = False

        # Relief caches are per raster layer/source and current preview footprint.
        # They contain a bounded hillshade-like scalar grid, never modified Z.
        self._relief_cache = {}

        # --- State for realtime display controls ---
        self._last_xyz_full = None
        self._last_cloud_layers = None
        self._last_preset = "iso"
        self._last_az = None
        self._last_el = None
        self._last_colors = None
        self._last_color_key = None
        self._preview_pending_redraw = False
        self._applied_color_key = None

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._apply_display_update)
        self._pending_reset_cam = False

        # Extraction-changing controls are debounced separately from cheap
        # realtime display controls. This avoids launching multiple heavy tasks
        # if the user clicks through presets quickly.
        self._box_rebuild_timer = QTimer(self)
        self._box_rebuild_timer.setSingleShot(True)
        self._box_rebuild_timer.timeout.connect(self._rebuild_last_box)

        controls_layout = QVBoxLayout()
        controls_layout.setContentsMargins(4, 4, 4, 4)
        controls_layout.setSpacing(3)

        self.dependency_label = QLabel("")
        self.dependency_label.setWordWrap(True)
        self.dependency_label.setVisible(False)
        if not self._dependencies_ok:
            self.dependency_label.setText("<b>Viewer dependency problem:</b><br>" + dependency_error_text())
            self.dependency_label.setVisible(True)
        controls_layout.addWidget(self.dependency_label)

        # --- Data manager: raster surfaces + vector overlays ----------------
        self.data_group = QGroupBox("Layers")
        data_layout = QVBoxLayout(self.data_group)
        data_layout.setContentsMargins(5, 7, 5, 5)
        data_layout.setSpacing(3)
        self.data_tabs = QTabWidget()

        surface_page = QWidget()
        surface_layout = QVBoxLayout(surface_page)
        surface_layout.setContentsMargins(2, 2, 2, 2)
        surface_layout.setSpacing(2)
        self.surface_list = QListWidget()
        self.surface_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.surface_list.setMinimumHeight(72)
        self.surface_list.setMaximumHeight(115)
        self.surface_list.setToolTip("Check/uncheck a rendered raster to show/hide it without rebuilding the preview.")
        surface_layout.addWidget(self.surface_list)
        sbtn = QGridLayout()
        sbtn.setHorizontalSpacing(2)
        sbtn.setVerticalSpacing(2)
        self.add_selected_btn = QPushButton("Add selected")
        self.add_files_btn = QPushButton("Add raster files...")
        self.remove_surface_btn = QPushButton("Remove")
        self.clear_surfaces_btn = QPushButton("Clear all")
        sbtn.addWidget(self.add_selected_btn, 0, 0)
        sbtn.addWidget(self.add_files_btn, 0, 1)
        sbtn.addWidget(self.remove_surface_btn, 1, 0)
        sbtn.addWidget(self.clear_surfaces_btn, 1, 1)
        surface_layout.addLayout(sbtn)
        self.data_tabs.addTab(surface_page, "Raster surfaces")

        vector_page = QWidget()
        vector_layout = QVBoxLayout(vector_page)
        vector_layout.setContentsMargins(2, 2, 2, 2)
        vector_layout.setSpacing(2)
        self.vector_list = QListWidget()
        self.vector_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.vector_list.setMinimumHeight(72)
        self.vector_list.setMaximumHeight(115)
        self.vector_list.setToolTip("3D line overlays: LineStringZ/MultiLineStringZ, or 2D lines with Z/ELEV/ELEVATION/CONTOUR/HEIGHT attribute.")
        vector_layout.addWidget(self.vector_list)
        vbtn = QGridLayout()
        vbtn.setHorizontalSpacing(2)
        vbtn.setVerticalSpacing(2)
        self.add_vector_selected_btn = QPushButton("Add selected")
        self.add_vector_files_btn = QPushButton("Add vector files...")
        self.remove_vector_btn = QPushButton("Remove")
        self.clear_vectors_btn = QPushButton("Clear all")
        vbtn.addWidget(self.add_vector_selected_btn, 0, 0)
        vbtn.addWidget(self.add_vector_files_btn, 0, 1)
        vbtn.addWidget(self.remove_vector_btn, 1, 0)
        vbtn.addWidget(self.clear_vectors_btn, 1, 1)
        vector_layout.addLayout(vbtn)
        self.data_tabs.addTab(vector_page, "Vector overlays")
        data_layout.addWidget(self.data_tabs)
        controls_layout.addWidget(self.data_group)

        self.add_selected_btn.clicked.connect(self.on_add_selected_surfaces)
        self.add_files_btn.clicked.connect(self.on_add_surface_files)
        self.remove_surface_btn.clicked.connect(self.on_remove_surfaces)
        self.clear_surfaces_btn.clicked.connect(self.on_clear_surfaces)
        self.surface_list.itemChanged.connect(self.on_surface_item_changed)
        self.add_vector_selected_btn.clicked.connect(self.on_add_selected_vectors)
        self.add_vector_files_btn.clicked.connect(self.on_add_vector_files)
        self.remove_vector_btn.clicked.connect(self.on_remove_vectors)
        self.clear_vectors_btn.clicked.connect(self.on_clear_vectors)
        self.vector_list.itemChanged.connect(self.on_vector_item_changed)

        # --- Compact tools ---------------------------------------------------
        self.tools_group = QGroupBox("Tools")
        tools_grid = QGridLayout(self.tools_group)
        tools_grid.setContentsMargins(5, 7, 5, 5)
        tools_grid.setHorizontalSpacing(3)
        tools_grid.setVerticalSpacing(3)

        self.draw_btn = QPushButton("Draw 3D Area")
        self.cs_btn = QPushButton("Draw Cross-Section")
        self.measure_btn = QPushButton("Measure in 3D")
        self.clear_btn = QPushButton("Clear View")
        self.draw_btn.setToolTip("Draw an area on the QGIS map and load it into the 3D viewer.")
        self.cs_btn.setToolTip("Draw a two-point cross-section through the checked raster surfaces and vector overlays.")
        self.measure_btn.setToolTip("Pick two native rendered raster samples in the 3D view to measure distance, elevation difference, and slope.")
        self.clear_btn.setToolTip("Clear the current 3D preview, cross-section, measurements, and remembered area.")
        self.draw_btn.clicked.connect(self.on_draw)
        self.cs_btn.clicked.connect(self.on_cs)
        self.measure_btn.clicked.connect(self.on_measure)
        self.clear_btn.clicked.connect(self.on_clear)
        tools_grid.addWidget(self.draw_btn, 0, 0)
        tools_grid.addWidget(self.cs_btn, 0, 1)
        tools_grid.addWidget(self.measure_btn, 1, 0)
        tools_grid.addWidget(self.clear_btn, 1, 1)

        tools_grid.addWidget(QLabel("Cross-section width"), 2, 0)
        self.cs_width_spin = QDoubleSpinBox()
        self.cs_width_spin.setDecimals(3)
        self.cs_width_spin.setRange(0.001, 1e9)
        self.cs_width_spin.setValue(5.0)
        self.cs_width_spin.setToolTip("Persistent sampling corridor width for every cross-section.")
        self.cs_width_spin.valueChanged.connect(self._on_cs_width_user_change)
        tools_grid.addWidget(self.cs_width_spin, 2, 1)
        self.measure_result_label = QLabel("Measure: click Measure in 3D, then pick two rendered points.")
        self.measure_result_label.setWordWrap(True)
        self.measure_result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tools_grid.addWidget(self.measure_result_label, 3, 0, 1, 2)
        controls_layout.addWidget(self.tools_group)

        # --- Compact display/QC controls ------------------------------------
        self.display_group = QGroupBox("3D Display")
        form = QGridLayout(self.display_group)
        form.setContentsMargins(5, 7, 5, 5)
        form.setHorizontalSpacing(4)
        form.setVerticalSpacing(2)
        form.setColumnStretch(1, 1)

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.5, 20.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setValue(2.5)
        self.size_spin.setToolTip("Base point size in screen pixels. Auto point size can adjust this slightly with zoom.")
        self.zex_spin = QDoubleSpinBox()
        self.zex_spin.setRange(0.1, 20.0)
        self.zex_spin.setSingleStep(0.1)
        self.zex_spin.setValue(1.0)
        self.zex_spin.setToolTip("Display-only vertical exaggeration. 1.0 = true Z scale.")
        form.addWidget(QLabel("Point size (px)"), 0, 0)
        form.addWidget(self.size_spin, 0, 1)
        form.addWidget(QLabel("Vertical scale"), 1, 0)
        form.addWidget(self.zex_spin, 1, 1)

        self.render_style_combo = QComboBox()
        self.render_style_combo.addItems(["Points", "Surface"])
        self.render_style_combo.setCurrentText("Points")
        self.render_style_combo.setToolTip(
            "Points renders raster cells as the existing QC point view. Surface connects "
            "valid native raster cells into a continuous triangle mesh; NoData stays open. "
            "Measurements and QC statistics still use native raster samples."
        )
        form.addWidget(QLabel("View style"), 2, 0)
        form.addWidget(self.render_style_combo, 2, 1)

        self.detail_combo = QComboBox()
        self.detail_combo.addItems(["Auto", "High", "Ultra", "Maximum"])
        self.detail_combo.setCurrentText("Auto")
        self.detail_combo.setToolTip(
            "Point budget for the 3D extraction. Auto ~2M, High ~3.5M, "
            "Ultra ~6M, Maximum ~10M total points across visible surfaces. "
            "Small selections remain full raster resolution. After a 3D box has "
            "been drawn, changing this setting rebuilds that same box automatically."
        )
        form.addWidget(QLabel("Detail level"), 3, 0)
        form.addWidget(self.detail_combo, 3, 1)

        self.density_combo = QComboBox()
        self.density_combo.addItems(["Native", "Dense 2×", "Dense 3×"])
        self.density_combo.setCurrentText("Native")
        self.density_combo.setToolTip(
            "Display-only densification. Dense 2×/3× adds deterministic randomized XY "
            "samples around native raster cells while keeping each synthetic point at the "
            "exact native Z. This avoids mid-height ghost points at roofs, powerlines, and "
            "vegetation. Synthetic points never affect QC/measurement and stay inside the "
            "selected 3D Detail budget."
        )
        form.addWidget(QLabel("Point density"), 4, 0)
        form.addWidget(self.density_combo, 4, 1)

        self.cmap_combo = QComboBox()
        self.cmap_combo.addItems(["Surface + height shading", "Surface colors", "Height (RGB)", "Grayscale"])
        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.10, 1.00)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setDecimals(2)
        self.opacity_spin.setValue(0.85)
        form.addWidget(QLabel("Coloring"), 5, 0)
        form.addWidget(self.cmap_combo, 5, 1)
        self.opacity_spin.setToolTip("Display opacity for raster surfaces. Useful when comparing overlapping surfaces.")
        form.addWidget(QLabel("Opacity"), 6, 0)
        form.addWidget(self.opacity_spin, 6, 1)

        self.imagery_chk = QCheckBox("Color from imagery")
        self.imagery_chk.setChecked(False)
        self.imagery_chk.setToolTip("When enabled, colors DEM/DSM points from the explicitly selected QGIS raster/basemap layer.")
        self.imagery_combo = QComboBox()
        self.imagery_combo.setEnabled(False)
        self.imagery_combo.setToolTip("Choose the exact raster, XYZ, WMS or WMTS layer to use for RGB coloring.")
        form.addWidget(self.imagery_chk, 7, 0)
        form.addWidget(self.imagery_combo, 7, 1)

        quality = QHBoxLayout()
        quality.setSpacing(6)
        self.relief_chk = QCheckBox("Relief shading")
        self.relief_chk.setChecked(True)
        self.relief_chk.setToolTip(
            "Display-only directional surface shading calculated from the native DEM/DSM. "
            "It improves roofs, embankments, vegetation and terrain shape without changing Z."
        )
        self.relief_strength_spin = QDoubleSpinBox()
        self.relief_strength_spin.setRange(0.0, 1.0)
        self.relief_strength_spin.setDecimals(2)
        self.relief_strength_spin.setSingleStep(0.05)
        self.relief_strength_spin.setValue(0.45)
        self.relief_strength_spin.setToolTip("Display-only relief strength. 0 = none, 1 = strongest shading.")
        self.relief_strength_spin.setMaximumWidth(72)
        self.adaptive_points_chk = QCheckBox("Auto point size")
        self.adaptive_points_chk.setChecked(True)
        self.adaptive_points_chk.setToolTip(
            "Slightly enlarges points when zoomed out and shrinks them during close inspection. "
            "This changes only screen-space point size, never geometry or QC values."
        )
        quality.addWidget(self.relief_chk)
        quality.addWidget(self.relief_strength_spin)
        quality.addWidget(self.adaptive_points_chk)
        quality.addStretch()
        form.addLayout(quality, 8, 0, 1, 2)

        flags1 = QHBoxLayout()
        flags1.setSpacing(8)
        self.invert_chk = QCheckBox("Invert colors")
        self.grid_chk = QCheckBox("Show grid")
        self.grid_chk.setChecked(True)
        flags1.addWidget(self.invert_chk)
        flags1.addWidget(self.grid_chk)
        flags1.addStretch()
        form.addLayout(flags1, 9, 0, 1, 2)

        flags2 = QHBoxLayout()
        flags2.setSpacing(8)
        self.auto_pct_chk = QCheckBox("Auto elevation range")
        self.auto_pct_chk.setChecked(True)
        self.auto_pct_chk.setToolTip("Use percentile-based elevation display limits so isolated spikes do not dominate the color range.")
        self.clip_range_chk = QCheckBox("Clip outside range")
        self.clip_range_chk.setChecked(False)
        self.clip_range_chk.setToolTip("Hide rendered points outside the active elevation range. QC values remain unchanged.")
        flags2.addWidget(self.auto_pct_chk)
        flags2.addWidget(self.clip_range_chk)
        flags2.addStretch()
        form.addLayout(flags2, 10, 0, 1, 2)

        self.p_lo = QDoubleSpinBox()
        self.p_lo.setRange(0.0, 49.9)
        self.p_lo.setDecimals(1)
        self.p_lo.setSingleStep(0.5)
        self.p_lo.setValue(2.0)
        self.p_hi = QDoubleSpinBox()
        self.p_hi.setRange(50.1, 100.0)
        self.p_hi.setDecimals(1)
        self.p_hi.setSingleStep(0.5)
        self.p_hi.setValue(98.0)
        form.addWidget(QLabel("Range low (%)"), 11, 0)
        form.addWidget(self.p_lo, 11, 1)
        form.addWidget(QLabel("Range high (%)"), 12, 0)
        form.addWidget(self.p_hi, 12, 1)

        self.zmin_spin = QDoubleSpinBox()
        self.zmin_spin.setDecimals(3)
        self.zmin_spin.setRange(-1e9, 1e9)
        self.zmin_spin.setValue(0.0)
        self.zmin_spin.setEnabled(False)
        self.zmax_spin = QDoubleSpinBox()
        self.zmax_spin.setDecimals(3)
        self.zmax_spin.setRange(-1e9, 1e9)
        self.zmax_spin.setValue(1.0)
        self.zmax_spin.setEnabled(False)
        form.addWidget(QLabel("Elevation min"), 13, 0)
        form.addWidget(self.zmin_spin, 13, 1)
        form.addWidget(QLabel("Elevation max"), 14, 0)
        form.addWidget(self.zmax_spin, 14, 1)

        self.robust_focus_chk = QCheckBox("Ignore extreme Z for camera")
        self.robust_focus_chk.setChecked(True)
        self.robust_focus_chk.setToolTip("Prevent isolated extreme Z values from forcing the camera to zoom too far out.")
        self.cursor_zoom_chk = QCheckBox("Zoom toward cursor")
        self.cursor_zoom_chk.setChecked(True)
        self.cursor_zoom_chk.setToolTip("Mouse-wheel zoom follows the nearest rendered point under the cursor. Double-click a point to set the orbit pivot exactly.")
        form.addWidget(self.robust_focus_chk, 15, 0, 1, 2)
        form.addWidget(self.cursor_zoom_chk, 16, 0, 1, 2)

        cams = QGridLayout()
        cams.setSpacing(2)
        self.reset_cam_btn = QPushButton("Isometric")
        self.top_cam_btn = QPushButton("Top")
        self.side_cam_btn = QPushButton("Side")
        self.focus_cam_btn = QPushButton("Fit data")
        cams.addWidget(self.reset_cam_btn, 0, 0)
        cams.addWidget(self.top_cam_btn, 0, 1)
        cams.addWidget(self.side_cam_btn, 1, 0)
        cams.addWidget(self.focus_cam_btn, 1, 1)
        form.addLayout(cams, 17, 0, 1, 2)

        self.size_spin.valueChanged.connect(self.request_display_update)
        self.zex_spin.valueChanged.connect(self.request_display_update)
        self.render_style_combo.currentIndexChanged.connect(self.on_render_style_changed)
        self.detail_combo.currentIndexChanged.connect(self.on_detail_changed)
        self.density_combo.currentIndexChanged.connect(self.on_visual_density_changed)
        self.cmap_combo.currentIndexChanged.connect(self.on_color_mode_changed)
        self.imagery_chk.toggled.connect(self.on_imagery_toggle)
        self.imagery_combo.currentIndexChanged.connect(self.on_imagery_source_changed)
        self.relief_chk.toggled.connect(self.request_display_update)
        self.relief_strength_spin.valueChanged.connect(self.request_display_update)
        self.adaptive_points_chk.toggled.connect(self.on_adaptive_points_toggle)
        self.opacity_spin.valueChanged.connect(self.request_display_update)
        self.invert_chk.toggled.connect(self.request_display_update)
        self.grid_chk.toggled.connect(self.on_grid_toggle)
        self.auto_pct_chk.toggled.connect(self.on_auto_clamp_toggle)
        self.p_lo.valueChanged.connect(self.request_display_update)
        self.p_hi.valueChanged.connect(self.request_display_update)
        self.zmin_spin.valueChanged.connect(self.request_display_update)
        self.zmax_spin.valueChanged.connect(self.request_display_update)
        self.clip_range_chk.toggled.connect(self.request_display_update)
        self.robust_focus_chk.toggled.connect(self.on_focus_setting_changed)
        self.cursor_zoom_chk.toggled.connect(self.on_cursor_zoom_toggle)
        self.reset_cam_btn.clicked.connect(lambda: self.on_camera_preset("iso"))
        self.top_cam_btn.clicked.connect(lambda: self.on_camera_preset("top"))
        self.side_cam_btn.clicked.connect(lambda: self.on_camera_preset("profile_x"))
        self.focus_cam_btn.clicked.connect(self.on_focus_range)
        self._color_widgets = [
            self.cmap_combo, self.opacity_spin, self.imagery_chk, self.relief_chk, self.relief_strength_spin,
            self.invert_chk, self.auto_pct_chk, self.p_lo, self.p_hi, self.zmin_spin, self.zmax_spin,
            self.clip_range_chk, self.robust_focus_chk,
        ]
        self._refresh_imagery_layers()
        try:
            QgsProject.instance().layersAdded.connect(self._on_project_layers_changed)
            QgsProject.instance().layersRemoved.connect(self._on_project_layers_changed)
        except Exception:
            pass
        controls_layout.addWidget(self.display_group)

        # --- QC summary ------------------------------------------------------
        self.qc_group = QGroupBox("Elevation QC")
        qc_layout = QVBoxLayout(self.qc_group)
        qc_layout.setContentsMargins(5, 7, 5, 5)
        self.stats_label = QLabel("Draw a box or cross-section to calculate elevation statistics.")
        self.stats_label.setWordWrap(True)
        self.stats_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        qc_layout.addWidget(self.stats_label)
        controls_layout.addWidget(self.qc_group)
        controls_layout.addStretch()

        self.progress_label = QLabel("")
        self.progress_label.setVisible(False)
        controls_layout.addWidget(self.progress_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        controls_layout.addWidget(self.progress)
        bottom = QHBoxLayout()
        bottom.setSpacing(3)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.on_cancel_task)
        bottom.addWidget(self.cancel_btn)
        controls_layout.addLayout(bottom)

        ctrl_content = QWidget()
        ctrl_content.setLayout(controls_layout)
        ctrl_scroll = QScrollArea()
        ctrl_scroll.setWidgetResizable(True)
        ctrl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ctrl_scroll.setWidget(ctrl_content)
        ctrl_scroll.setMinimumWidth(280)

        # The preview is intentionally created here (the viewer owns all data
        # and signals) but is hosted by a separate QDockWidget in main_plugin.
        # This keeps the controls compact while the 3D viewport starts as a
        # large floating window which the user can freely dock/undock.
        self.preview = PointCloudPreview()
        self.preview.setMinimumSize(520, 420)
        try:
            self.preview.interaction_finished.connect(self._on_preview_interaction_finished)
            self.preview.point_picked.connect(self._on_3d_point_picked)
        except Exception as exc:
            log_exception("Could not connect preview signals", exc, Qgis.Info)
        self.preview.set_cursor_zoom_enabled(True)
        self.preview.set_adaptive_point_size_enabled(True)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(3, 3, 3, 3)
        main_layout.addWidget(ctrl_scroll)
        self.setLayout(main_layout)

        if not self._dependencies_ok:
            self.draw_btn.setEnabled(False)
            self.cs_btn.setEnabled(False)

        self.on_add_selected_surfaces(silent=True)
        self._refresh_surface_units_and_defaults()
        self.on_auto_clamp_toggle(True)

    # --- Slots ---------------------------------------------------------------

    def detail_level_name(self) -> str:
        try:
            name = str(self.detail_combo.currentText()).strip()
        except Exception:
            name = "Auto"
        return name if name in DETAIL_POINT_BUDGETS else "Auto"

    def detail_point_budget(self) -> int:
        return int(DETAIL_POINT_BUDGETS.get(self.detail_level_name(), PREVIEW_TARGET_POINTS))

    def render_style_key(self) -> str:
        try:
            return "surface" if str(self.render_style_combo.currentText()).strip().lower().startswith("surface") else "points"
        except Exception:
            return "points"

    def on_render_style_changed(self, *args):
        surface = self.render_style_key() == "surface"
        try:
            self.size_spin.setEnabled(not surface)
            self.density_combo.setEnabled(not surface)
            self.density_combo.setToolTip(
                "Surface mode already renders continuous triangles from the raster grid." if surface else
                "Display-only native-Z scatter. Dense 2×/3× randomizes XY inside native raster cells without interpolating elevation."
            )
        except Exception:
            pass
        if surface:
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                "Surface mode: valid raster cells are connected into triangles; NoData remains holes. QC and measurement remain native-sample based.",
                level=Qgis.Info, duration=5
            )
        if self._preview_mode == "cross_section" and self._last_cs_canvas_p1 is not None and self._last_cs_canvas_p2 is not None and self._last_cs_width is not None:
            layers = self.get_surface_layers(fallback=True)
            if layers:
                p1 = QgsPointXY(self._last_cs_canvas_p1)
                p2 = QgsPointXY(self._last_cs_canvas_p2)
                width = float(self._last_cs_width)
                QTimer.singleShot(0, lambda: self.run_cs_3d(p1, p2, width, layers))
                return
        self._schedule_box_rebuild(f"Render style: {'Surface' if surface else 'Points'}")

    def detail_cross_section_cap(self) -> int:
        name = self.detail_level_name()
        return {"Auto": 300_000, "High": 400_000, "Ultra": 500_000, "Maximum": 500_000}.get(name, 300_000)

    def on_detail_changed(self, *args):
        name = self.detail_level_name()
        if name == "Maximum":
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                f"Maximum detail allows up to {self.detail_point_budget():,} rendered points. "
                "Use a smaller AOI if navigation becomes heavy.",
                level=Qgis.Info,
                duration=5,
            )
        self._schedule_box_rebuild(f"3D detail: {name}")

    def visual_density_name(self) -> str:
        try:
            name = str(self.density_combo.currentText()).strip()
        except Exception:
            name = "Native"
        return name if name in VISUAL_DENSITY_FACTORS else "Native"

    def visual_density_factor(self) -> int:
        return int(VISUAL_DENSITY_FACTORS.get(self.visual_density_name(), 1))

    def on_visual_density_changed(self, *args):
        name = self.visual_density_name()
        if name != "Native":
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                f"{name} is display-only native-Z scatter; synthetic XY is randomized "
                "but elevation stays exactly on native raster values. Measurements and QC remain native-raster only.",
                level=Qgis.Info,
                duration=4,
            )
        self._schedule_box_rebuild(f"Visual density: {name}")

    def remember_box_extent(self, extent: QgsRectangle) -> None:
        """Remember the last user-drawn AOI in the canvas CRS used at draw time."""
        self._last_box_extent = QgsRectangle(extent)
        try:
            self._last_box_canvas_crs = iface.mapCanvas().mapSettings().destinationCrs()
        except Exception:
            self._last_box_canvas_crs = None

    def _last_box_extent_in_current_canvas(self):
        if self._last_box_extent is None:
            return None
        extent = QgsRectangle(self._last_box_extent)
        try:
            old_crs = self._last_box_canvas_crs
            new_crs = iface.mapCanvas().mapSettings().destinationCrs()
            if (
                old_crs is not None
                and old_crs.isValid()
                and new_crs.isValid()
                and old_crs != new_crs
            ):
                transform = QgsCoordinateTransform(old_crs, new_crs, QgsProject.instance())
                extent = transform.transformBoundingBox(extent)
        except Exception as exc:
            log_exception("Could not transform remembered 3D box", exc, Qgis.Warning)
            return None
        return extent

    def _schedule_box_rebuild(self, reason: str = "") -> None:
        """Debounce settings that require a fresh extraction of the same 3D AOI."""
        if self._last_box_extent is None or self._preview_mode != "box":
            return
        self._pending_box_rebuild_reason = str(reason or "3D settings changed")
        try:
            self._box_rebuild_timer.start(275)
        except Exception:
            self._rebuild_last_box()

    def _rebuild_last_box(self) -> None:
        if self._last_box_extent is None or self._preview_mode != "box":
            return
        if self._active_cs_task is not None:
            # Never replace an active cross-section task with an old area view.
            return
        extent = self._last_box_extent_in_current_canvas()
        if extent is None or extent.isEmpty():
            return
        reason = self._pending_box_rebuild_reason or "3D settings changed"
        self._pending_box_rebuild_reason = ""
        try:
            iface.messageBar().pushMessage(
                "Terrain Viewer",
                f"Updating the current box — {reason}.",
                level=Qgis.Info,
                duration=2,
            )
        except Exception:
            pass
        # Keep the existing viewport visible while extraction runs. The task
        # completion replaces the points but restores the exact camera state,
        # making Native/2×/3× and Auto/High/Ultra comparisons immediate.
        self.rect_tool.handle_extent(
            extent,
            remember_extent=False,
            preserve_camera=True,
        )

    def _surface_item_for_id(self, layer_id: str):
        for i in range(self.surface_list.count()):
            item = self.surface_list.item(i)
            if item.data(Qt.UserRole) == layer_id:
                return item
        return None

    def _add_surface_layer(self, layer, checked: bool = True) -> bool:
        if not is_raster_layer(layer):
            return False
        try:
            raster_source_path(layer)
        except Exception as exc:
            iface.messageBar().pushMessage("Surfaces", f"Cannot add '{layer.name()}': {exc}", level=Qgis.Warning, duration=6)
            return False
        layer_id = str(layer.id())
        if self._surface_item_for_id(layer_id) is not None:
            return False
        color_index = int(self._surface_color_counter)
        self._surface_color_counter += 1
        rgb = surface_rgb(color_index)
        item = QListWidgetItem(layer.name())
        item.setData(Qt.UserRole, layer_id)
        item.setData(Qt.UserRole + 1, color_index)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        try:
            item.setForeground(QColor.fromRgbF(float(rgb[0]), float(rgb[1]), float(rgb[2])))
            item.setToolTip(
                f"{raster_source_path(layer)}\nCRS: {layer.crs().authid() or layer.crs().description()}\n"
                "Uncheck to hide this surface in the current 3D preview."
            )
        except Exception:
            pass
        self.surface_list.addItem(item)
        self._surface_layer_ids.append(layer_id)
        self._refresh_surface_units_and_defaults()
        return True

    def surface_rgb_for_layer(self, layer_id: str, fallback_index: int = 0):
        item = self._surface_item_for_id(str(layer_id))
        if item is not None:
            try:
                return surface_rgb(int(item.data(Qt.UserRole + 1)))
            except Exception:
                pass
        return surface_rgb(fallback_index)

    def _on_cs_width_user_change(self, *args):
        self._cs_width_user_set = True

    def _refresh_surface_units_and_defaults(self):
        layers = self.get_surface_layers(fallback=False) if hasattr(self, "surface_list") else []
        if not layers:
            try:
                self.cs_width_spin.setSuffix("")
            except Exception:
                pass
            return
        first = layers[0]
        try:
            unit = crs_unit_label(first)
            self.cs_width_spin.setSuffix(f" {unit}")
            if not self._cs_width_user_set:
                with rasterio.open(raster_source_path(first)) as src:
                    rx, ry = (abs(float(v)) for v in src.res)
                default_width = max(min(rx, ry) * 5.0, min(rx, ry))
                self.cs_width_spin.blockSignals(True)
                self.cs_width_spin.setValue(default_width)
                self.cs_width_spin.blockSignals(False)
        except Exception:
            pass

    def on_surface_item_changed(self, item):
        if item is None:
            return
        layer_id = str(item.data(Qt.UserRole))
        visible = item.checkState() == Qt.Checked
        found = False
        try:
            found = bool(self.preview.set_layer_visible(layer_id, visible))
        except Exception:
            found = False
        for layer_info in list(self._last_cloud_layers or []):
            if str(layer_info.get("layer_id", "")) == layer_id:
                layer_info["visible"] = bool(visible)
                found = True
        if visible and not found and self._last_cloud_layers:
            iface.messageBar().pushMessage(
                "Surfaces",
                "That surface is enabled for the next extraction. Draw the box again to load it into the current 3D preview.",
                level=Qgis.Info,
                duration=4,
            )
        self._refresh_surface_units_and_defaults()
        # Visibility changes must never throw away the user's current camera/orbit.
        self.request_display_update(reset_camera=False)

    def on_add_selected_surfaces(self, *args, silent: bool = False):
        # Elevation surfaces must be GDAL/file-backed. WMS/WMTS/XYZ layers are
        # imagery-only and must never be auto-added as DEM/DTM surfaces.
        layers = selected_surface_layers()
        if not layers:
            active = iface.activeLayer()
            if is_supported_surface_layer(active):
                layers = [active]
        added = 0
        for layer in layers:
            added += 1 if self._add_surface_layer(layer) else 0
        if not silent and not layers:
            iface.messageBar().pushMessage(
                "Surfaces",
                "Select a file/GDAL-backed DEM, DTM or DSM layer. WMS/XYZ layers belong in the Imagery dropdown.",
                level=Qgis.Info, duration=5
            )
        elif not silent and added:
            iface.messageBar().pushMessage("Surfaces", f"Added {added} surface(s).", level=Qgis.Success, duration=3)
        self._refresh_surface_units_and_defaults()

    def on_add_surface_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add DEM/DTM Surfaces",
            "",
            "Raster files (*.tif *.tiff *.img *.vrt *.asc *.bil *.dem);;All files (*.*)",
        )
        if not paths:
            return
        added = 0
        failed = []
        project = QgsProject.instance()
        for path in paths:
            layer = QgsRasterLayer(path, Path(path).stem)
            if not layer.isValid():
                failed.append(path)
                continue
            project.addMapLayer(layer)
            if self._add_surface_layer(layer):
                added += 1
        if added:
            iface.messageBar().pushMessage("Surfaces", f"Added {added} surface(s) to QGIS and the viewer.", level=Qgis.Success, duration=4)
        if failed:
            iface.messageBar().pushMessage("Surfaces", f"Could not open {len(failed)} raster file(s).", level=Qgis.Warning, duration=6)
        self._refresh_surface_units_and_defaults()

    def on_remove_surfaces(self):
        rows = sorted({idx.row() for idx in self.surface_list.selectedIndexes()}, reverse=True)
        removed_ids = []
        for row in rows:
            item = self.surface_list.takeItem(row)
            if item is not None:
                lid = str(item.data(Qt.UserRole))
                removed_ids.append(lid)
                if lid in self._surface_layer_ids:
                    self._surface_layer_ids.remove(lid)
                try:
                    self.preview.set_layer_visible(lid, False)
                except Exception:
                    pass
        for info in list(self._last_cloud_layers or []):
            if str(info.get("layer_id", "")) in removed_ids:
                info["visible"] = False
        self._refresh_surface_units_and_defaults()
        self.request_display_update(reset_camera=False)

    def on_clear_surfaces(self):
        for info in list(self._last_cloud_layers or []):
            info["visible"] = False
            try:
                self.preview.set_layer_visible(str(info.get("layer_id", "")), False)
            except Exception:
                pass
        self.surface_list.clear()
        self._surface_layer_ids = []
        self._refresh_surface_units_and_defaults()
        self.request_display_update(reset_camera=False)

    def _vector_item_for_id(self, layer_id: str):
        for i in range(self.vector_list.count()):
            item = self.vector_list.item(i)
            if str(item.data(Qt.UserRole)) == str(layer_id):
                return item
        return None

    def _add_vector_layer(self, layer, checked: bool = True) -> bool:
        if not is_line_vector_layer(layer):
            return False
        layer_id = str(layer.id())
        if self._vector_item_for_id(layer_id) is not None:
            return False
        color_index = int(self._vector_color_counter)
        self._vector_color_counter += 1
        rgb = vector_overlay_rgb(color_index)
        item = QListWidgetItem(layer.name())
        item.setData(Qt.UserRole, layer_id)
        item.setData(Qt.UserRole + 1, color_index)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        try:
            item.setForeground(QColor.fromRgbF(float(rgb[0]), float(rgb[1]), float(rgb[2])))
            item.setToolTip(
                f"{layer.source()}\nCRS: {layer.crs().authid() or layer.crs().description()}\n"
                "Supports geometry Z or a constant feature attribute named Z, ELEV, ELEVATION, CONTOUR, or HEIGHT."
            )
        except Exception:
            pass
        self.vector_list.addItem(item)
        self._vector_layer_ids.append(layer_id)
        return True

    def get_vector_layers(self):
        layers = []
        project = QgsProject.instance()
        stale = []
        for i in range(self.vector_list.count()):
            item = self.vector_list.item(i)
            if item.checkState() != Qt.Checked:
                continue
            layer = project.mapLayer(str(item.data(Qt.UserRole)))
            if layer is None or not is_line_vector_layer(layer):
                stale.append(i)
                continue
            layers.append(layer)
        for row in reversed(stale):
            self.vector_list.takeItem(row)
        return layers

    def on_add_selected_vectors(self):
        layers = selected_line_vector_layers()
        added = sum(1 for layer in layers if self._add_vector_layer(layer))
        if not layers:
            iface.messageBar().pushMessage(
                "Vector overlays", "Select one or more line vector layers in QGIS first.",
                level=Qgis.Info, duration=4
            )
        elif added:
            iface.messageBar().pushMessage(
                "Vector overlays", f"Added {added} line layer(s).", level=Qgis.Success, duration=3
            )
        self._refresh_vector_overlays()

    def on_add_vector_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add SHP / GeoPackage line overlays", "",
            "Vector files (*.shp *.gpkg *.geojson *.json);;All files (*.*)"
        )
        if not paths:
            return
        project = QgsProject.instance()
        added = 0
        failed = []
        for path in paths:
            candidates = []
            if str(path).lower().endswith(".gpkg"):
                try:
                    meta = QgsProviderRegistry.instance().providerMetadata("ogr")
                    for detail in (meta.querySublayers(path) or []):
                        try:
                            candidates.append(QgsVectorLayer(detail.uri(), detail.name(), "ogr"))
                        except Exception:
                            pass
                except Exception:
                    pass
            if not candidates:
                candidates = [QgsVectorLayer(path, Path(path).stem, "ogr")]
            any_valid = False
            for layer in candidates:
                if layer is None or not layer.isValid() or not is_line_vector_layer(layer):
                    continue
                any_valid = True
                project.addMapLayer(layer)
                if self._add_vector_layer(layer):
                    added += 1
            if not any_valid:
                failed.append(path)
        if added:
            iface.messageBar().pushMessage(
                "Vector overlays", f"Added {added} line overlay layer(s).", level=Qgis.Success, duration=4
            )
        if failed:
            iface.messageBar().pushMessage(
                "Vector overlays", f"Could not find a supported line layer in {len(failed)} file(s).",
                level=Qgis.Warning, duration=6
            )
        self._refresh_vector_overlays()

    def on_remove_vectors(self):
        rows = sorted({idx.row() for idx in self.vector_list.selectedIndexes()}, reverse=True)
        for row in rows:
            item = self.vector_list.takeItem(row)
            if item is None:
                continue
            lid = str(item.data(Qt.UserRole))
            if lid in self._vector_layer_ids:
                self._vector_layer_ids.remove(lid)
            try:
                self.preview.set_vector_visible(lid, False)
            except Exception:
                pass
        self._refresh_vector_overlays()

    def on_clear_vectors(self):
        self.vector_list.clear()
        self._vector_layer_ids = []
        try:
            self.preview.clear_vector_overlays()
        except Exception:
            pass

    def on_vector_item_changed(self, item):
        if item is None:
            return
        lid = str(item.data(Qt.UserRole))
        visible = item.checkState() == Qt.Checked
        found = False
        try:
            found = bool(self.preview.set_vector_visible(lid, visible))
        except Exception:
            found = False
        if visible and not found:
            self._refresh_vector_overlays()

    @staticmethod
    def _vector_z_field(layer):
        preferred = ("z", "elev", "elevation", "contour", "height")
        try:
            by_lower = {str(f.name()).lower(): str(f.name()) for f in layer.fields()}
        except Exception:
            return None
        for name in preferred:
            if name in by_lower:
                return by_lower[name]
        return None

    def _refresh_vector_overlays(self):
        if not hasattr(self, "preview") or self.preview is None:
            return
        vector_layers = self.get_vector_layers() if hasattr(self, "vector_list") else []
        if not vector_layers or not self._last_cloud_layers or self._last_preview_crs is None:
            try:
                self.preview.clear_vector_overlays()
            except Exception:
                pass
            return

        if (
            getattr(self, "_preview_mode", "") == "cross_section"
            and self._last_cs_p1 is not None
            and self._last_cs_p2 is not None
        ):
            self._refresh_cross_section_vector_overlays(vector_layers)
            return

        # Use the current extracted DEM footprint as the clipping region.
        xyz_parts = []
        for info in list(self._last_cloud_layers or []):
            xyz = np.asarray(info.get("xyz", np.empty((0, 3))), dtype=np.float64)
            if xyz.ndim == 2 and xyz.shape[0]:
                xyz_parts.append(xyz[:, :2])
        if not xyz_parts:
            self.preview.clear_vector_overlays()
            return
        xy = np.vstack(xyz_parts)
        xmin, ymin = np.nanmin(xy, axis=0)
        xmax, ymax = np.nanmax(xy, axis=0)
        if not np.all(np.isfinite([xmin, ymin, xmax, ymax])):
            return
        pad = max(float(xmax - xmin), float(ymax - ymin), 1.0) * 0.01
        preview_rect = QgsRectangle(float(xmin - pad), float(ymin - pad), float(xmax + pad), float(ymax + pad))
        project = QgsProject.instance()
        overlays = []
        max_vertices = 500_000

        for layer_idx, layer in enumerate(vector_layers):
            item = self._vector_item_for_id(str(layer.id()))
            color_index = layer_idx
            if item is not None:
                try:
                    color_index = int(item.data(Qt.UserRole + 1))
                except Exception:
                    pass
            rgb = vector_overlay_rgb(color_index)
            transform = None
            request_rect = QgsRectangle(preview_rect)
            try:
                if layer.crs().isValid() and self._last_preview_crs.isValid() and layer.crs() != self._last_preview_crs:
                    to_preview = QgsCoordinateTransform(layer.crs(), self._last_preview_crs, project)
                    to_layer = QgsCoordinateTransform(self._last_preview_crs, layer.crs(), project)
                    transform = to_preview
                    request_rect = to_layer.transformBoundingBox(preview_rect)
            except Exception as exc:
                log_exception(f"Vector CRS transform failed for {layer.name()}", exc, Qgis.Info)
                continue

            z_field = self._vector_z_field(layer)
            vertices = []
            skipped_no_z = 0
            try:
                request = QgsFeatureRequest().setFilterRect(request_rect)
                for feature in layer.getFeatures(request):
                    geom = feature.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    g = QgsGeometry(geom)
                    try:
                        g.convertToStraightSegment()
                    except Exception:
                        pass
                    has_z = False
                    try:
                        has_z = bool(QgsWkbTypes.hasZ(g.wkbType()))
                    except Exception:
                        pass
                    attr_z = None
                    if not has_z and z_field:
                        try:
                            attr_z = float(feature[z_field])
                            if not np.isfinite(attr_z):
                                attr_z = None
                        except Exception:
                            attr_z = None
                    if not has_z and attr_z is None:
                        skipped_no_z += 1
                        continue
                    try:
                        parts = g.asGeometryCollection() if g.isMultipart() else [g]
                    except Exception:
                        parts = [g]
                    for part in parts:
                        pts = []
                        try:
                            iterator = part.vertices()
                        except Exception:
                            continue
                        for pnt in iterator:
                            try:
                                x, y = float(pnt.x()), float(pnt.y())
                                if transform is not None:
                                    tp = transform.transform(QgsPointXY(x, y))
                                    x, y = float(tp.x()), float(tp.y())
                                z = float(pnt.z()) if has_z else float(attr_z)
                                if not np.all(np.isfinite([x, y, z])):
                                    continue
                                pts.append((x, y, z))
                            except Exception:
                                continue
                        for a, b in zip(pts[:-1], pts[1:]):
                            vertices.extend((a, b))
                            if len(vertices) >= max_vertices:
                                break
                        if len(vertices) >= max_vertices:
                            break
                    if len(vertices) >= max_vertices:
                        break
            except Exception as exc:
                log_exception(f"Could not read vector overlay {layer.name()}", exc, Qgis.Warning)
                continue

            if not vertices:
                if skipped_no_z:
                    iface.messageBar().pushMessage(
                        "Vector overlays",
                        f"'{layer.name()}' has no usable geometry Z or Z/ELEV/ELEVATION/CONTOUR/HEIGHT values in the current view.",
                        level=Qgis.Info, duration=5
                    )
                continue
            arr = np.asarray(vertices, dtype=np.float32)
            rgba = np.tile(np.asarray([[rgb[0], rgb[1], rgb[2], 1.0]], dtype=np.float32), (arr.shape[0], 1))
            overlays.append({
                "xyz": arr, "rgba": rgba, "width": 1.8,
                "layer_id": str(layer.id()), "name": str(layer.name()), "visible": True,
            })

        self.preview.set_vector_overlays(overlays)

    @staticmethod
    def _clip_segment_to_cross_section_corridor(a, b, x1, y1, ux, uy, length, half_width, eps=1e-12):
        """
        Clip a 3D vector segment to the exact cross-section sampling corridor.

        The DEM/DTM cross-section is sampled from a slab whose local coordinates are:
          s = distance along the transect, 0 .. length
          q = perpendicular offset, -half_width .. +half_width

        Returning the clipped 3D endpoints makes contour/breakline overlays use the
        same physical thickness as the raster point cloud instead of intersecting
        only the centerline.
        """
        ax, ay, az = (float(a[0]), float(a[1]), float(a[2]))
        bx, by, bz = (float(b[0]), float(b[1]), float(b[2]))
        vx, vy = bx - ax, by - ay

        # Local corridor coordinates.  Perpendicular unit vector is (-uy, ux).
        sa = (ax - x1) * ux + (ay - y1) * uy
        qa = (ax - x1) * (-uy) + (ay - y1) * ux
        sb = (bx - x1) * ux + (by - y1) * uy
        qb = (bx - x1) * (-uy) + (by - y1) * ux
        ds = sb - sa
        dq = qb - qa

        # Liang-Barsky clipping in local (s, q) coordinates.
        t0, t1 = 0.0, 1.0
        bounds = (
            (-ds, sa - 0.0),
            ( ds, float(length) - sa),
            (-dq, qa + float(half_width)),
            ( dq, float(half_width) - qa),
        )
        for p, q in bounds:
            if abs(p) <= eps:
                if q < -eps:
                    return None
                continue
            r = q / p
            if p < 0.0:
                if r > t1 + eps:
                    return None
                if r > t0:
                    t0 = r
            else:
                if r < t0 - eps:
                    return None
                if r < t1:
                    t1 = r

        if t1 < t0 - eps:
            return None
        t0 = min(1.0, max(0.0, t0))
        t1 = min(1.0, max(0.0, t1))
        if t1 - t0 <= eps:
            return None

        p0 = (
            ax + (bx - ax) * t0,
            ay + (by - ay) * t0,
            az + (bz - az) * t0,
        )
        p1 = (
            ax + (bx - ax) * t1,
            ay + (by - ay) * t1,
            az + (bz - az) * t1,
        )
        return p0, p1

    def _refresh_cross_section_vector_overlays(self, vector_layers):
        """
        Clip contour/breakline geometry to the same corridor used to build the
        raster cross-section point cloud.

        This intentionally uses the full cross-section width, not just the center
        transect.  A contour segment is therefore visible only for the portion that
        lies inside the sampled DEM/DTM slab. Geometry-Z is preserved/interpolated;
        2D contours use Z/ELEV/ELEVATION/CONTOUR/HEIGHT attributes.
        """
        p1 = self._last_cs_p1
        p2 = self._last_cs_p2
        if p1 is None or p2 is None or self._last_preview_crs is None:
            self.preview.clear_vector_overlays()
            return

        x1, y1 = float(p1.x()), float(p1.y())
        x2, y2 = float(p2.x()), float(p2.y())
        dx, dy = x2 - x1, y2 - y1
        length = float(np.hypot(dx, dy))
        if not np.isfinite(length) or length <= 0:
            self.preview.clear_vector_overlays()
            return

        ux, uy = dx / length, dy / length
        width = max(float(self._last_cs_width or 0.0), 0.0)
        half_width = max(width * 0.5, 1e-9)
        # Bounding-box prefilter around the actual rotated corridor.  It may include
        # extra features, but exact clipping below removes everything outside it.
        px, py = -uy, ux
        corridor_corners = [
            (x1 + px * half_width, y1 + py * half_width),
            (x1 - px * half_width, y1 - py * half_width),
            (x2 + px * half_width, y2 + py * half_width),
            (x2 - px * half_width, y2 - py * half_width),
        ]
        xs = [c[0] for c in corridor_corners]
        ys = [c[1] for c in corridor_corners]
        preview_rect = QgsRectangle(min(xs), min(ys), max(xs), max(ys))

        project = QgsProject.instance()
        overlays = []
        max_segments_per_layer = 100_000

        for layer_idx, layer in enumerate(vector_layers):
            item = self._vector_item_for_id(str(layer.id()))
            color_index = layer_idx
            if item is not None:
                try:
                    color_index = int(item.data(Qt.UserRole + 1))
                except Exception:
                    pass
            rgb = vector_overlay_rgb(color_index)
            transform = None
            request_rect = QgsRectangle(preview_rect)
            try:
                if layer.crs().isValid() and self._last_preview_crs.isValid() and layer.crs() != self._last_preview_crs:
                    transform = QgsCoordinateTransform(layer.crs(), self._last_preview_crs, project)
                    to_layer = QgsCoordinateTransform(self._last_preview_crs, layer.crs(), project)
                    request_rect = to_layer.transformBoundingBox(preview_rect)
            except Exception as exc:
                log_exception(f"Vector cross-section CRS transform failed for {layer.name()}", exc, Qgis.Info)
                continue

            z_field = self._vector_z_field(layer)
            clipped_vertices = []
            segment_count = 0
            try:
                request = QgsFeatureRequest().setFilterRect(request_rect)
                for feature in layer.getFeatures(request):
                    geom = feature.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    g = QgsGeometry(geom)
                    try:
                        g.convertToStraightSegment()
                    except Exception:
                        pass
                    has_z = False
                    try:
                        has_z = bool(QgsWkbTypes.hasZ(g.wkbType()))
                    except Exception:
                        pass
                    attr_z = None
                    if not has_z and z_field:
                        try:
                            attr_z = float(feature[z_field])
                            if not np.isfinite(attr_z):
                                attr_z = None
                        except Exception:
                            attr_z = None
                    if not has_z and attr_z is None:
                        continue
                    try:
                        parts = g.asGeometryCollection() if g.isMultipart() else [g]
                    except Exception:
                        parts = [g]

                    for part in parts:
                        pts = []
                        try:
                            iterator = part.vertices()
                        except Exception:
                            continue
                        for pnt in iterator:
                            try:
                                vx, vy = float(pnt.x()), float(pnt.y())
                                if transform is not None:
                                    tp = transform.transform(QgsPointXY(vx, vy))
                                    vx, vy = float(tp.x()), float(tp.y())
                                vz = float(pnt.z()) if has_z else float(attr_z)
                                if np.all(np.isfinite([vx, vy, vz])):
                                    pts.append((vx, vy, vz))
                            except Exception:
                                continue

                        for a, b in zip(pts[:-1], pts[1:]):
                            clipped = self._clip_segment_to_cross_section_corridor(
                                a, b, x1, y1, ux, uy, length, half_width
                            )
                            if clipped is None:
                                continue
                            c0, c1 = clipped
                            clipped_vertices.extend((c0, c1))
                            segment_count += 1
                            if segment_count >= max_segments_per_layer:
                                break
                        if segment_count >= max_segments_per_layer:
                            break
                    if segment_count >= max_segments_per_layer:
                        break
            except Exception as exc:
                log_exception(f"Could not build contour cross-section for {layer.name()}", exc, Qgis.Warning)
                continue

            if not clipped_vertices:
                continue
            arr = np.asarray(clipped_vertices, dtype=np.float32)
            rgba = np.tile(
                np.asarray([[rgb[0], rgb[1], rgb[2], 1.0]], dtype=np.float32),
                (arr.shape[0], 1),
            )
            overlays.append({
                "xyz": arr,
                "rgba": rgba,
                "width": 3.0,
                "layer_id": str(layer.id()),
                "name": f"{layer.name()} cross-section corridor",
                "visible": True,
            })

        self.preview.set_vector_overlays(overlays)

    def get_surface_layers(self, fallback: bool = False):
        layers = []
        project = QgsProject.instance()
        stale_rows = []
        for i in range(self.surface_list.count()):
            item = self.surface_list.item(i)
            if item.checkState() != Qt.Checked:
                continue
            layer = project.mapLayer(str(item.data(Qt.UserRole)))
            if layer is None or not is_raster_layer(layer):
                stale_rows.append(i)
                continue
            layers.append(layer)
        for row in reversed(stale_rows):
            self.surface_list.takeItem(row)
        # If the user has explicitly built a surface list, unchecked means excluded.
        # Only fall back to QGIS selection when the viewer list is actually empty.
        if not layers and fallback and self.surface_list.count() == 0:
            # Never fall back to WMS/WMTS/XYZ just because it is the active
            # raster. Those layers are valid imagery sources but not DEM data.
            selected = selected_surface_layers()
            if selected:
                return selected
            active = iface.activeLayer()
            if is_supported_surface_layer(active):
                return [active]
        return layers

    def _set_color_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable display controls without special-casing multi-surface mode."""
        enabled = bool(enabled)
        self._color_controls_available = enabled
        for widget in getattr(self, "_color_widgets", []):
            try:
                widget.setEnabled(enabled)
            except Exception:
                pass
        try:
            self.imagery_combo.setEnabled(enabled and bool(self.imagery_chk.isChecked()))
        except Exception:
            pass
        if enabled:
            self.on_auto_clamp_toggle(bool(self.auto_pct_chk.isChecked()))

    def _deactivate_map_tools(self) -> None:
        canvas = iface.mapCanvas()
        try:
            current = canvas.mapTool()
        except Exception:
            current = None

        try:
            self.rect_tool.clear()
        except Exception:
            pass
        try:
            if self.cs_tool is not None:
                self.cs_tool.clear()
        except Exception:
            pass
        try:
            if self.measure_tool is not None:
                self.measure_tool.clear()
        except Exception:
            pass

        if current is self.rect_tool or current is self.cs_tool or current is self.measure_tool:
            try:
                canvas.unsetMapTool(current)
            except Exception:
                pass
        try:
            self._measure_active = False
            self._measure_points_3d = []
            self._measure_pick_meta = []
            self.preview.set_pick_mode(False)
            self.measure_btn.setText("Measure in 3D")
        except Exception:
            pass
        iface.mainWindow().statusBar().clearMessage()

    def _cancel_all_tasks(self, detach: bool = False) -> bool:
        did_cancel = False

        try:
            task = getattr(self.rect_tool, "_active_task", None)
            if task is not None:
                task.cancel()
                did_cancel = True
                if detach:
                    self.rect_tool._active_task = None
        except Exception as exc:
            log_exception("Could not cancel 3D DEM task", exc, Qgis.Info)

        try:
            task = self._active_cs_task
            if task is not None:
                task.cancel()
                did_cancel = True
                if detach:
                    self._active_cs_task = None
        except Exception as exc:
            log_exception("Could not cancel cross-section task", exc, Qgis.Info)

        return did_cancel

    def _set_processing_state(self, processing: bool) -> None:
        enabled = self._dependencies_ok and not bool(processing)
        self.draw_btn.setEnabled(enabled)
        self.cs_btn.setEnabled(enabled)
        self.measure_btn.setEnabled(enabled)
        self.cs_width_spin.setEnabled(enabled)
        for widget in (
            self.surface_list, self.add_selected_btn, self.add_files_btn, self.remove_surface_btn, self.clear_surfaces_btn,
            self.vector_list, self.add_vector_selected_btn, self.add_vector_files_btn, self.remove_vector_btn, self.clear_vectors_btn,
            self.imagery_chk, self.imagery_combo,
        ):
            try:
                widget.setEnabled(not bool(processing))
            except Exception:
                pass
        try:
            self.imagery_combo.setEnabled(enabled and bool(self.imagery_chk.isChecked()))
        except Exception:
            pass
        self.cancel_btn.setEnabled(bool(processing))

    def on_grid_toggle(self, checked: bool):
        try:
            self.preview.set_grid_visible(bool(checked))
        except Exception:
            pass

    def on_cursor_zoom_toggle(self, checked: bool):
        try:
            self.preview.set_cursor_zoom_enabled(bool(checked))
        except Exception:
            pass

    def on_adaptive_points_toggle(self, checked: bool):
        try:
            self.preview.set_adaptive_point_size_enabled(bool(checked))
        except Exception:
            pass

    def on_auto_clamp_toggle(self, checked: bool):
        if not getattr(self, "_color_controls_available", True):
            return
        checked = bool(checked)
        self.zmin_spin.setEnabled(not checked)
        self.zmax_spin.setEnabled(not checked)
        self.p_lo.setEnabled(checked)
        self.p_hi.setEnabled(checked)
        self.request_display_update()

    def on_focus_setting_changed(self, *args):
        self.request_display_update(reset_camera=True)

    def on_camera_preset(self, preset: str):
        self._last_preset = str(preset or "iso")
        self._last_az = None
        self._last_el = None
        try:
            self.preview.reset_camera(preset=self._last_preset)
        except Exception:
            pass

    def on_focus_range(self):
        zrange = self._current_display_range()
        if zrange is None:
            return
        try:
            self.preview.set_focus_z_range(*zrange)
            self.preview.reset_camera(
                preset=self._last_preset,
                azimuth_deg=self._last_az,
                elevation_deg=self._last_el,
            )
        except Exception:
            pass

    def request_display_update(self, *args, reset_camera: bool = False):
        self._pending_reset_cam = bool(self._pending_reset_cam) or bool(reset_camera)
        try:
            self._update_timer.start(90)
        except Exception:
            self._apply_display_update()

    def _apply_display_update(self):
        try:
            self.update_preview_realtime(reset_camera=bool(self._pending_reset_cam))
        finally:
            self._pending_reset_cam = False

    def on_color_mode_changed(self, *args):
        """Recolor immediately without changing camera state."""
        self.request_display_update(reset_camera=False)

    def _on_project_layers_changed(self, *args):
        self._refresh_imagery_layers()

    def _refresh_imagery_layers(self):
        """Populate the imagery selector with every valid QGIS raster-like layer.

        The selection is explicit on purpose: DEMs, DSMs and hillshades are also
        raster layers, so guessing the 'top' layer can easily color a surface from
        the wrong source.
        """
        previous_id = None
        try:
            previous_id = str(self.imagery_combo.currentData() or "")
        except Exception:
            previous_id = ""

        try:
            self.imagery_combo.blockSignals(True)
            self.imagery_combo.clear()
            self.imagery_combo.addItem("Select imagery layer...", "")

            # Preserve map-canvas order because it is familiar to QGIS users.
            seen = set()
            ordered = []
            try:
                ordered.extend(list(iface.mapCanvas().layers() or []))
            except Exception:
                pass
            try:
                ordered.extend(list(QgsProject.instance().mapLayers().values()))
            except Exception:
                pass

            for layer in ordered:
                try:
                    lid = str(layer.id())
                    if lid in seen or not is_raster_layer(layer) or not layer.isValid():
                        continue
                    seen.add(lid)
                    provider = str(layer.providerType() or "raster").upper()
                    self.imagery_combo.addItem(f"{layer.name()}  [{provider}]", lid)
                except Exception:
                    continue

            if previous_id:
                for i in range(self.imagery_combo.count()):
                    if str(self.imagery_combo.itemData(i) or "") == previous_id:
                        self.imagery_combo.setCurrentIndex(i)
                        break
        finally:
            try:
                self.imagery_combo.blockSignals(False)
            except Exception:
                pass

        self.imagery_combo.setEnabled(bool(self.imagery_chk.isChecked()) and self._color_controls_available)

    def on_imagery_toggle(self, checked: bool):
        self.imagery_combo.setEnabled(bool(checked) and self._color_controls_available)
        self._invalidate_imagery_cache()
        if checked and not str(self.imagery_combo.currentData() or ""):
            iface.messageBar().pushMessage(
                "Aerial RGB",
                "Choose the exact imagery/basemap layer from the Imagery dropdown.",
                level=Qgis.Info,
                duration=4,
            )
        self.request_display_update(reset_camera=False)

    def on_imagery_source_changed(self, *args):
        self._invalidate_imagery_cache()
        if bool(self.imagery_chk.isChecked()):
            self.request_display_update(reset_camera=False)

    def _invalidate_imagery_cache(self):
        self._imagery_cache_key = None
        self._imagery_cache_rgba = None
        self._imagery_cache_extent = None
        self._imagery_cache_source_id = None
        self._imagery_cache_source_name = ""
        self._imagery_missing_warned = False
        # Both caches depend on the current preview footprint. Clearing relief
        # here is cheap and prevents stale shading after AOI/source changes.
        try:
            self._relief_cache.clear()
        except Exception:
            self._relief_cache = {}

    def _resolve_imagery_layer(self):
        """Return the exact raster/XYZ/WMS/WMTS layer chosen in the GUI."""
        if not bool(self.imagery_chk.isChecked()):
            return None
        try:
            layer_id = str(self.imagery_combo.currentData() or "")
        except Exception:
            layer_id = ""
        if not layer_id:
            return None
        try:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None and is_raster_layer(layer) and layer.isValid():
                return layer
        except Exception:
            pass
        return None

    def _preview_xy_extent(self):
        xmin = ymin = float("inf")
        xmax = ymax = float("-inf")
        found = False
        for layer_info in list(self._last_cloud_layers or []):
            if not bool(layer_info.get("visible", True)):
                continue
            try:
                xyz = np.asarray(layer_info.get("xyz", np.empty((0, 3))), dtype=np.float32)
                if xyz.ndim != 2 or xyz.shape[0] == 0:
                    continue
                x = xyz[:, 0].astype(float)
                y = xyz[:, 1].astype(float)
                valid = np.isfinite(x) & np.isfinite(y)
                if not np.any(valid):
                    continue
                xv = x[valid]
                yv = y[valid]
                xmin = min(xmin, float(np.min(xv)))
                xmax = max(xmax, float(np.max(xv)))
                ymin = min(ymin, float(np.min(yv)))
                ymax = max(ymax, float(np.max(yv)))
                found = True
            except Exception:
                continue
        if not found:
            return None

        width = max(xmax - xmin, 0.0)
        height = max(ymax - ymin, 0.0)
        ref = max(width, height, 1.0)
        if width <= 1e-12:
            xmin -= ref * 0.005
            xmax += ref * 0.005
        if height <= 1e-12:
            ymin -= ref * 0.005
            ymax += ref * 0.005
        # Tiny buffer prevents points at the exact footprint edge from falling
        # outside the rendered image because of floating-point rounding.
        pad_x = max((xmax - xmin) * 0.003, ref * 1e-6)
        pad_y = max((ymax - ymin) * 0.003, ref * 1e-6)
        return QgsRectangle(xmin - pad_x, ymin - pad_y, xmax + pad_x, ymax + pad_y)

    @staticmethod
    def _qimage_to_rgba_numpy(image):
        """Convert QImage to HxWx4 uint8 RGBA using PyQt5-compatible APIs."""
        if image is None or image.isNull():
            return None
        img = image.convertToFormat(QImage.Format_RGBA8888)
        width = int(img.width())
        height = int(img.height())
        if width <= 0 or height <= 0:
            return None
        nbytes = int(img.sizeInBytes()) if hasattr(img, "sizeInBytes") else int(img.byteCount())
        ptr = img.bits()
        try:
            ptr.setsize(nbytes)
            buf = np.frombuffer(ptr, dtype=np.uint8, count=nbytes)
        except Exception:
            try:
                buf = np.frombuffer(ptr.asstring(nbytes), dtype=np.uint8, count=nbytes)
            except Exception:
                return None
        bpl = int(img.bytesPerLine())
        if bpl <= 0 or buf.size < height * bpl:
            return None
        rows = buf[: height * bpl].reshape((height, bpl))
        packed = rows[:, : width * 4]
        return packed.reshape((height, width, 4)).copy()

    @staticmethod
    def _bilinear_rgba_sample(rgba, fx, fy, chunk_size=IMAGERY_SAMPLE_CHUNK):
        """Memory-bounded bilinear sampling of an RGBA image.

        ``fx`` and ``fy`` are normalized 0..1 image coordinates.  Returned RGB
        is float32 0..1 and validity requires meaningful interpolated alpha.
        """
        img = np.asarray(rgba, dtype=np.uint8)
        n = int(np.asarray(fx).size)
        rgb_out = np.zeros((n, 3), dtype=np.float32)
        valid_out = np.zeros((n,), dtype=bool)
        if img.ndim != 3 or img.shape[2] < 4 or n <= 0:
            return rgb_out, valid_out
        h, w = int(img.shape[0]), int(img.shape[1])
        if h <= 0 or w <= 0:
            return rgb_out, valid_out
        fx = np.asarray(fx, dtype=np.float64).reshape((-1,))
        fy = np.asarray(fy, dtype=np.float64).reshape((-1,))
        chunk = max(10_000, int(chunk_size))
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            x = fx[start:end]
            y = fy[start:end]
            ok = np.isfinite(x) & np.isfinite(y) & (x >= 0.0) & (x <= 1.0) & (y >= 0.0) & (y <= 1.0)
            if not np.any(ok):
                continue
            xp = np.clip(x, 0.0, 1.0) * max(0, w - 1)
            yp = np.clip(y, 0.0, 1.0) * max(0, h - 1)
            x0 = np.floor(xp).astype(np.int64)
            y0 = np.floor(yp).astype(np.int64)
            x1 = np.minimum(x0 + 1, w - 1)
            y1 = np.minimum(y0 + 1, h - 1)
            tx = (xp - x0).astype(np.float32)
            ty = (yp - y0).astype(np.float32)
            c00 = img[y0, x0].astype(np.float32)
            c10 = img[y0, x1].astype(np.float32)
            c01 = img[y1, x0].astype(np.float32)
            c11 = img[y1, x1].astype(np.float32)
            wx0 = (1.0 - tx)[:, None]
            wx1 = tx[:, None]
            top = c00 * wx0 + c10 * wx1
            bot = c01 * wx0 + c11 * wx1
            out = top * (1.0 - ty)[:, None] + bot * ty[:, None]
            alpha_ok = out[:, 3] > 8.0
            ok &= alpha_ok
            rgb_out[start:end] = np.clip(out[:, :3] / 255.0, 0.0, 1.0)
            valid_out[start:end] = ok
        return rgb_out, valid_out

    @staticmethod
    def _bilinear_scalar_sample(grid, fx, fy, chunk_size=RELIEF_SAMPLE_CHUNK):
        """Memory-bounded bilinear sampling of a float grid."""
        arr = np.asarray(grid, dtype=np.float32)
        n = int(np.asarray(fx).size)
        out = np.ones((n,), dtype=np.float32)
        valid_out = np.zeros((n,), dtype=bool)
        if arr.ndim != 2 or n <= 0:
            return out, valid_out
        h, w = arr.shape
        fx = np.asarray(fx, dtype=np.float64).reshape((-1,))
        fy = np.asarray(fy, dtype=np.float64).reshape((-1,))
        chunk = max(10_000, int(chunk_size))
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            x = fx[start:end]
            y = fy[start:end]
            ok = np.isfinite(x) & np.isfinite(y) & (x >= 0.0) & (x <= 1.0) & (y >= 0.0) & (y <= 1.0)
            if not np.any(ok):
                continue
            xp = np.clip(x, 0.0, 1.0) * max(0, w - 1)
            yp = np.clip(y, 0.0, 1.0) * max(0, h - 1)
            x0 = np.floor(xp).astype(np.int64)
            y0 = np.floor(yp).astype(np.int64)
            x1 = np.minimum(x0 + 1, w - 1)
            y1 = np.minimum(y0 + 1, h - 1)
            tx = (xp - x0).astype(np.float32)
            ty = (yp - y0).astype(np.float32)
            v00 = arr[y0, x0]
            v10 = arr[y0, x1]
            v01 = arr[y1, x0]
            v11 = arr[y1, x1]
            finite = np.isfinite(v00) & np.isfinite(v10) & np.isfinite(v01) & np.isfinite(v11)
            vals = (v00 * (1.0 - tx) + v10 * tx) * (1.0 - ty) + (v01 * (1.0 - tx) + v11 * tx) * ty
            ok &= finite
            out[start:end] = np.where(finite, vals, 1.0).astype(np.float32)
            valid_out[start:end] = ok
        return out, valid_out

    @staticmethod
    def _source_snap_params(layer_info):
        """Return source-raster affine parameters used to color dense scatter."""
        source_path = str(layer_info.get("source_path", "") or "")
        if not source_path or rasterio is None:
            return None
        try:
            with rasterio.open(source_path) as src:
                return tuple(src._inv_gt), tuple(src._gt)
        except Exception:
            return None

    @staticmethod
    def _snap_xy_to_source_centers(x, y, snap_params):
        """Snap XY arrays to source pixel centers without allocating full XYZ copies."""
        if snap_params is None:
            return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
        inv, gt = snap_params
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        colf = inv[0] + x * inv[1] + y * inv[2]
        rowf = inv[3] + x * inv[4] + y * inv[5]
        col = np.floor(colf)
        row = np.floor(rowf)
        cx = gt[0] + (col + 0.5) * gt[1] + (row + 0.5) * gt[2]
        cy = gt[3] + (col + 0.5) * gt[4] + (row + 0.5) * gt[5]
        return cx, cy

    def _imagery_rgb_for_layer(self, layer_info, xyz):
        """Sample imagery with synthetic Dense points inheriting source-pixel RGB."""
        pts = np.asarray(xyz)
        n = int(pts.shape[0]) if pts.ndim == 2 else 0
        if n <= 0:
            return None, None
        rgb = np.zeros((n, 3), dtype=np.float32)
        valid = np.zeros((n,), dtype=bool)
        native_n = max(0, min(n, int(layer_info.get("native_count", n))))

        if native_n > 0:
            r, v = self._imagery_rgb_for_xyz(pts[:native_n])
            if r is None or v is None:
                return None, None
            rgb[:native_n] = r
            valid[:native_n] = v

        if native_n < n:
            snap = self._source_snap_params(layer_info)
            chunk = int(IMAGERY_SAMPLE_CHUNK)
            for start in range(native_n, n, chunk):
                end = min(n, start + chunk)
                part = pts[start:end]
                cx, cy = self._snap_xy_to_source_centers(part[:, 0], part[:, 1], snap)
                sample_pts = np.empty((end - start, 3), dtype=np.float64)
                sample_pts[:, 0] = cx
                sample_pts[:, 1] = cy
                sample_pts[:, 2] = part[:, 2]
                r, v = self._imagery_rgb_for_xyz(sample_pts)
                if r is None or v is None:
                    continue
                rgb[start:end] = r
                valid[start:end] = v
        return rgb, valid


    def _ensure_imagery_cache(self):
        """Render the selected QGIS aerial/basemap once over the 3D footprint."""
        source = self._resolve_imagery_layer()
        extent = self._preview_xy_extent()
        crs = self._last_preview_crs
        if source is None or extent is None or crs is None or not crs.isValid():
            if not self._imagery_missing_warned:
                self._imagery_missing_warned = True
                iface.messageBar().pushMessage(
                    "Aerial RGB",
                    "Imagery RGB is enabled but no valid imagery layer is selected. "
                    "Choose the exact raster/XYZ/WMS/WMTS layer from the Imagery dropdown.",
                    level=Qgis.Warning,
                    duration=7,
                )
            return False

        ew = max(float(extent.width()), 1e-12)
        eh = max(float(extent.height()), 1e-12)
        detail_name = str(self.detail_combo.currentText() or "Auto")
        max_dim = IMAGERY_MAX_DIM_HIGH if detail_name in ("High", "Ultra", "Maximum") else IMAGERY_MAX_DIM_AUTO
        min_dim = 192
        if ew >= eh:
            out_w = max_dim
            out_h = max(min_dim, int(round(max_dim * eh / ew)))
        else:
            out_h = max_dim
            out_w = max(min_dim, int(round(max_dim * ew / eh)))
        out_w = max(64, min(max_dim, out_w))
        out_h = max(64, min(max_dim, out_h))
        # Hard pixel cap protects WMS/XYZ requests and temporary QImage memory.
        pixels = int(out_w) * int(out_h)
        if pixels > int(IMAGERY_MAX_PIXELS):
            scale = math.sqrt(float(IMAGERY_MAX_PIXELS) / float(max(1, pixels)))
            out_w = max(64, int(math.floor(out_w * scale)))
            out_h = max(64, int(math.floor(out_h * scale)))

        key = (
            str(source.id()),
            str(crs.authid() or crs.toWkt()),
            round(float(extent.xMinimum()), 6), round(float(extent.yMinimum()), 6),
            round(float(extent.xMaximum()), 6), round(float(extent.yMaximum()), 6),
            int(out_w), int(out_h),
        )
        if self._imagery_cache_key == key and self._imagery_cache_rgba is not None:
            return True

        old_cursor = False
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            old_cursor = True
        except Exception:
            pass
        try:
            settings = QgsMapSettings()
            settings.setLayers([source])
            settings.setDestinationCrs(crs)
            settings.setExtent(extent)
            settings.setOutputSize(QSize(int(out_w), int(out_h)))
            settings.setBackgroundColor(QColor(0, 0, 0, 0))
            try:
                settings.setTransformContext(QgsProject.instance().transformContext())
            except Exception:
                pass

            job = QgsMapRendererParallelJob(settings)
            job.start()
            job.waitForFinished()
            image = job.renderedImage()
            rgba = self._qimage_to_rgba_numpy(image)
            if rgba is None:
                raise RuntimeError("QGIS returned an empty imagery render")

            actual_extent = settings.visibleExtent()
            self._imagery_cache_key = key
            self._imagery_cache_rgba = rgba
            self._imagery_cache_extent = QgsRectangle(actual_extent)
            self._imagery_cache_source_id = str(source.id())
            self._imagery_cache_source_name = str(source.name())
            self._imagery_missing_warned = False
            return True
        except Exception as exc:
            self._imagery_cache_key = None
            self._imagery_cache_rgba = None
            self._imagery_cache_extent = None
            log_exception("Could not render aerial imagery for RGB coloring", exc, Qgis.Warning)
            iface.messageBar().pushMessage(
                "Aerial RGB",
                f"Could not render imagery layer '{source.name()}': {exc}",
                level=Qgis.Warning,
                duration=7,
            )
            return False
        finally:
            if old_cursor:
                try:
                    QApplication.restoreOverrideCursor()
                except Exception:
                    pass

    def _imagery_rgb_for_xyz(self, xyz: np.ndarray):
        if not self._ensure_imagery_cache():
            return None, None
        rgba = self._imagery_cache_rgba
        extent = self._imagery_cache_extent
        if rgba is None or extent is None:
            return None, None

        xyz = np.asarray(xyz)
        if xyz.ndim != 2 or xyz.shape[0] == 0:
            return None, None
        ew = max(float(extent.width()), 1e-12)
        eh = max(float(extent.height()), 1e-12)
        x = xyz[:, 0].astype(np.float64, copy=False)
        y = xyz[:, 1].astype(np.float64, copy=False)
        fx = (x - float(extent.xMinimum())) / ew
        fy = (float(extent.yMaximum()) - y) / eh
        return self._bilinear_rgba_sample(rgba, fx, fy)


    def _ensure_relief_cache_for_layer(self, layer_info):
        """Build a bounded display-only relief grid from the native elevation raster."""
        source_path = str(layer_info.get("source_path", "") or "")
        extent = self._preview_xy_extent()
        if not source_path or extent is None or rasterio is None:
            return None
        try:
            with rasterio.open(source_path) as src:
                w = src.window(
                    float(extent.xMinimum()), float(extent.yMinimum()),
                    float(extent.xMaximum()), float(extent.yMaximum()),
                ).round_offsets().round_lengths()
                width = max(1, int(w.width))
                height = max(1, int(w.height))
                if width <= 1 or height <= 1:
                    return None

                if width >= height:
                    out_w = min(int(RELIEF_MAX_DIM), width)
                    out_h = max(2, int(round(out_w * height / float(width))))
                else:
                    out_h = min(int(RELIEF_MAX_DIM), height)
                    out_w = max(2, int(round(out_h * width / float(height))))
                out_w = max(2, min(int(RELIEF_MAX_DIM), out_w))
                out_h = max(2, min(int(RELIEF_MAX_DIM), out_h))

                key = (
                    source_path,
                    int(w.col_off), int(w.row_off), int(w.width), int(w.height),
                    int(out_w), int(out_h),
                )
                cached = self._relief_cache.get(key)
                if cached is not None:
                    return cached

                arr = src.read(1, window=w, out_shape=(out_h, out_w), resampling=Resampling.nearest, masked=True)
                data = np.asarray(np.ma.getdata(arr), dtype=np.float64)
                valid = ~np.asarray(np.ma.getmaskarray(arr), dtype=bool)
                valid &= np.isfinite(data)
                if not np.any(valid):
                    return None

                # Affine for the reduced grid. Gradients are calculated in grid
                # coordinates then transformed into world X/Y, so rotated rasters
                # are handled correctly.
                tf = src.window_transform(w)
                tf = tf * Affine.scale(width / float(out_w), height / float(out_h))
                safe = np.where(valid, data, np.nan)
                with np.errstate(invalid="ignore", divide="ignore"):
                    dz_dr, dz_dc = np.gradient(safe)
                det = float(tf.a * tf.e - tf.b * tf.d)
                if abs(det) <= 1e-20:
                    return None
                gx = (tf.e * dz_dc - tf.d * dz_dr) / det
                gy = (-tf.b * dz_dc + tf.a * dz_dr) / det
                norm = np.sqrt(gx * gx + gy * gy + 1.0)
                nx = -gx / norm
                ny = -gy / norm
                nz = 1.0 / norm

                az = math.radians(float(RELIEF_LIGHT_AZIMUTH_DEG))
                el = math.radians(float(RELIEF_LIGHT_ELEVATION_DEG))
                lx = math.sin(az) * math.cos(el)
                ly = math.cos(az) * math.cos(el)
                lz = math.sin(el)
                dot = nx * lx + ny * ly + nz * lz
                # Neutral-ish directional relief. Values around 1 preserve the
                # original color; slopes facing away darken and facing the light
                # brighten slightly. No geometry or Z is modified.
                shade = 0.35 + 0.85 * np.clip(dot, 0.0, 1.0)
                shade = np.clip(shade, 0.35, 1.20).astype(np.float32)
                shade[~np.isfinite(shade)] = np.nan
                shade[~valid] = np.nan

                inv_tf = _inverse_gt((tf.c, tf.a, tf.b, tf.f, tf.d, tf.e))
                cached = {
                    "shade": shade,
                    "inv_tf": inv_tf,
                    "width": int(out_w),
                    "height": int(out_h),
                }
                # Keep only a handful of current preview entries even with many
                # surfaces. This avoids unbounded RAM growth across redraws.
                if len(self._relief_cache) >= 12:
                    try:
                        self._relief_cache.pop(next(iter(self._relief_cache)))
                    except Exception:
                        self._relief_cache.clear()
                self._relief_cache[key] = cached
                return cached
        except Exception as exc:
            log_exception("Could not calculate relief shading", exc, Qgis.Info)
            return None

    def _relief_for_layer_xyz(self, layer_info, xyz):
        cache = self._ensure_relief_cache_for_layer(layer_info)
        pts = np.asarray(xyz)
        n = int(pts.shape[0]) if pts.ndim == 2 else 0
        if cache is None or n <= 0:
            return np.ones((n,), dtype=np.float32), np.zeros((n,), dtype=bool)
        inv = cache["inv_tf"]
        w = max(1, int(cache["width"]))
        h = max(1, int(cache["height"]))
        shade_out = np.ones((n,), dtype=np.float32)
        valid_out = np.zeros((n,), dtype=bool)
        native_n = max(0, min(n, int(layer_info.get("native_count", n))))
        snap = self._source_snap_params(layer_info) if native_n < n else None
        chunk = int(RELIEF_SAMPLE_CHUNK)
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            part = pts[start:end]
            x = part[:, 0].astype(np.float64, copy=False)
            y = part[:, 1].astype(np.float64, copy=False)
            if start >= native_n and snap is not None:
                x, y = self._snap_xy_to_source_centers(x, y, snap)
            elif start < native_n < end and snap is not None:
                # Rare boundary chunk: split only its synthetic tail.
                x = np.asarray(x, dtype=np.float64).copy()
                y = np.asarray(y, dtype=np.float64).copy()
                k = native_n - start
                cx, cy = self._snap_xy_to_source_centers(x[k:], y[k:], snap)
                x[k:] = cx
                y[k:] = cy
            colf = inv[0] + x * inv[1] + y * inv[2]
            rowf = inv[3] + x * inv[4] + y * inv[5]
            fx = (colf - 0.5) / max(1.0, float(w - 1))
            fy = (rowf - 0.5) / max(1.0, float(h - 1))
            sv, vv = self._bilinear_scalar_sample(cache["shade"], fx, fy, chunk_size=chunk)
            shade_out[start:end] = sv
            valid_out[start:end] = vv
        return shade_out, valid_out


    def _apply_relief_to_rgb(self, layer_info, xyz, rgb):
        if not bool(self.relief_chk.isChecked()):
            return np.asarray(rgb, dtype=np.float32)
        strength = max(0.0, min(1.0, float(self.relief_strength_spin.value())))
        if strength <= 1e-6:
            return np.asarray(rgb, dtype=np.float32)
        shade, valid = self._relief_for_layer_xyz(layer_info, xyz)
        out = np.asarray(rgb, dtype=np.float32).copy()
        if shade.size != out.shape[0] or valid.size != out.shape[0] or not np.any(valid):
            return out
        multiplier = 1.0 + strength * (shade - 1.0)
        out[valid] = np.clip(out[valid] * multiplier[valid, None], 0.0, 1.0)
        return out

    @staticmethod
    def _qc_xyz_for_layer(layer_info):
        xyz = layer_info.get("qc_xyz", None)
        if xyz is None:
            xyz = layer_info.get("xyz", np.empty((0, 3)))
        return np.asarray(xyz, dtype=np.float32)

    def _pick_alpha_for_layer(self, layer_info, zrange):
        xyz = self._qc_xyz_for_layer(layer_info)
        n = int(xyz.shape[0]) if xyz.ndim == 2 else 0
        alpha = np.ones((n,), dtype=np.float32)
        if n and bool(self.clip_range_chk.isChecked()) and zrange is not None:
            z0, z1 = float(zrange[0]), float(zrange[1])
            zs = xyz[:, 2].astype(float)
            alpha[(~np.isfinite(zs)) | (zs < z0) | (zs > z1)] = 0.0
        return alpha

    def _z_sample(self):
        parts = []
        layers = list(self._last_cloud_layers or [])
        for L in layers:
            if not bool(L.get("visible", True)):
                continue
            try:
                xyz = self._qc_xyz_for_layer(L)
                if xyz.size == 0:
                    continue
                z = xyz[:, 2].astype(float)
                z = z[np.isfinite(z)]
                if z.size == 0:
                    continue
                if z.size > 150_000:
                    step = max(1, int(np.ceil(float(z.size) / 150_000.0)))
                    z = z[::step]
                parts.append(z)
            except Exception:
                continue
        if not parts:
            return np.empty((0,), dtype=float)
        z = np.concatenate(parts)
        if z.size > 500_000:
            step = max(1, int(np.ceil(float(z.size) / 500_000.0)))
            z = z[::step]
        return z

    def _current_display_range(self):
        z = self._z_sample()
        if z.size == 0:
            return None
        if bool(self.auto_pct_chk.isChecked()):
            lo = max(0.0, min(49.9, float(self.p_lo.value())))
            hi = max(50.1, min(100.0, float(self.p_hi.value())))
            z0 = float(np.percentile(z, lo))
            z1 = float(np.percentile(z, hi))
        else:
            z0 = float(self.zmin_spin.value())
            z1 = float(self.zmax_spin.value())
        if not np.isfinite(z0) or not np.isfinite(z1) or z1 <= z0:
            z0 = float(np.nanmin(z))
            z1 = float(np.nanmax(z))
        if not np.isfinite(z0) or not np.isfinite(z1) or z1 <= z0:
            return None
        return z0, z1

    def _layer_rgba(self, layer_info, index: int, z0: float, z1: float):
        xyz = np.asarray(layer_info.get("xyz", np.empty((0, 3))), dtype=np.float32)
        n = int(xyz.shape[0]) if xyz.ndim == 2 else 0
        if n <= 0:
            return np.empty((0, 4), dtype=np.float32)
        zs = xyz[:, 2].astype(float)
        denom = max(float(z1 - z0), 1e-12)
        norm = np.clip((zs - float(z0)) / denom, 0.0, 1.0)
        if bool(self.invert_chk.isChecked()):
            norm = 1.0 - norm

        mode = str(self.cmap_combo.currentText() or "").strip().lower()
        base = np.asarray(layer_info.get("base_rgb", surface_rgb(index)), dtype=np.float32).reshape((1, 3))
        if bool(self.imagery_chk.isChecked()):
            sampled_rgb, sampled_valid = self._imagery_rgb_for_layer(layer_info, xyz)
            rgb = np.tile(base, (n, 1)).astype(np.float32)
            if sampled_rgb is not None and sampled_valid is not None:
                rgb[sampled_valid] = sampled_rgb[sampled_valid]
            else:
                # Graceful fallback keeps the surface visible even when an
                # online imagery provider is unavailable.
                brightness = (0.38 + 0.62 * norm).astype(np.float32).reshape((-1, 1))
                rgb = np.clip(base * brightness, 0.0, 1.0)
        elif mode.startswith("surface +"):
            # Preserve source identity while adding elevation brightness. True
            # directional relief is applied below when Relief is enabled.
            brightness = (0.38 + 0.62 * norm).astype(np.float32).reshape((-1, 1))
            rgb = np.clip(base * brightness, 0.0, 1.0)
        elif mode.startswith("surface colors"):
            rgb = np.tile(base, (n, 1)).astype(np.float32)
        elif "gray" in mode:
            rgb = np.stack([norm, norm, norm], axis=1).astype(np.float32)
        else:
            rgb = height_to_rgb(norm).astype(np.float32)

        # Directional relief is purely display shading derived from the native
        # raster grid. It never modifies coordinates, Z, measurements or QC.
        rgb = self._apply_relief_to_rgb(layer_info, xyz, rgb)

        alpha = np.full((n, 1), float(self.opacity_spin.value()), dtype=np.float32)
        if bool(self.clip_range_chk.isChecked()):
            outside = (~np.isfinite(zs)) | (zs < float(z0)) | (zs > float(z1))
            alpha[outside, 0] = 0.0
        rgba = np.hstack([rgb, alpha]).astype(np.float32)
        bad = ~np.isfinite(rgba).all(axis=1)
        if np.any(bad):
            rgba[bad, :3] = 1.0
            rgba[bad, 3] = 0.0 if bool(self.clip_range_chk.isChecked()) else float(self.opacity_spin.value())
        return rgba

    def _update_qc_stats(self):
        layers = list(self._last_cloud_layers or [])
        if not layers:
            self.stats_label.setText("Draw a box or cross-section to calculate elevation statistics.")
            return
        lines = []
        total = 0
        rendered_total = 0
        synthetic_total = 0
        for idx, L in enumerate(layers):
            try:
                xyz = self._qc_xyz_for_layer(L)
                if xyz.size == 0:
                    continue
                z = xyz[:, 2].astype(float)
                z = z[np.isfinite(z)]
                if z.size == 0:
                    continue
                total += int(z.size)
                render_xyz = np.asarray(L.get("xyz", np.empty((0, 3))), dtype=np.float32)
                rendered_total += int(render_xyz.shape[0]) if render_xyz.ndim == 2 else 0
                synthetic_total += int(L.get("synthetic_count", max(0, (int(render_xyz.shape[0]) if render_xyz.ndim == 2 else 0) - int(z.size))))
                sample = z
                if sample.size > 200_000:
                    step = max(1, int(np.ceil(float(sample.size) / 200_000.0)))
                    sample = sample[::step]
                p02, med, p98 = np.percentile(sample, [2.0, 50.0, 98.0])
                state = "ON" if bool(L.get("visible", True)) else "OFF"
                name = str(L.get("name", f"Surface {idx + 1}"))
                lines.append(
                    f"{state}  {name}: min {np.min(sample):.3f} | P02 {p02:.3f} | "
                    f"median {med:.3f} | P98 {p98:.3f} | max {np.max(sample):.3f}"
                )
            except Exception:
                continue
        zrange = self._current_display_range()
        if zrange is not None:
            lines.append(f"Display range: {zrange[0]:.3f} to {zrange[1]:.3f} | Native QC samples: {total:,}")
        if rendered_total:
            surface_mode = any(str(L.get("render_style", "points")).lower() == "surface" for L in layers)
            if surface_mode:
                lines.append(f"Rendered mesh vertices: {rendered_total:,} | QC remains native raster samples")
            elif synthetic_total > 0:
                lines.append(f"Rendered points: {rendered_total:,} = {rendered_total - synthetic_total:,} native + {synthetic_total:,} synthetic (visual only)")
            else:
                lines.append(f"Rendered points: {rendered_total:,} native")
        if bool(self.robust_focus_chk.isChecked()):
            lines.append("Outlier Guard: camera fit uses the display range; extreme Z cells do not control zoom.")
        if bool(self.imagery_chk.isChecked()):
            if self._imagery_cache_source_name:
                lines.append(f"Aerial RGB source: {self._imagery_cache_source_name}")
            else:
                selected_name = str(self.imagery_combo.currentText() or "").strip()
                lines.append(f"Aerial RGB source: {selected_name or 'not selected'}")
        self.stats_label.setText("\n".join(lines) if lines else "No valid elevation cells in the current preview.")

    def set_preview_cloud(
        self,
        xyz: np.ndarray,
        preset: str = "iso",
        az=None,
        el=None,
        reset_camera: bool = True,
    ):
        xyz = np.asarray(xyz, dtype=np.float32) if xyz is not None else np.empty((0, 3), dtype=np.float32)
        rgb = surface_rgb(0)
        layer = {
            "xyz": xyz,
            "qc_xyz": xyz,
            "pick_xyz": xyz,
            "native_count": int(xyz.shape[0]),
            "synthetic_count": 0,
            "base_rgb": rgb,
            "rgba": solid_rgba(int(xyz.shape[0]), rgb[0], rgb[1], rgb[2], float(self.opacity_spin.value())),
            "layer_id": "__single__",
            "name": "Surface",
            "visible": True,
        }
        self.set_preview_cloud_layers([layer], preset=preset, az=az, el=el, reset_camera=reset_camera)

    def set_preview_cloud_layers(
        self,
        layers,
        preset: str = "iso",
        az=None,
        el=None,
        reset_camera: bool = True,
    ):
        self._last_xyz_full = None
        self._last_cloud_layers = list(layers or [])
        self._last_preset = preset or "iso"
        self._last_az = az
        self._last_el = el
        self._last_colors = None
        self._last_color_key = None
        self._applied_color_key = None
        self._invalidate_imagery_cache()
        self._set_color_controls_enabled(True)

        for idx, L in enumerate(self._last_cloud_layers):
            L.setdefault("base_rgb", surface_rgb(idx))
            L.setdefault("layer_id", str(L.get("name", idx)))
            L.setdefault("name", f"Surface {idx + 1}")
            L.setdefault("visible", True)
            L.setdefault("render_style", "points")
            L.setdefault("qc_xyz", L.get("xyz", np.empty((0, 3))))
            L.setdefault("pick_xyz", L.get("qc_xyz"))
            L.setdefault("native_count", int(np.asarray(L.get("qc_xyz", np.empty((0, 3)))).shape[0]))
            L.setdefault("synthetic_count", max(0, int(np.asarray(L.get("xyz", np.empty((0, 3)))).shape[0]) - int(L.get("native_count", 0))))

        z_all = []
        for L in self._last_cloud_layers:
            try:
                xyz = self._qc_xyz_for_layer(L)
                if xyz.size:
                    z = xyz[:, 2].astype(float)
                    z = z[np.isfinite(z)]
                    if z.size:
                        z_all.append(z)
            except Exception:
                pass
        if z_all:
            zmin = min(float(np.min(z)) for z in z_all)
            zmax = max(float(np.max(z)) for z in z_all)
            self.zmin_spin.blockSignals(True)
            self.zmax_spin.blockSignals(True)
            self.zmin_spin.setValue(zmin)
            self.zmax_spin.setValue(zmax)
            self.zmin_spin.blockSignals(False)
            self.zmax_spin.blockSignals(False)

        cam_state = None
        if not reset_camera:
            try:
                cam_state = self.preview.get_camera_state()
            except Exception:
                cam_state = None

        zrange = self._current_display_range()
        if zrange is not None:
            for idx, L in enumerate(self._last_cloud_layers):
                L["rgba"] = self._layer_rgba(L, idx, zrange[0], zrange[1])
                L["pick_alpha"] = self._pick_alpha_for_layer(L, zrange)
        else:
            for L in self._last_cloud_layers:
                L["pick_alpha"] = self._pick_alpha_for_layer(L, None)

        self.preview.set_cloud_layers(
            layers=self._last_cloud_layers,
            size_px=float(self.size_spin.value()),
            reset_camera=False,
            preset=self._last_preset,
            azimuth_deg=self._last_az,
            elevation_deg=self._last_el,
        )
        self.preview.set_z_exaggeration(float(self.zex_spin.value()))
        if bool(self.robust_focus_chk.isChecked()) and zrange is not None:
            self.preview.set_focus_z_range(*zrange)
        else:
            self.preview.set_focus_z_range(None, None)

        if reset_camera:
            self.preview.reset_camera(
                preset=self._last_preset,
                azimuth_deg=self._last_az,
                elevation_deg=self._last_el,
            )
        elif cam_state:
            self.preview.restore_camera_state(cam_state)
        self._update_qc_stats()
        self._refresh_vector_overlays()

    def update_preview_realtime(self, *args, reset_camera: bool = False):
        if getattr(self.preview, "_interacting", False):
            self._preview_pending_redraw = True
            self._pending_reset_cam = bool(self._pending_reset_cam) or bool(reset_camera)
            return
        self._preview_pending_redraw = False

        layers = list(self._last_cloud_layers or [])
        if not layers:
            return

        zrange = self._current_display_range()
        if zrange is not None:
            rgba_layers = []
            pick_alpha_layers = []
            for idx, L in enumerate(layers):
                rgba = self._layer_rgba(L, idx, zrange[0], zrange[1])
                L["rgba"] = rgba
                rgba_layers.append(rgba)
                pa = self._pick_alpha_for_layer(L, zrange)
                L["pick_alpha"] = pa
                pick_alpha_layers.append(pa)
            try:
                self.preview.set_layer_colors(rgba_layers)
                self.preview.set_layer_pick_alpha(pick_alpha_layers)
            except Exception:
                pass

        try:
            self.preview.set_point_size(float(self.size_spin.value()))
            self.preview.set_adaptive_point_size_enabled(bool(self.adaptive_points_chk.isChecked()))
        except Exception:
            pass
        try:
            self.preview.set_z_exaggeration(float(self.zex_spin.value()))
        except Exception:
            pass

        if bool(self.robust_focus_chk.isChecked()) and zrange is not None:
            self.preview.set_focus_z_range(*zrange)
        else:
            self.preview.set_focus_z_range(None, None)

        if reset_camera:
            try:
                self.preview.reset_camera(
                    preset=self._last_preset,
                    azimuth_deg=self._last_az,
                    elevation_deg=self._last_el,
                )
            except Exception:
                pass
        self._update_qc_stats()

    def _on_preview_interaction_finished(self):
        try:
            if hasattr(self, "_update_timer") and self._update_timer.isActive():
                self._update_timer.stop()
                self._apply_display_update()
                return
        except Exception:
            pass
        if getattr(self, "_preview_pending_redraw", False):
            try:
                self._apply_display_update()
            except Exception:
                pass

    def on_draw(self):
        if not self._dependencies_ok:
            iface.messageBar().pushMessage(
                "Terrain Viewer", dependency_error_text(), level=Qgis.Critical, duration=7
            )
            return

        rasters = self.get_surface_layers(fallback=True)
        if not rasters:
            iface.messageBar().pushMessage(
                "Terrain Viewer", "Add at least one DEM/DTM surface to the viewer first.", level=Qgis.Warning, duration=4
            )
            return

        try:
            ensure_same_raster_crs(rasters)
            for layer in rasters:
                raster_source_path(layer)
        except Exception as exc:
            iface.messageBar().pushMessage(
                "Terrain Viewer", str(exc), level=Qgis.Warning, duration=7
            )
            return

        self._deactivate_map_tools()
        iface.mapCanvas().setMapTool(self.rect_tool)
        self.rect_tool.enable()
        iface.mainWindow().statusBar().showMessage(
            "Draw a rectangle on the DEM to build the 3D preview."
        )

    def on_measure(self):
        if self._measure_active:
            self._measure_active = False
            self._measure_points_3d = []
            self._measure_pick_meta = []
            self.preview.set_pick_mode(False)
            self.measure_btn.setText("Measure in 3D")
            self.measure_result_label.setText("3D measurement canceled.")
            return

        layers = [L for L in list(self._last_cloud_layers or []) if bool(L.get("visible", True))]
        if not layers:
            iface.messageBar().pushMessage(
                "Measure in 3D", "Draw a 3D area or cross-section first.", level=Qgis.Info, duration=4
            )
            return
        self._deactivate_map_tools()
        self._measure_points_3d = []
        self._measure_pick_meta = []
        self._measure_active = True
        self.preview.clear_annotations()
        self.preview.set_pick_mode(True)
        self.measure_btn.setText("Cancel measure")
        self.measure_result_label.setText(
            "<b>Measure in 3D:</b> click the first rendered point, then the second. "
            "The nearest visible DEM point to each click is used."
        )

    def _on_3d_point_picked(self, pick):
        if not self._measure_active or not isinstance(pick, dict):
            return
        try:
            world = np.asarray(pick.get("world"), dtype=float).reshape((3,))
        except Exception:
            return
        if not np.all(np.isfinite(world)):
            return
        self._measure_points_3d.append(world)
        self._measure_pick_meta.append(str(pick.get("layer_name", "Surface")))
        self.preview.show_measurement(self._measure_points_3d)

        if len(self._measure_points_3d) == 1:
            self.measure_result_label.setText(
                f"P1 [{self._measure_pick_meta[0]}]: X {world[0]:,.3f}, Y {world[1]:,.3f}, Z {world[2]:,.3f}. Click P2."
            )
            return

        p1 = np.asarray(self._measure_points_3d[0], dtype=float)
        p2 = np.asarray(self._measure_points_3d[1], dtype=float)
        dxy = p2[:2] - p1[:2]
        horizontal = float(np.hypot(dxy[0], dxy[1]))
        dz = float(p2[2] - p1[2])
        dist3d = float(np.sqrt(horizontal * horizontal + dz * dz))
        slope_pct = (dz / horizontal * 100.0) if horizontal > 0 else float("inf")
        angle = float(np.degrees(np.arctan2(dz, horizontal))) if horizontal > 0 else 90.0
        unit = str(self._last_preview_unit or "map units")
        self.measure_result_label.setText(
            f"<b>3D measurement</b> [{self._measure_pick_meta[0]} → {self._measure_pick_meta[1]}] — H: {horizontal:,.3f} {unit} | ΔZ: {dz:+,.3f} {unit} | "
            f"3D: {dist3d:,.3f} {unit}<br>"
            f"Slope: {slope_pct:+.2f}% | Angle: {angle:+.2f}° | "
            f"Z1/Z2: {p1[2]:,.3f} / {p2[2]:,.3f}"
        )
        self._measure_active = False
        self.preview.set_pick_mode(False)
        self.measure_btn.setText("Measure in 3D")

    def on_cs(self):
        if not self._dependencies_ok:
            iface.messageBar().pushMessage(
                "Cross-section", dependency_error_text(), level=Qgis.Critical, duration=7
            )
            return

        layers = self.get_surface_layers(fallback=True)
        if not layers:
            iface.messageBar().pushMessage(
                "Cross-section",
                "Add at least one DEM/DTM surface to the viewer first.",
                level=Qgis.Warning,
                duration=4,
            )
            return

        try:
            ensure_same_raster_crs(layers)
            first = layers[0]
            src_path = raster_source_path(first)
            with rasterio.open(src_path) as src:
                rx, ry = (abs(float(v)) for v in src.res)
            base_res = max(min(rx, ry), 1e-12)
        except Exception as exc:
            log_exception("Could not inspect cross-section surfaces", exc, Qgis.Critical)
            iface.messageBar().pushMessage("Cross-section", str(exc), level=Qgis.Critical, duration=7)
            return

        width = float(self.cs_width_spin.value())
        if not np.isfinite(width) or width <= 0:
            iface.messageBar().pushMessage(
                "Cross-section", "Cross-section width must be greater than zero.", level=Qgis.Warning, duration=4
            )
            return

        self._deactivate_map_tools()
        self.cs_tool = CrossSectionTool(
            iface.mapCanvas(), width=width, layers=layers, callback=self.run_cs_3d
        )
        iface.mapCanvas().setMapTool(self.cs_tool)
        iface.mainWindow().statusBar().showMessage(
            f"Cross-section through {len(layers)} surface(s), width {width:g}: click first point."
        )

    def run_cs_3d(self, p1, p2, width: float, layers):
        layers = [lyr for lyr in list(layers or []) if is_raster_layer(lyr)]
        if not layers:
            iface.messageBar().pushMessage(
                "Cross-section", "The selected surfaces are no longer available.",
                level=Qgis.Warning, duration=5
            )
            return

        # Preserve the exact canvas-space transect so render-style changes can
        # rebuild the same cross-section without asking the user to redraw it.
        self._last_cs_canvas_p1 = QgsPointXY(p1)
        self._last_cs_canvas_p2 = QgsPointXY(p2)

        try:
            ensure_same_raster_crs(layers)
            first = layers[0]
            p1_layer = canvas_to_layer_point(p1, first)
            p2_layer = canvas_to_layer_point(p2, first)
            dx = p2_layer.x() - p1_layer.x()
            dy = p2_layer.y() - p1_layer.y()
            length = float(np.hypot(dx, dy))
            if not np.isfinite(length) or length <= 0:
                raise ValueError("Cross-section start and end points must be different.")

            surface_mode = self.render_style_key() == "surface"
            src_paths = []
            sample_specs = []
            for layer in layers:
                src_path = raster_source_path(layer)
                with rasterio.open(src_path) as src:
                    rx, ry = (abs(float(v)) for v in src.res)
                sample_step = max(min(rx, ry), 1e-12)
                dist_samples = min(int(MAX_CS_DISTANCE_SAMPLES), max(2, int(np.ceil(length / sample_step)) + 1))
                width_samples = min(int(MAX_CS_WIDTH_SAMPLES), max(1, int(np.ceil(float(width) / sample_step)) + 1))
                if width_samples > 1 and width_samples % 2 == 0:
                    width_samples = min(int(MAX_CS_WIDTH_SAMPLES), width_samples + 1)
                total = dist_samples * width_samples
                if total > int(MAX_CS_TOTAL_SAMPLES):
                    dist_samples = max(2, int(MAX_CS_TOTAL_SAMPLES) // max(1, width_samples))
                    total = dist_samples * width_samples
                if total > int(MAX_CS_TOTAL_SAMPLES):
                    width_samples = max(1, int(MAX_CS_TOTAL_SAMPLES) // max(2, dist_samples))
                    if width_samples > 1 and width_samples % 2 == 0:
                        width_samples = max(1, width_samples - 1)
                if surface_mode and dist_samples > 1 and width_samples > 1:
                    max_vertices = max(6, int(self.detail_cross_section_cap()))
                    target_quads = max(1, max_vertices // 6)
                    quads = max(1, (dist_samples - 1) * (width_samples - 1))
                    if quads > target_quads:
                        scale = math.sqrt(quads / float(target_quads))
                        dist_samples = max(2, int(math.floor((dist_samples - 1) / scale)) + 1)
                        width_samples = max(2, int(math.floor((width_samples - 1) / scale)) + 1)
                src_paths.append(src_path)
                sample_specs.append((dist_samples, width_samples))

        except Exception as exc:
            log_exception("Could not prepare multi-surface cross-section", exc, Qgis.Critical)
            iface.messageBar().pushMessage("Cross-section", str(exc), level=Qgis.Critical, duration=7)
            return

        self._cancel_all_tasks(detach=True)
        self.progress_start("Building cross-section")
        task = BuildMultiCrossSectionTask(
            title="Build multi-surface 3D cross-section",
            src_paths=src_paths,
            p1=p1_layer,
            p2=p2_layer,
            width=width,
            sample_specs=sample_specs,
            surface_mode=surface_mode,
        )
        self._active_cs_task = task

        def _progress_changed(pct):
            if task is self._active_cs_task:
                self.progress_set(pct)

        def _finished(ok: bool):
            if task is not self._active_cs_task:
                return
            try:
                if not ok:
                    msg = task.error_msg or "Task failed"
                    level_msg = Qgis.Info if msg == "Canceled" else Qgis.Critical
                    iface.messageBar().pushMessage("Cross-section", msg, level=level_msg, duration=5)
                    return

                xyz_list = task.xyz_list or []
                qc_xyz_list = getattr(task, "qc_xyz_list", None) or []
                draw_layers = []
                for idx, xyz in enumerate(xyz_list):
                    if xyz is None or xyz.size == 0 or idx >= len(layers):
                        continue
                    xyz_draw = np.asarray(xyz)
                    n = int(xyz_draw.shape[0])
                    cs_cap = int(self.detail_cross_section_cap())
                    if n > cs_cap:
                        if surface_mode:
                            tri = xyz_draw.reshape((-1, 3, 3))
                            keep_tri = max(1, cs_cap // 3)
                            step = max(1, int(math.ceil(tri.shape[0] / float(keep_tri))))
                            xyz_draw = np.ascontiguousarray(tri[::step][:keep_tri].reshape((-1, 3)))
                        else:
                            xyz_draw = cap_points_grid_order(xyz_draw, cs_cap)
                        n = int(xyz_draw.shape[0])
                    qc_xyz = qc_xyz_list[idx] if idx < len(qc_xyz_list) else xyz_draw
                    lyr = layers[idx]
                    rgb = self.surface_rgb_for_layer(str(lyr.id()), idx)
                    draw_layers.append({
                        "xyz": xyz_draw,
                        "qc_xyz": np.asarray(qc_xyz),
                        "pick_xyz": np.asarray(qc_xyz),
                        "native_count": int(np.asarray(qc_xyz).shape[0]),
                        "synthetic_count": 0,
                        "render_style": "surface" if surface_mode else "points",
                        "rgba": solid_rgba(n, rgb[0], rgb[1], rgb[2], float(self.opacity_spin.value())),
                        "base_rgb": rgb,
                        "layer_id": str(lyr.id()),
                        "name": str(lyr.name()),
                        "source_path": src_paths[idx] if idx < len(src_paths) else "",
                        "visible": True,
                    })

                if not draw_layers:
                    iface.messageBar().pushMessage(
                        "Cross-section", "No valid DEM samples were found along this cross-section.",
                        level=Qgis.Warning, duration=5
                    )
                    return

                line_azimuth = float(np.degrees(np.arctan2(dy, dx)))
                camera_azimuth = line_azimuth + 90.0
                self._last_preview_crs = layers[0].crs() if layers else None
                self._last_preview_unit = crs_unit_label(layers[0]) if layers else "map units"
                self._preview_mode = "cross_section"
                self._last_cs_p1 = QgsPointXY(p1_layer)
                self._last_cs_p2 = QgsPointXY(p2_layer)
                self._last_cs_width = float(width)
                self.set_preview_cloud_layers(
                    layers=draw_layers, preset="profile_x",
                    az=camera_azimuth, el=0.0, reset_camera=True
                )
            finally:
                self._active_cs_task = None
                self.progress_end()

        task.progressChanged.connect(_progress_changed)
        task.taskCompleted.connect(lambda: _finished(True))
        task.taskTerminated.connect(lambda: _finished(False))
        QgsApplication.taskManager().addTask(task)

    def on_clear(self):
        # Detach canceled tasks so late completion signals cannot repopulate the preview.
        self._cancel_all_tasks(detach=True)
        self._deactivate_map_tools()
        self.progress_end()

        self._last_xyz_full = None
        self._last_cloud_layers = None
        self._last_colors = None
        self._last_color_key = None
        self._applied_color_key = None
        self._preview_pending_redraw = False
        self._pending_reset_cam = False
        self._invalidate_imagery_cache()
        self._preview_mode = "none"
        self._last_cs_p1 = None
        self._last_cs_p2 = None
        self._last_cs_width = None
        self._last_cs_canvas_p1 = None
        self._last_cs_canvas_p2 = None
        self._last_box_extent = None
        self._last_box_canvas_crs = None
        self._pending_box_rebuild_reason = ""
        try:
            if self._box_rebuild_timer.isActive():
                self._box_rebuild_timer.stop()
        except Exception:
            pass
        self._set_color_controls_enabled(True)
        self.stats_label.setText("Draw a box or cross-section to calculate elevation statistics.")
        self.measure_result_label.setText("Measure: click Measure in 3D, then pick two rendered points.")
        self.preview.set_points(np.empty((0, 3), dtype=np.float32))

    def on_cancel_task(self):
        did_cancel = self._cancel_all_tasks(detach=False)
        self.cancel_btn.setEnabled(False)
        if did_cancel:
            self.progress_label.setText("Canceling...")
            self.progress_label.setVisible(True)
        else:
            self.progress_label.setText("")
            self.progress_label.setVisible(False)

    def progress_start(self, label: str = "Processing..."):
        self.progress_label.setText(label)
        self.progress_label.setVisible(True)
        self.progress.setFormat(label + " %p%")
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_processing_state(True)

    def progress_set(self, pct: float):
        self.progress.setValue(int(max(0, min(100, pct))))

    def progress_end(self):
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self.progress_label.setVisible(False)
        self._set_processing_state(False)

    def closeEvent(self, e):
        try:
            if self._update_timer.isActive():
                self._update_timer.stop()
        except Exception:
            pass
        try:
            if self._box_rebuild_timer.isActive():
                self._box_rebuild_timer.stop()
        except Exception:
            pass
        self._cancel_all_tasks(detach=True)
        self._deactivate_map_tools()
        self.progress_end()
        super().closeEvent(e)
