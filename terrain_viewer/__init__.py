# SPDX-FileCopyrightText: 2026 LiDARgeek
# SPDX-License-Identifier: GPL-2.0-or-later

"""QGIS entry point for Terrain Viewer."""


def classFactory(iface):
    """Return the plugin instance expected by QGIS Plugin Manager."""
    from .main_plugin import TerrainViewerPlugin

    return TerrainViewerPlugin(iface)
