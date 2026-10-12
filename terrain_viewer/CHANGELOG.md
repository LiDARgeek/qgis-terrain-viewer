# Changelog

## 1.3.0 - 2026-10-11

- Renamed the plugin and all user-facing windows/actions to **Terrain Viewer**.
- Renamed the installable Python package from `dem_3d_viewer` to `terrain_viewer`.
- Kept the 1.2.1 rendering, QC, imagery, measurement, cross-section, surface, and native-Z density behavior unchanged.


## 1.2.1 - 2026-10-07

- Polished the public-facing UI without changing QC or geometry behavior.
- Renamed controls to clearer task-oriented language (for example **Draw 3D Area**, **Measure in 3D**, **Detail level**, **Point density**, and **Color from imagery**).
- Renamed the dock to **Terrain Viewer**.
- Added concise tooltips explaining display-only controls, native-sample measurement behavior, elevation-range handling, camera outlier protection, and imagery coloring.
- Kept conservative release defaults: Auto detail, Native point density, relief shading on, auto elevation range on, extreme-Z camera protection on, and cursor-focused zoom on.

## 1.2.0 - 2026-10-07

Visual-quality and shading update.

- Added **Relief** display shading derived from the native DEM/DSM surface using a directional light (315° azimuth / 45° elevation by default).
- Relief shading is display-only and never changes raster Z, measurements, QC statistics, or surface geometry.
- Added adjustable **Relief strength** and enabled relief by default so Height RGB, grayscale, per-surface colors, and aerial RGB retain better 3D shape definition.
- Increased imagery render resolution with bounded 2K/3K supersampling depending on 3D Detail while enforcing a hard image-pixel memory cap.
- Replaced nearest-pixel imagery lookup with memory-bounded **bilinear RGB sampling** for cleaner roads, roof edges, markings, and orthophoto detail.
- Dense 2x/3x synthetic points now sample imagery/relief from their source raster pixel center, reducing color speckle from jittered XY positions.
- Added **Adaptive points**: point size changes mildly with camera distance to close visual gaps when zoomed out while staying crisp during close inspection.
- Preserved all native-Z scatter, NoData, point-budget, memory-budget, measurement, and QC safety rules.

## 1.1.2 - 2026-10-07

Edge-safe visual density update.

- Replaced Dense 2x / Dense 3x bilinear Z interpolation with deterministic **native-Z scatter**.
- Synthetic display points now inherit the exact elevation of a native raster cell and randomize only XY inside that cell footprint.
- Eliminates false mid-height ghost points along sharp DSM discontinuities such as roof edges, powerlines, and vegetation.
- Dense scatter remains display-only, deterministic, and constrained by the existing point/memory budgets.
- QC statistics and 3D measurement remain native-raster only.


## 1.1.1

- Fixed a regression where starting Cross-Section raised `name "p1" is not defined`.
- Same-transect rebuild is preserved when switching Points/Surface render style.

## 1.1.0 - 2026-10-05

Continuous raster-surface rendering.

- Added **Points / Surface** render style selector.
- Surface mode builds a triangle mesh from valid raster-grid neighborhoods only.
- NoData/masked cells remain open holes; the mesh does not bridge missing data.
- Surface rendering works with multiple DEM/DTM/DSM layers, per-layer visibility, opacity, height colors, and imagery RGB.
- 3D measurement and QC statistics remain tied to native raster samples rather than triangle-derived values.
- Cross-sections render as continuous surface ribbons in Surface mode while preserving the existing corridor-based contour/breakline overlays.
- Render-style changes reuse the current 3D box or current cross-section for direct comparison.
- Existing point-rendering, Dense 2x/3x visual interpolation, and GDAL/NumPy compatibility behavior remain available.

## 1.0.1 - 2026-10-05

Compatibility patch for QGIS/Windows environments with a GDAL/NumPy array ABI mismatch.

- Replaced all GDAL `ReadAsArray()` calls with `ReadRaster()` plus direct NumPy byte decoding.
- Avoids importing GDAL's optional compiled `gdal_array/_gdal_array` bridge during DEM reads.
- Fixes `numpy.core.multiarray failed to import` during 3D box and cross-section extraction on affected installations.
- No new dependencies; raster values, masks, resampling, QC, and display behavior are otherwise unchanged.

## 1.0.0 - 2026-10-05

First stable public release.

- Multi-surface DEM/DTM/DSM visualization and per-surface visibility.
- Detached/re-dockable 3D viewport and compact QGIS dock controls.
- Height shading, per-surface color modes, imagery RGB from an explicitly selected QGIS raster/XYZ/WMS/WMTS layer.
- Percentile/manual Z ranges and outlier-safe camera framing.
- Direct 3D two-point measurement.
- Persistent-width raster cross-sections with live transect feedback.
- Z-aware SHP/GeoPackage/GeoJSON contour and breakline overlays.
- Contour/breakline cross-section clipping to the full profile corridor.
- Cursor-focused zoom and double-click orbit-pivot navigation.
- Adaptive detail presets with deterministic grid-aware reduction and shared multi-surface budgets.
- Dense 2x / Dense 3x display-only bilinear visual fill inside valid 2x2 DEM neighborhoods.
- Native-only measurement and QC statistics when visual fill is enabled.
- Same-box automatic rebuild for detail/density changes while preserving camera/orbit/zoom.
- No pip-installed runtime dependencies.
