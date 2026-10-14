# Geographic browser fixtures

`plotly-resources.json` contains a Backprop SDK history event and captured map
resources. Browser tests replay the event and serve those bytes without the original
resource server.

`map-glyphs.pbf` is the gzip-decoded Open Sans Regular range 0–255 response from
https://tiles.basemaps.cartocdn.com/fonts/Open%20Sans%20Regular/0-255.pbf, retrieved
October 4, 2026. Its Open Font License is preserved in
[`map-glyphs-LICENSE.txt`](map-glyphs-LICENSE.txt), from
https://github.com/googlefonts/opensans/blob/main/OFL.txt. The font fixture and
synthetic provider responses are test data and are not application assets.

Raster and sprite pixels use the fixture's 2 × 2 PNG. The Maki icon fixture is an
authored SVG triangle. Browser tests intercept provider requests and return these local
resources.
