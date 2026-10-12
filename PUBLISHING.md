# Publishing checklist

Repository: https://github.com/LiDARgeek/qgis-terrain-viewer

1. Create the public GitHub repository `LiDARgeek/qgis-terrain-viewer`.
2. Push the contents of this repository root to GitHub (do not upload the outer ZIP as the repository contents).
3. Enable GitHub Issues.
4. Run `python scripts/finalize_release.py --github-user LiDARgeek --repo qgis-terrain-viewer`.
5. Install the generated QGIS ZIP on a clean QGIS 3.40.x profile and a clean QGIS 3.44.x profile.
6. Confirm toolbar/menu launch, raster loading, 3D Box, cross-section, 3D measurement, imagery RGB, vector overlay, Dense 2x/3x, same-box rebuild, and plugin unload/reload.
7. Create/sign in with an OSGeo ID.
8. Open the official QGIS Plugin Repository and choose **Upload a plugin**.
9. Upload the generated plugin ZIP.
10. Keep GitHub source synchronized with every ZIP submitted to QGIS.

The official QGIS repository requires public source code, working homepage/repository/tracker links, GPLv2-or-later-compatible licensing, minimal documentation, and no bundled binaries.
