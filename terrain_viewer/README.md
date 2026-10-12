# Terrain Viewer 1.3.0

**Author:** LiDARgeek  
**Contact:** geek.lidar@gmail.com  
**License:** GPL-2.0-or-later

Interactive DEM/DTM/DSM QC inside QGIS with both point-based and continuous raster-surface rendering, a compact controls dock, and a detached, freely re-dockable 3D viewport.

## Highlights

- Load and compare multiple DEM, DTM, and DSM surfaces in one 3D scene.
- Switch between **Points** and **Surface** rendering. Surface mode connects valid raster cells into triangles and leaves NoData as open holes.
- Show/hide individual surfaces without resetting the current camera.
- Detached 3D viewport by default, with standard QGIS re-docking behavior.
- Adaptive 3D detail presets: Auto, High, Ultra, and Maximum.
- Display-only Dense 2x / Dense 3x native-Z scatter: synthetic XY samples are randomized inside source raster pixels while retaining the exact native cell elevation.
- Native raster samples remain authoritative for measurements and QC statistics.
- Optional RGB coloring from an explicitly selected QGIS imagery layer (local raster, XYZ, WMS, or WMTS), rendered at bounded high resolution and sampled bilinearly for cleaner color detail.
- Display-only **Relief** shading with adjustable strength for Height RGB, grayscale, surface colors, and aerial imagery.
- **Adaptive points** slightly adjusts screen-space point size with camera distance to reduce visual gaps without adding geometry.
- Z-aware SHP, GeoPackage, and GeoJSON line overlays for contours and breaklines.
- Cross-sections through raster surfaces and checked vector overlays, clipped to the same profile corridor width.
- In Surface mode, raster cross-sections render as continuous surface ribbons instead of point samples.
- 3D two-point measurement with horizontal distance, delta Z, 3D distance, slope percent, and slope angle.
- Cursor-focused zoom and double-click orbit-pivot selection for close inspection.
- Percentile/manual Z display ranges and camera outlier protection.
- Same-box live rebuild when 3D detail or visual density changes.

## QGIS compatibility

Version 1.3.0 targets **QGIS 3.40 through QGIS 3.44** (`qgisMinimumVersion=3.40`, `qgisMaximumVersion=3.99`).

Tested on:

- QGIS 3.40.9 on Windows
- QGIS 3.44.x on Windows

QGIS 4 / Qt6 is not yet supported. Linux and macOS code paths are included, but version 1.3.0 has not yet been field-tested on those platforms.

## Installation

### Official QGIS repository

Once approved in the official QGIS Plugin Repository:

1. Open **Plugins > Manage and Install Plugins**.
2. Search for **Terrain Viewer**.
3. Click **Install Plugin**.

### ZIP installation

1. Download the release ZIP.
2. Open **Plugins > Manage and Install Plugins > Install from ZIP**.
3. Choose the ZIP and install it.
4. Open the plugin from the toolbar or **Raster > Terrain Viewer**.

No pip install is required.

## Basic workflow

1. Add one or more DEM/DTM/DSM surfaces to the plugin.
2. Check the surfaces you want visible.
3. Click **Draw 3D Area** and draw a box in the QGIS map canvas.
4. Inspect the result in the detached 3D viewport.
5. Choose **Points** or **Surface**, then adjust detail, density (Points only), colors, Z exaggeration, imagery RGB, and display range as needed.
6. Use **Draw Cross-Section**, **Measure in 3D**, and optional vector overlays for QC.

## Point and surface rendering

**Points** preserves the original QC workflow, including optional Dense 2x / Dense 3x display-only native-Z scatter.

**Surface** renders the selected raster as a triangle mesh built only from fully valid raster neighborhoods. NoData/masked cells remain holes and are never bridged. Multi-surface visibility, imagery RGB, Z exaggeration, contours, cursor-focused navigation, and native-sample 3D measurement remain available. Cross-sections render as continuous surface ribbons in this mode.

Changing the render style after drawing a 3D box rebuilds the same AOI while preserving the current camera. Changing it while viewing a cross-section rebuilds the same transect.

## Safe visual densification

Dense 2x and Dense 3x are display-only modes. They no longer interpolate elevation between neighboring cells. Instead, each synthetic point is tied to one valid native raster cell: its XY is deterministically randomized inside that cell while Z is copied exactly from the native cell value. This prevents false mid-height "ghost" points at sharp DSM discontinuities such as roof edges, powerlines, and vegetation. Synthetic points stay inside the selected point/memory budget and are excluded from native QC statistics and 3D measurement picking.

## Imagery RGB

Enable **Color from imagery** and explicitly select the QGIS imagery layer to sample. This can be a local orthophoto or a raster-like XYZ/WMS/WMTS layer already loaded in QGIS. The imagery layer is used only for display color; it is never treated as an elevation surface.

Version 1.2 renders the current 3D footprint at a bounded higher resolution (up to roughly 2K/3K depending on the 3D Detail preset) and uses bilinear RGB sampling rather than nearest-pixel lookup. Dense 2x/3x synthetic display points inherit color from their source raster pixel center so XY jitter does not create unnecessary imagery speckle.

## Relief shading

**Relief shading** is enabled by default and derives directional lighting from the native elevation raster. It improves visual definition on roofs, bridge decks, embankments, vegetation, and terrain while leaving Z untouched. The relief-strength control sets how strongly the shading modulates the selected color mode or imagery RGB.

Relief is display-only: it does not alter measurements, QC statistics, native samples, synthetic-point Z, or surface geometry.

## Vector overlays

The viewer supports line overlays from SHP, GeoPackage, and GeoJSON. Z may come from true Z geometry or an elevation attribute named `Z`, `ELEV`, `ELEVATION`, `CONTOUR`, or `HEIGHT`.

## Dependencies

The plugin uses only QGIS-provided Python/GDAL/NumPy/PyQt functionality plus the operating system OpenGL implementation. No external Python package installation is required.

## Reporting bugs

Use the GitHub issue tracker: https://github.com/LiDARgeek/qgis-terrain-viewer/issues

Include:

- QGIS version
- operating system
- raster/vector format and CRS
- exact error/traceback if available
- minimal steps to reproduce

## License

GPL-2.0-or-later. See `LICENSE`.
