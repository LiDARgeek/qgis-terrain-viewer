# SPDX-FileCopyrightText: 2026 LiDARgeek
# SPDX-License-Identifier: GPL-2.0-or-later

"""QGIS plugin lifecycle wrapper for Terrain Viewer."""

from pathlib import Path
import traceback

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDockWidget
from qgis.core import Qgis, QgsMessageLog


class TerrainViewerPlugin:
    """Own the toolbar/menu action and dockable viewer lifecycle."""

    MENU_NAME = "&Terrain Viewer"
    DOCK_OBJECT_NAME = "TerrainViewerDock"
    PREVIEW_DOCK_OBJECT_NAME = "TerrainViewerPreviewDock"

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.preview_dock = None
        self.viewer = None
        self.plugin_dir = Path(__file__).resolve().parent

    def initGui(self):
        icon_path = str(self.plugin_dir / "icon.svg")
        self.action = QAction(
            QIcon(icon_path),
            "Terrain Viewer",
            self.iface.mainWindow(),
        )
        self.action.setObjectName("TerrainViewerAction")
        self.action.setToolTip("Open Terrain Viewer for DEM, DTM, DSM, cross-sections, imagery, and vector overlays")
        self.action.triggered.connect(self.run)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu(self.MENU_NAME, self.action)

    def _processing_dock(self):
        """Best-effort lookup so our panel can share the Processing Toolbox area."""
        try:
            for dock in self.iface.mainWindow().findChildren(QDockWidget):
                title = str(dock.windowTitle() or "").lower()
                name = str(dock.objectName() or "").lower()
                if "processing" in title or "processing" in name:
                    return dock
        except Exception:
            pass
        return None

    def _create_dock(self):
        from .viewer import TerrainViewerDialog

        main = self.iface.mainWindow()
        self.viewer = TerrainViewerDialog(parent=main)
        self.dock = QDockWidget("Terrain Viewer", main)
        self.dock.setObjectName(self.DOCK_OBJECT_NAME)
        self.dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        self.dock.setWidget(self.viewer)
        self.dock.setMinimumWidth(300)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)

        # Put the compact controls in the same side-panel stack as Processing
        # Toolbox when possible.  The 3D viewport is deliberately NOT embedded
        # in this controls dock (see preview_dock below).
        processing = self._processing_dock()
        if processing is not None and processing is not self.dock:
            try:
                main.tabifyDockWidget(processing, self.dock)
            except Exception:
                pass

        # Separate 3D viewport.  A QDockWidget gives the user the best of both
        # worlds: it starts as a large detached/floating window, but can be
        # dragged back onto any QGIS dock area whenever desired.
        self.preview_dock = QDockWidget("Terrain Viewer — 3D View", main)
        self.preview_dock.setObjectName(self.PREVIEW_DOCK_OBJECT_NAME)
        self.preview_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea
            | Qt.RightDockWidgetArea
            | Qt.TopDockWidgetArea
            | Qt.BottomDockWidgetArea
        )
        self.preview_dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        self.preview_dock.setWidget(self.viewer.preview)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.preview_dock)
        self.preview_dock.setFloating(True)
        self.preview_dock.resize(1100, 780)
        self.preview_dock.show()
        self.preview_dock.raise_()

    def unload(self):
        if self.viewer is not None:
            try:
                self.viewer.close()
            except Exception:
                pass

        if self.preview_dock is not None:
            try:
                self.iface.removeDockWidget(self.preview_dock)
            except Exception:
                pass
            try:
                self.preview_dock.close()
                self.preview_dock.deleteLater()
            except Exception:
                pass
            self.preview_dock = None

        if self.viewer is not None:
            try:
                self.viewer.deleteLater()
            except Exception:
                pass
            self.viewer = None

        if self.dock is not None:
            try:
                self.iface.removeDockWidget(self.dock)
            except Exception:
                pass
            try:
                self.dock.deleteLater()
            except Exception:
                pass
            self.dock = None

        if self.action is not None:
            try:
                self.iface.removePluginRasterMenu(self.MENU_NAME, self.action)
            except Exception:
                pass
            try:
                self.iface.removeToolBarIcon(self.action)
            except Exception:
                pass
            try:
                self.action.triggered.disconnect(self.run)
            except Exception:
                pass
            self.action.deleteLater()
            self.action = None

    def run(self):
        try:
            if self.dock is None or self.viewer is None:
                self._create_dock()

            self.dock.show()
            self.dock.raise_()
            if self.preview_dock is not None:
                self.preview_dock.show()
                self.preview_dock.raise_()
            try:
                self.viewer.show()
                self.viewer.setFocus(Qt.OtherFocusReason)
            except Exception:
                pass
        except Exception as exc:
            detail = f"Could not start Terrain Viewer: {exc}"
            QgsMessageLog.logMessage(
                detail + "\n" + traceback.format_exc(),
                "Terrain Viewer",
                level=Qgis.Critical,
            )
            self.iface.messageBar().pushMessage(
                "Terrain Viewer",
                detail,
                level=Qgis.Critical,
                duration=10,
            )
