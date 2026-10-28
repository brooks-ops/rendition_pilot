# Commercial building sketches — first field version

Open a Commercial property, choose **Building sketch**, then **Open field sketch**. Sketches are shared by account across model rows within the authenticated district. They are independent of model values and cannot write appraisal inputs.

## Working workflow

- Attach an overhead image before drawing. Upload supporting front, side, rear, angled, or detail photos, or use the phone camera input. This version accepts uploaded aerial imagery; it does not connect a live map/imagery provider.
- Trace wall corners or add a rectangle. Drag vertices, edit normalized corner coordinates, split a component vertically/horizontally, change its label/type, or resize a measured wall. Complex concave splits can require manual correction; server validation rejects crossed shapes.
- Types: main building, addition, canopy, covered loading, covered walkway, detached building, other.
- Set scale by supplying a known distance in feet and selecting its two endpoints. Scale calculations respect the overhead image's width/height ratio. No scale means no dimensions or areas. Scale is not inferred from photo perspective.
- Areas are per component. Overlaps are not deducted and roof edges may not represent walls. No automatic measured-accuracy claim is made.
- Mark every component and the scale verified before approval. Further changes produce a draft; earlier approvals remain in revision history.
- Save draft or approve to persist to the account. Export saved sketches as PDF, vector SVG, or structured JSON (`rendition-pilot-sketch-v1`). SVG/JSON provide a starting exchange format for later CAD sketch import work.

## Photos, storage, and concurrency

The browser resizes images to 1600px. The server validates supported JPEG/PNG/WebP files, limits dimensions, strips EXIF, and converts to JPEG up to 1800px. Limits: 12 photos and 10 MB encoded image data per account. Replacing the overhead requires removing components and saving a draft first to avoid silently moving geometry to another image.

Geometry, metadata, and revision history live in additive SQLite tables initialized by Store. Production RemoteStore writes image bytes to immutable, UUID-named objects in the existing private `commercial-workspace` bucket under the district prefix. Workspace snapshots contain photo references, not image bytes. Images are read through district-authorized server endpoints; no public URLs or browser storage keys are used. Old photos are retained for revision provenance. Failed durable writes restore the prior workspace state; an image uploaded before a later snapshot failure can remain an unreferenced private object.

Writes use an expected revision under SQLite's write transaction and the existing single-instance remote write lock. Another appraiser's intervening save produces a conflict instead of an overwrite. All geometry validation, identity attribution, scale checks, and approval checks happen server-side. As with the existing Commercial store, this deployment assumes one application instance; multiple replicas require shared transactional storage/CAS before scaling out.

IndexedDB retains device drafts keyed by district, signed-in identity, and account, without tokens. Existing drafts in an open field workspace can be edited without connectivity and restored if the server is unavailable. The whole Commercial application is not an offline-installed PWA: a cold load, live imagery, server approval, and AI require connectivity. Device drafts are user/device specific and should be uploaded with Save draft when connected. Conflicting device drafts are retained, not automatically merged over newer server work.

## AI activation

Automatic outlining uses OpenAI Responses structured outputs with image inputs and `store=False`. Configure server-only `COMMERCIAL_SKETCH_API_KEY` (or `OPENAI_API_KEY`) and optionally `COMMERCIAL_SKETCH_MODEL` (default `gpt-4.1`). Keys never reach the browser. The public bootstrap and authorized sketch response expose a boolean availability flag only. OpenAI API usage is separate from a ChatGPT subscription.

The Generate AI draft action explicitly discloses that it sends the attached overhead plus up to five supporting photos to OpenAI. Account values, notes, and identifiers are excluded from the prompt. Photo content is treated as evidence, not instructions. Outputs are validated as bounded, simple polygons with allowed component types; all suggestions start unverified and unscaled. Ambiguous/insufficient images can return no outline without replacing the saved sketch. Provider errors do not alter saved geometry. Replacing existing components requires confirmation, and a revision change during generation prevents overwriting a newer sketch.

Without a configured credential, the editor clearly disables generation while manual tracing, photo capture, measured scale, approvals, and exports remain usable. A model-generated footprint still requires field validation; visual models can place inaccurate corners or confuse roof edges and walls.

Sketch draft saves, approvals, generated drafts, and photo additions are recorded in Commercial activity, with outcomes available in the existing Admin breakdown/exports. Analytics stores no photos or inspection notes.

## Verification

`tests/test_commercial_sketch.py` covers geometry validity, physical scale/aspect ratio, approval requirements, revision conflicts, server identity/account scoping, invalid images, remote rollback, private photo references, restart persistence, valuation isolation, AI credential absence, AI-output validation, and PDF/SVG export. Browser verification uses a synthetic overhead and local sample accounts; no fake inspections are inserted in production. Native file-chooser automation can require the Chrome extension's file-URL permission; backend upload and image display are independently verified when that test permission is unavailable.


## Photo-first capture workflow

The default screen requests an overhead image, front angle, and side angle. Create building sketch uploads pending photos and generates a saved AI draft without a separate upload/save step. Reviewers can confirm all visible component outlines together, select a numbered wall and enter one independently verified length to scale the whole sketch, then approve. Manual tracing, corner editing, splitting, and technical exports are behind correction/manual tools. Latest front and side images take precedence over retained older photos during inference. Aerial imagery acquisition, uncalibrated automatic measurements, and editable Orion delivery remain unimplemented; approval saves only within Rendition Pilot.


## Accuracy changes after the first field trial

Generation now isolates the target building in original-image pixels, traces a focused crop with a coordinate grid, maps those points back to the full image, and independently compares a clean full overhead/crop with candidate overlays. Material disagreement, unsupported canopies, oblique/ambiguous overheads, invalid crop coordinates, and overlapping components fail closed without replacing the saved sketch. The default model is `gpt-6-astra`, following current official API migration guidance; an explicit `COMMERCIAL_SKETCH_MODEL` remains authoritative. Supported newer models use original image detail; older configured models use high. Four bounded calls have a combined 180-second budget. Two separate, stateless crop traces must agree per component on type, at least 97% intersection-over-union, and at most three pixels of boundary displacement before the visual critic is consulted. This is a repeatability check, not an accuracy score. A regression test captures the two initially visually accepted traces of the reported building, which differed materially at the front edge and must be rejected. Visual acceptance is a model judgment, not ground truth or a survey certificate. It must be evaluated on real examples.

A generation `preview: true` request runs the same pipeline without saving any geometry, revision, or activity record. This is useful for evaluating a failed example without overwriting the appraiser's work. No caller-supplied account, actor, or arbitrary model is accepted. Normal generation continues to create a draft revision, not an approval.

All AI-derived approvals now require a verified reference scale and an independent, nonparallel wall measurement on the current outline. Predicted versus measured length must agree within the larger of 0.5 ft and 2%. This is a proportion check, not proof that every edge is correct. An edited checked wall invalidates the check. Component overlaps above 0.5% of the smaller polygon are blocked at approval for manual and AI sketches. Areas remain per-component and are not silently netted.

New photos retain up to 2400 pixels and higher JPEG quality. Existing low-resolution uploads are not artificially enlarged. Ground photos identify structure but cannot provide survey-accurate hidden geometry. A roof-edge draft stays explicitly unverified until the appraiser confirms wall offsets. OCR-detected property labels differing from the account produce a warning. Neither the photo label nor model confidence overrides the server-owned account association. Calibration and the independent check are cleared whenever AI regenerates geometry. No direct Orion import has been added by these accuracy changes.

Official references: https://developers.openai.com/api/docs/guides/images-vision and https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md#migration-quickstart .
