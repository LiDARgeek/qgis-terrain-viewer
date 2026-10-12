# Terrain Viewer for QGIS

**Terrain Viewer** is a QGIS plugin for interactive 3D DEM/DTM/DSM visualization and production QC.

The installable plugin source is in [`terrain_viewer/`](terrain_viewer/).

## Current release

**1.3.0**, targeting QGIS 3.40-3.44.

Core features include multi-surface 3D viewing, detached/re-dockable 3D viewport, Points/Surface rendering, adaptive detail, edge-safe native-Z visual densification, selectable imagery RGB, relief shading, adaptive screen-space point fill, 3D measurement, cross-sections, and Z-aware contour/breakline overlays.

See [`terrain_viewer/README.md`](terrain_viewer/README.md) for installation and usage details.

## Repository

- Source: https://github.com/LiDARgeek/qgis-terrain-viewer
- Issues: https://github.com/LiDARgeek/qgis-terrain-viewer/issues

## Release build

```bash
python scripts/finalize_release.py --github-user LiDARgeek --repo qgis-terrain-viewer
```

The release script validates metadata, checks for compiled/binary files, syntax-checks Python sources, and creates the installable QGIS ZIP.

## License

GPL-2.0-or-later.
