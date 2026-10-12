# Terrain Viewer for QGIS

### Advanced 3D DEM / DTM / DSM Visualization and QC Tool for QGIS

**QGIS Plugin** • **3D Terrain Visualization** • **Raster QC** • **Elevation Analysis** • **Cross-Sections** • **Imagery Coloring**

---

## Overview

**Terrain Viewer** is a QGIS plugin for interactive 3D visualization and quality control of DEM, DTM, and DSM raster surfaces.

It is designed for LiDAR production, elevation QC, terrain inspection, surface comparison, and rapid investigation of raster artifacts directly inside QGIS.

Terrain Viewer provides a detached interactive 3D viewport while keeping compact controls docked inside QGIS. Multiple raster surfaces can be loaded, displayed, compared, colored, shaded, measured, and inspected without leaving the QGIS environment.

The plugin is designed primarily for production workflows where fast visual inspection of elevation data is more important than creating presentation-only 3D scenes.

---

# Key Features

## 3D Terrain Visualization

- Interactive 3D DEM / DTM / DSM viewing
- Detached 3D viewport
- Free camera navigation
- Top-view mode
- Cursor-focused zoom
- Surface and point-cloud style rendering
- Multiple raster surfaces in the same 3D scene
- Per-layer visibility control
- Per-layer coloring
- Z exaggeration
- Grid display
- Camera reset and preset views

---

## Raster Surface Rendering

Terrain Viewer can display elevation rasters as:

- Native raster points
- Dense point visualization
- Continuous triangle surfaces
- Elevation-colored surfaces
- Relief-shaded terrain
- Imagery-colored terrain

Surface meshes are generated while respecting NoData areas so disconnected raster regions are not artificially bridged.

---

## Adaptive Visual Density

Terrain Viewer includes display-only densification modes designed to make sparse raster visualization appear smoother without modifying the original elevation data.

Available modes include:

- Native
- Dense 2×
- Dense 3×

Synthetic display points are generated only for visualization.

Native raster elevations remain the basis for measurements and QC statistics.

---

## Imagery RGB Coloring

Terrain surfaces can be colored using imagery loaded in QGIS.

Supported imagery sources include compatible:

- Raster imagery
- XYZ layers
- WMS / WMTS layers

Features include:

- High-resolution imagery sampling
- Bilinear RGB interpolation
- Color inheritance for synthetic display points
- Raster / imagery alignment through QGIS coordinate transforms

This allows DEM or DSM geometry to be inspected using real-world imagery instead of only elevation colors.

---

## Elevation and Relief Shading

Terrain Viewer supports elevation-based visualization with directional shading.

Options include:

- Height-based coloring
- Relief shading
- Combined height + shading visualization
- Adjustable visualization parameters
- Improved surface depth perception

Relief shading is display-only and does not modify the raster.

---

## Cross-Sections

Terrain Viewer includes interactive cross-section tools for inspecting terrain and surface features.

Features include:

- Draw cross-sections directly in QGIS
- Adjustable corridor width
- Cross-sections through DEM / DTM / DSM data
- Surface ribbon visualization
- Automatic profile extraction
- Snap-assisted drawing
- Multiple surface comparison

Cross-sections are useful for inspecting:

- Roads
- Berms
- Ditches
- Buildings
- Vegetation
- Powerline corridors
- Terrain discontinuities
- Surface noise

---

## 3D Measurement

Measure directly inside the 3D viewport.

Supported measurements include:

- Two-point 3D measurement
- Horizontal distance
- Elevation difference
- True 3D distance

This is useful for quickly checking:

- Object height
- Terrain offsets
- Building elevations
- Vegetation height
- Surface inconsistencies

---

## Vector Overlays

Terrain Viewer supports vector overlays inside the 3D scene.

Supported vector formats include QGIS-compatible sources such as:

- Shapefile
- GeoPackage
- GeoJSON

Features include:

- Z-aware line rendering
- Contour overlays
- Breakline visualization
- Vector visibility control
- Per-layer coloring

Z-enabled vector data can be displayed directly at its true elevation.

---

## Multi-Surface Comparison

Multiple elevation rasters can be loaded simultaneously.

Each raster can have independent:

- Visibility
- Color
- Rendering mode
- Display settings

This makes Terrain Viewer useful for comparing:

- DTM vs DSM
- Ground vs surface models
- Different processing versions
- Pre-project vs post-project surfaces
- Multiple acquisition dates
- QC revisions

---

# Production QC Applications

Terrain Viewer was designed with LiDAR and geospatial production workflows in mind.

Typical applications include:

### DEM / DTM QC

Inspect:

- Ground classification artifacts
- Terrain spikes
- Holes
- Interpolation problems
- Low-density areas
- Edge effects
- Breakline inconsistencies

### DSM QC

Inspect:

- Buildings
- Vegetation
- Powerlines
- Elevated objects
- Surface noise
- Ghost points
- Interpolation artifacts

### LiDAR Production

Useful for:

- Ground surface review
- DSM inspection
- Hydrographic QC
- Contour validation
- Surface comparison
- Deliverable QC
- Visual investigation of suspicious raster areas

---

# Installation

## QGIS Plugin Repository

Once the plugin is publicly approved in the official QGIS Plugin Repository:

1. Open QGIS
2. Go to **Plugins → Manage and Install Plugins**
3. Search for **Terrain Viewer**
4. Click **Install Plugin**

---

## Manual Installation

Download the latest plugin ZIP from the GitHub Releases page.

In QGIS:

1. Open **Plugins → Manage and Install Plugins**
2. Select **Install from ZIP**
3. Select the Terrain Viewer ZIP
4. Click **Install Plugin**

Do not extract the installation ZIP manually.

---

# Basic Usage

## 1. Load Elevation Data

Load a DEM, DTM, or DSM raster into QGIS.

---

## 2. Open Terrain Viewer

Open:

**Raster → Terrain Viewer**

or use the Terrain Viewer plugin controls.

---

## 3. Add a Surface

Select the raster you want to visualize.

Add it to Terrain Viewer and generate the 3D view.

---

## 4. Choose Rendering

Select the desired visualization mode:

- Points
- Dense points
- Surface
- Elevation coloring
- Imagery RGB
- Relief shading

---

## 5. Navigate the 3D Scene

Use the mouse to:

- Rotate
- Pan
- Zoom
- Inspect terrain
- Focus on specific features

---

## 6. Run QC Tools

Use:

- Cross-sections
- 3D measurement
- Raster comparison
- Vector overlays
- Elevation shading

to investigate the surface.

---

# Performance

Terrain Viewer is designed to work with large elevation rasters without requiring the entire dataset to be rendered at full resolution.

The plugin uses adaptive visualization and sampling so large DEM / DSM files can remain practical for interactive QC.

Performance depends on:

- Raster dimensions
- Raster resolution
- Number of active surfaces
- Visualization density
- Computer RAM
- GPU performance

For very large rasters, lower visual density can improve responsiveness.

---

# Data Integrity

Terrain Viewer separates visualization from source data.

Important principles:

- Source rasters are not modified
- Dense visualization does not modify native elevation values
- Synthetic display points are used only for rendering
- QC statistics remain based on native raster samples
- Measurements use actual elevation data
- NoData gaps are preserved in surface rendering

---

# Requirements

- QGIS 3.40 or newer recommended
- Tested with QGIS 3.40.x
- Tested with QGIS 3.44.x
- Windows tested
- Python provided by QGIS
- OpenGL-capable graphics hardware

No additional Python packages or `pip` installation are required.

### QGIS 4

QGIS 4 is not currently supported.

Qt6 compatibility work is in progress for future QGIS versions.

---

# Supported Raster Types

Terrain Viewer works with raster elevation formats supported by QGIS / GDAL, including commonly used formats such as:

- GeoTIFF
- DEM
- DTM
- DSM
- Other QGIS-supported raster elevation datasets

---

# Typical Use Cases

## LiDAR Production

- Ground model inspection
- DSM validation
- Surface artifact detection
- Deliverable QC
- Contour review

## Surveying

- Elevation verification
- Terrain inspection
- Surface comparison
- Breakline checking

## Engineering

- Road corridor inspection
- Terrain analysis
- Drainage review
- Slope inspection
- Surface comparison

## GIS

- Terrain visualization
- Elevation analysis
- Raster QC
- Surface interpretation

---

# Troubleshooting

## 3D Viewer Does Not Open

Check that:

- QGIS is using a supported version
- Your graphics driver supports OpenGL
- The raster is valid
- The raster contains valid elevation values

Restart QGIS after updating graphics drivers.

---

## Terrain Appears Empty

Check:

- Raster NoData values
- Raster extent
- Raster CRS
- Display density
- Selected surface visibility

Try resetting the camera.

---

## Imagery Does Not Appear Correctly

Check:

- Imagery covers the same area
- The imagery layer is visible in QGIS
- CRS information is correct
- XYZ / WMS services are accessible

---

## Performance Is Slow

Try:

- Reducing density
- Displaying fewer surfaces
- Using point mode instead of full surface mode
- Reducing the visible area
- Closing unnecessary QGIS layers

---

# Development

Source code:

https://github.com/LiDARgeek/qgis-terrain-viewer

Bug reports and feature requests:

https://github.com/LiDARgeek/qgis-terrain-viewer/issues

---

# Contributing

Contributions, testing, bug reports, and feature suggestions are welcome.

Typical workflow:

```bash
git clone https://github.com/LiDARgeek/qgis-terrain-viewer.git
