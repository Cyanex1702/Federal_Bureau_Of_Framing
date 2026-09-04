# Federal Bureau of Framing — V7.3.1

V6 is the universal appliance-normalization version of the project.

It is designed for **coffee machines, refrigerators, water heaters, air conditioners, hobs, hoods, ovens, dishwashers, freezers, small appliances, commercial equipment, and other product categories**.

Coffee machines were the main V6 debugging dataset, but the architecture is intentionally category-independent.

---

# What changed in V6

The major V5 limitation was simple:

```text
all visible foreground
        ↓
one bounding box
        ↓
scale from that box
```

That works well for a clean single-product image, but it can make the primary appliance too small when the photo also contains:

- cups
- milk containers
- bean jars
- remote controls
- pipe fittings
- side modules
- secondary refrigeration modules
- other legitimate product accessories

V6 replaces this with:

```text
image
  ↓
foreground segmentation
  ↓
connected-component analysis
  ↓
PRIMARY BODY / STRUCTURAL GROUP
  +
FULL VISIBLE EXTENT
  ↓
scale from PRIMARY BODY
  ↓
keep FULL EXTENT inside safe canvas
  ↓
category + geometry + subtype
  ↓
canonical alignment
```

This is the core V6 improvement.

---

# V6 Universal Product Model

Every image can now have two important bounding areas.

## Primary body

The main appliance or structural product group.

This controls:

- visual scale
- horizontal centering
- baseline
- vertical centering

## Full visible extent

Everything that should remain visible in the final product photo.

This controls:

- crop safety
- minimum edge padding
- accessory preservation

Example:

```text
Coffee machine + cup + milk container

        cup
         ○

    ┌───────────┐
    │  PRIMARY  │
    │  MACHINE  │
    │   BODY    │
    └───────────┘

       milk jug
          ▯

Primary body controls SCALE.
Full scene controls CROP SAFETY.
```

The same logic applies to other categories.

### AC example

```text
indoor AC unit + remote

AC chassis = primary body
remote     = secondary/accessory
```

### Water heater example

```text
heater tank + small loose fittings

tank     = primary body
fittings = secondary foreground
```

### Multipart commercial product

```text
machine + sold refrigeration module

both major modules = structural multipart product
```

---

# V6 Universal Structural Subtypes

V6 no longer relies only on broad categories such as `Coffee machine`.

It automatically detects geometric/structural subtypes:

- `boxy`
- `portrait_boxy`
- `tall_standing`
- `wide`
- `flat_horizontal`
- `accessory_heavy`
- `multipart`

These work across categories.

Examples:

| Product | Likely V6 subtype |
|---|---|
| Countertop coffee machine | boxy |
| Floor-standing coffee vending tower | tall_standing |
| Refrigerator | tall_standing / portrait_boxy |
| Split AC indoor unit | wide |
| Hob / cooktop | flat_horizontal |
| Coffee machine with detached cups/jugs | accessory_heavy |
| Coffee machine + fridge module | multipart |
| Portable vertical AC | tall_standing |

This prevents visually different products on the same category page from being forced into one calibration group.

---

# Subgroup-Aware Detection

V5 could compare most products inside the same broad profile.

V6 compares products using:

```text
geometric profile + structural subtype
```

For example:

```text
Coffee-machine page
│
├── boxy::boxy
├── tall::tall_standing
├── boxy::accessory_heavy
└── wide::multipart
```

So a floor-standing coffee machine is no longer used as the scale reference for a normal countertop machine.

The same system works automatically for other appliance pages.

---

# Detection Test

Paste a category/listing URL.

The app scouts the page for:

- product names
- product images
- product-page links
- Product JSON-LD
- normal HTML product cards

Each usable image is analyzed for:

- primary-body height occupancy
- primary-body width occupancy
- primary-body horizontal centering
- primary-body baseline / vertical center
- full visible edge padding
- full foreground area
- accessory/secondary foreground ratio
- component count
- structural subtype
- geometric profile
- segmentation reliability

Potential problems include:

- primary body too zoomed out
- primary body too zoomed in
- primary body shifted left/right
- inconsistent body baseline
- inconsistent body vertical centering
- full product/accessory extent too close to an edge
- suspicious foreground segmentation

---

# Body Interpretation Modes

V6 has four body strategies.

## Auto — smart primary body

Recommended default.

The app:

- finds connected components
- identifies the largest body
- merges substantial nearby structural parts
- detects multiple similarly large modules
- keeps smaller detached objects as accessory/secondary foreground

## Main body only

Forces the largest connected foreground component to control the scaling.

Useful when an image contains:

- cups
- props
- detached accessories

## Full visible extent

Uses all visible foreground for scale.

This behaves more like V5 and is useful when everything visible should truly determine the product size.

## Multipart

Merges major components before scaling.

Useful for:

- appliance + sold refrigeration module
- paired commercial units
- two structural bodies that form one SKU/configuration

---

# Page Showcase

After Detection Test, open **Page Showcase**.

You can view:

- **Fixed page**
- **Current page**
- **Changed only**

Replacement policies:

- **Fix flagged images only**
- **Normalize every image**

For final production output, **Normalize every image** is usually the strongest choice because every product passes through the same normalization pipeline.

---

# V6 Per-Product Exception Tuner

V6 adds a manual edge-case panel inside Page Showcase.

Open:

**V6 per-product exception tuner**

Choose a product and adjust:

- body interpretation
- extra size adjustment
- extra horizontal nudge
- extra vertical nudge
- extra baseline trim

Available body interpretations:

- Auto — smart primary body
- Main body only
- Full visible extent
- Multipart

Click:

**Save product override**

The exception is immediately used by:

- Page Showcase
- corrected preview
- one-click page export

This is intended for the final 1–2% of unusual product images.

---

# Strict / Page-Calibrated Mode

V6 strict mode is **subtype-aware**.

Instead of calculating one median for every coffee machine or every refrigerator, the app calculates calibration targets inside compatible groups.

Example:

```text
boxy::boxy
        ↓
median body occupancy
median baseline
        ↓
strict subgroup target
```

A group needs multiple examples before V6 forces a page-derived calibration. Otherwise the canonical category/geometric defaults remain safer.

---

# Global Fine-Tune Controls

The sidebar still includes:

- Global size trim
- Horizontal nudge
- Vertical nudge
- Baseline trim

These affect the full batch.

Per-product overrides are added on top of the global values.

---

# One-Click Page Export

After scanning and reviewing the page, click:

**Normalize & build export package**

The app can export:

- all analyzed products
- only products that need review

Product-specific V6 overrides are automatically included.

Output:

```text
page_normalization_export/
│
├── perfected_images/
│   ├── Product Name A/
│   │   └── Product Name A.png
│   ├── Product Name B/
│   │   └── Product Name B.png
│   └── ...
│
├── side_by_side_previews/
│   ├── Product Name A.jpg
│   ├── Product Name B.jpg
│   └── ...
│
└── export_manifest.csv
```

The manifest now records V6 details such as:

- profile
- structural subtype
- body interpretation mode
- accessory area ratio
- manual size adjustment
- manual X/Y nudges
- baseline override
- final QA result
- normalization warnings

---

# Categories

The current app includes mappings for:

- Air conditioner
- Blender
- Coffee machine
- Cooktop
- Dishwasher
- Freezer
- Hob
- Hood
- Kettle
- Microwave
- Oven
- Refrigerator
- Water heater

Adding another product category does **not** require a new image-processing algorithm.

Usually you only add:

```text
category → default geometric family / canonical framing preference
```

V6 will still inspect the actual primary-body geometry and can correct obvious category/profile mismatches.

---

# Windows — Easiest Way to Run

V6 includes:

```text
RUN_APP.bat
```

Simply double-click:

```text
RUN_APP.bat
```

The launcher automatically:

1. moves into the correct app folder
2. verifies Python
3. creates `.venv` if necessary
4. checks required packages
5. installs requirements when needed
6. launches the app using the reliable Windows command

```bat
.venv\Scripts\python.exe -m streamlit run app.py
```

The application normally opens at:

```text
http://localhost:8501
```

Keep the Command Prompt window open while using the app.

Press `Ctrl+C` to stop the app.

---

# Manual Windows Setup

Create the virtual environment:

```bat
python -m venv .venv
```

Activate it:

```bat
.venv\Scripts\activate
```

Install requirements:

```bat
python -m pip install -r requirements.txt
```

Run:

```bat
python -m streamlit run app.py
```

Use:

```bat
python -m streamlit run app.py
```

instead of:

```bat
streamlit run app.py
```

to avoid the common Windows PATH issue.

---

# Optional AI Background Removal

Activate the V6 environment:

```bat
.venv\Scripts\activate
```

Install:

```bat
python -m pip install rembg onnxruntime
```

Then launch normally:

```bat
python -m streamlit run app.py
```

or double-click `RUN_APP.bat`.

---

# Recommended V6 Workflow

For final production assets:

1. Double-click `RUN_APP.bat`.
2. Open **Detection Test**.
3. Paste the product-category URL.
4. Select the real catalog category if available.
5. Scan the page.
6. Review detected V6 subtypes and flagged products.
7. Switch to **Strict / page-calibrated** if the page contains enough comparable products.
8. Open **Page Showcase**.
9. Choose **Normalize every image**.
10. Review the complete corrected grid.
11. Use the **V6 per-product exception tuner** only for unusual edge cases.
12. Save any exceptions.
13. Verify the Fixed Page again.
14. Return to Detection Test.
15. Click **Normalize & build export package**.
16. Download the ZIP.
17. Review `export_manifest.csv`.

---

# URL Scanner Limitation

The current page scanner uses normal HTTP requests.

It can scan many pages that expose:

- server-rendered product HTML
- Product JSON-LD
- normal image tags

It may not see products when:

- the catalog is rendered only after JavaScript executes
- login is required
- an anti-bot system blocks requests
- imagery comes from unusual client-side APIs

A future version can add browser rendering such as Playwright.

---

# Important Scope

V6 improves:

- visual product scale
- primary-body scale consistency
- whitespace
- horizontal alignment
- baseline / vertical alignment
- accessory-aware framing
- multipart-product framing
- full-extent crop safety

It does **not** reconstruct a new camera viewpoint.

If source photographs use very different perspective angles, V6 preserves those perspectives.

---

# Files

```text
product_image_normalizer_app/
├── app.py
├── normalizer.py
├── page_detector.py
├── profiles.json
├── requirements.txt
├── README.md
├── RUN_APP.bat
├── Dockerfile
├── sample_input.png
└── sample_output.png
```

---

# V6 Design Principle

The system is no longer:

> "Make every coffee machine the same size."

It is:

> **Identify the primary product body, understand its geometric/structural class, scale that body to a canonical standard, preserve the complete sold/product-visible extent, and align compatible products consistently across the page.**

That principle is what allows the same V6 engine to work across the rest of the website.


---

# V6.1 Bugfix Update

V6.1 specifically fixes two issues found while testing additional appliance pages.

## 1. Pixel-level padding / alignment drift

Some products that should have shared the same alignment could appear one or two pixels higher/lower.

The cause was not the canonical target itself. V6 used floating-point predictions of the resized primary-body coordinates and then clamped the full foreground crop to preserve accessories. Rounding and late clamping could therefore introduce a very small body-position shift.

V6.1 now uses **pixel-locked placement**:

```text
choose integer canonical target
        ↓
calculate scale that fits full visible extent
WITHOUT moving the body anchor
        ↓
resize
        ↓
measure the ACTUAL resized primary-body mask
        ↓
place its exact integer bbox
on the canonical center/baseline
```

For ordinary unconstrained images, products in the same calibrated subgroup now share the same canonical body anchor with no source-size-dependent 1–2 px drift.

The QA metadata now includes:

- `anchor_error_x_px`
- `anchor_error_y_px`
- `pixel_lock_ok`
- `placement_constrained`

If an unusually large detached accessory genuinely prevents exact placement inside the safe frame, V6.1 reports that instead of silently hiding the reason.

## 2. Wrong-category products appearing in Detection Test

Some pages contain:

- recommended products
- related-product carousels
- footer product cards
- cross-sell sections

V6 could scout those cards even when they belonged to another appliance category.

V6.1 adds a **category gate** before image analysis.

It uses:

- the category selected in the app, or
- page URL
- page title
- H1/H2 text
- breadcrumb/category-title text

to determine the effective category.

It then removes products with strong evidence that they belong to a different known category.

Example:

```text
Microwave category page
        ↓
TEKA microwave               KEEP
Baumatic microwave           KEEP
Coffee / espresso machine    FILTER
```

Ambiguous products are kept rather than aggressively deleted.

Detection Test now shows:

- Scouted cards
- Category-matched cards
- Filtered off-category cards
- Needs review
- Consistency pass rate

You can open **Filtered off-category products** to inspect what was excluded.

## Additional categories

V6.1 also adds built-in category mappings for:

- Laundry equipment
- Cart / trolley
- Ice machine
- Water dispenser

alongside the existing appliance categories.



---

# V6.2 Alignment Fix

V6.2 addresses a second alignment issue discovered on built-in microwave pages.

## Why V6.1 could still look wrong

V6.1 correctly pixel-locked the **primary detected body**, but some products do not have a primary mask whose center matches the visual center of the whole appliance.

This is especially common with:

- built-in microwaves
- built-in ovens
- hobs / cooktops
- hoods
- wall-mounted AC units
- light stainless-steel outer frames around dark inner panels

Example:

```text
visible microwave frame
┌─────────────────────────────┐
│        dark door      panel │
│        ████████        ▓▓   │
└─────────────────────────────┘
```

If the dark inner region dominates segmentation, locking that inner region perfectly can still make the **outer appliance frame** look slightly high, low, left or right compared with another model.

## V6.2 separates scale and alignment

V6.2 uses two independent concepts:

```text
SCALE BASIS
What determines how large the appliance should appear?

ALIGNMENT REFERENCE
What visible envelope should sit on the canonical center/baseline?
```

For clean facade-style products such as microwaves, the default is now:

```text
scale using product/body rules
        +
center the FULL VISIBLE APPLIANCE ENVELOPE
```

For accessory-heavy coffee-machine imagery:

```text
scale primary machine body
        +
align primary machine body
        +
keep cups/jugs/accessories inside safe canvas
```

## Category-aware alignment defaults

V6.2 automatically uses **full visible appliance centering** for clean categories such as:

- Microwave
- Oven
- Hob
- Cooktop
- Hood
- Air conditioner
- Water heater
- Cart / trolley

It normally keeps **primary-body alignment** for:

- Coffee machine
- Refrigerator
- Freezer
- Dishwasher
- Laundry equipment
- Water dispenser
- Ice machine

Accessory-heavy or multipart analysis can automatically override those choices when necessary.

## Manual Alignment Reference

The V6 per-product tuner now includes:

**Alignment reference**

Options:

- Auto — category-aware alignment
- Primary body — ignore detached props
- Full visible appliance — center outer envelope

This is independent from **Body interpretation**.

That distinction is important:

- Body interpretation controls scale/body structure.
- Alignment reference controls what is visually centered/aligned.

## Recommended microwave workflow

For a microwave category page:

1. Run Detection Test.
2. Select `Microwave` if Auto did not infer it.
3. Use Standard or Strict scale mode as preferred.
4. Open Page Showcase.
5. Choose `Normalize every image`.
6. V6.2 will automatically use full-appliance envelope centering.
7. Only use per-product overrides for unusual outliers.



---

# V6.3 Advanced Multi-Page Audit

V6.3 adds a dedicated **Advanced Audit** tab for auditing a complete website category structure instead of reviewing only one page at a time.

## Example audit structure

```text
Coffee Machines
├── Espresso
│   ├── Page 1
│   ├── Page 2
│   └── Page 3
├── Automatic
│   ├── Page 1
│   └── Page 2
├── Office
├── Home
└── Capsule

Air Conditioners
├── Split AC
├── Cassette
├── Window AC
└── Portable AC

Refrigerators
├── Single Door
├── Double Door
└── Side-by-Side
```

The hierarchy is not hard-coded. You type the top-level category, subcategory, page label, and URL for each scan.

## Advanced Audit output

For every product, the audit can record:

- main appliance category
- subcategory
- page label
- page URL
- technical detection category
- product name
- pass/review status
- consistency score
- exact detected problem(s)
- suggested correction
- geometric profile
- structural subtype
- comparison subgroup
- body height/width occupancy
- horizontal center offset
- baseline position
- full edge padding
- accessory area ratio
- connected component count
- product URL
- image URL
- scan timestamp

## Single master CSV

Advanced Audit can maintain one persistent file:

```text
audit_data/master_audit.csv
```

Each new page can be:

- added to the master audit
- used to replace the previous rows for that same category/subcategory/page
- appended while avoiding exact product duplicates

The master CSV remains available after Streamlit is restarted because it is stored inside the application folder.

The recommended workflow when rescanning a page is:

**Replace this page if it already exists**

That keeps one current result per website page.

## Separate CSV option

Every scanned page can also be downloaded immediately as its own CSV.

The full master audit can additionally be exported as separate files in:

```text
Category/
└── Subcategory/
    ├── Page 1.csv
    ├── Page 2.csv
    └── Page 3.csv
```

using:

**Download separate CSVs by category / subcategory / page**

## Advanced issue view

The Advanced Audit tab focuses on problem products by default.

Each item shows:

- product image
- product name
- severity
- consistency score
- profile
- subtype
- detected problem descriptions
- suggested correction
- diagnostic framing metrics
- product-page link

Passing products can be shown with the **Show passing products too** toggle.

## Main category vs Detection category

V6.3 intentionally keeps these separate.

Example:

```text
Master CSV category: Coffee Machines
Detection category:  Coffee machine
Subcategory:         Espresso
Page:                Page 2
```

The first field controls your reporting hierarchy.

The Detection category controls:

- wrong-category filtering
- category geometry
- normalization logic

This lets the audit structure match the website even when the technical CV category is broader.



---

# V6.4 Automatic Pagination + Visual Audit Packages

V6.4 extends Advanced Audit in two major ways.

## 1. One URL can audit the complete paginated subcategory

You can now paste one listing URL, for example:

```text
Coffee Machines
└── Espresso
    └── Page 1 URL
```

and enable:

**Automatically discover all pagination pages**

V6.4 follows actual server-rendered pagination links and `rel=next` links on the website.

If the listing exposes:

```text
Page 1
Page 2
Page 3
Page 4
```

the app audits all four pages automatically.

The Advanced Audit result is shown page-by-page, including:

- products with problems
- product image
- issue descriptions
- suggested correction
- consistency score
- profile / subtype
- diagnostic measurements
- off-category products that were filtered

### Pagination safety

V6.4 follows real pagination links rather than blindly guessing arbitrary URLs such as `?page=2`.

The crawl is:

- limited to the same website origin
- bounded by the **Maximum pages to crawl** setting
- based on pagination containers, numeric page links, and `rel=next`

If the website renders pagination only with JavaScript, the normal HTTP scanner may still see only Page 1. A future browser-rendered scanner can handle those sites.

---

## 2. Current + Normalized images in the audit workflow

CSV is plain text and cannot directly contain image binary data inside cells.

V6.4 therefore uses a portable audit-package approach.

The master CSV now includes these columns:

```text
current_image_file
normalized_image_file
comparison_image_file
visual_report_section
```

When:

**Save current + normalized + side-by-side images with the master audit**

is enabled, the app generates and saves:

```text
audit_data/
├── master_audit.csv
└── media/
    └── Coffee Machines/
        └── Espresso/
            ├── Page 1/
            │   └── Product Name/
            │       ├── current.png
            │       ├── normalized.png
            │       └── before_after.jpg
            ├── Page 2/
            └── Page 3/
```

The CSV contains the relative paths to those files.

---

# Master Visual Audit Package

Advanced Audit now includes:

**Download MASTER VISUAL AUDIT PACKAGE**

The ZIP contains:

```text
master_audit.csv
visual_report.html
media/
├── ...
└── ...
README.txt
```

After extracting the ZIP, open:

```text
visual_report.html
```

The report is visually divided into:

```text
Category
  ↓
Subcategory
  ↓
Page
  ↓
Product
```

For every recorded product, it displays:

```text
CURRENT IMAGE          NORMALIZED IMAGE
[ before ]             [ after ]
```

along with:

- detected problem
- suggested fix
- status
- consistency score
- profile
- subtype
- link to the exported side-by-side comparison image

This gives you both:

1. a machine-readable master CSV, and
2. a human-readable visual QA report.

---

# Recommended V6.4 Website-Wide Audit Workflow

Example:

```text
Coffee Machines → Espresso
```

1. Paste the Espresso Page 1 URL.
2. Enable **Automatically discover all pagination pages**.
3. Run the Advanced Audit.
4. Review Page 1 / Page 2 / Page 3 / Page 4 results.
5. Save the full crawl to the master audit.
6. Move to Coffee Machines → Automatic.
7. Repeat.
8. Move to Office, Home, Capsule, etc.
9. Move to another top-level appliance such as Air Conditioners.
10. Continue using the same master audit.

The final hierarchy can look like:

```text
Coffee Machines
├── Espresso
│   ├── Page 1
│   ├── Page 2
│   ├── Page 3
│   └── Page 4
├── Automatic
├── Office
├── Home
└── Capsule

Air Conditioners
├── Split AC
├── Cassette
└── Portable

Refrigerators
├── Single Door
├── Double Door
└── Side-by-Side
```

All results can remain in a single `master_audit.csv`.



---

# V6.5 Boss-Friendly Audit Reporting

V6.5 simplifies the audit output for management while keeping engineering detail available separately.

## Boss SIMPLE CSV

Use:

**Boss SIMPLE CSV**

Columns:

```text
Main Category
Subcategory
Page URL
Page Label
Product Name
Product Status
Problem Summary
Consistency Score
Suggested Fix
Profile
Subtype
```

The simple report intentionally does **not** include:

- effective detection category
- product URL
- source image URL
- scan timestamp / UTC time
- body-height occupancy
- body-width occupancy
- center-offset measurements
- baseline measurements
- padding measurements
- component counts

## Advanced TECHNICAL CSV

The advanced CSV keeps the engineering data needed for debugging:

- comparison group
- body height occupancy
- body width occupancy
- horizontal center offset
- body baseline
- full edge padding
- accessory area ratio
- component count
- saved visual asset filenames

It still does **not** expose:

- product URL
- source image URL
- effective detection category
- audit timestamp

## Actual images

CSV is a plain-text format and cannot embed actual image binaries in cells.

V6.5 therefore adds:

**Boss VISUAL EXCEL**

The workbook contains two sheets:

```text
Simple Audit
Advanced Audit
```

Both sheets can include the actual:

```text
Current Image
Normalized Image
Before / After Image
```

as embedded spreadsheet images.

## Complete Boss Report Package

Use:

**Complete BOSS REPORT PACKAGE**

It contains:

```text
01_Boss_Simple_Audit.csv
02_Advanced_Technical_Audit.csv
03_Boss_Visual_Audit.xlsx
04_Visual_Report.html
media/
└── Category/
    └── Subcategory/
        └── Page/
            └── Product/
                ├── current.png
                ├── normalized.png
                └── before_after.jpg
README.txt
```

For sending the audit to management, the recommended files are:

1. `03_Boss_Visual_Audit.xlsx`
2. `01_Boss_Simple_Audit.csv`

The advanced CSV is primarily for engineering/debugging.

## Privacy / audit metadata

V6.5 removes audit timestamps from the exported and persisted audit rows.

The reports do not state when the audit was performed.


---

# V6.5.1 Hotfix

V6.5.1 fixes a startup regression introduced while simplifying the V6.5 audit-report code.

The V6.5 build accidentally omitted two helper functions from `audit_store.py`:

- `hierarchy_summary`
- `separate_page_csv_zip`

`app.py` still imported those helpers, which produced:

```text
ImportError: cannot import name 'hierarchy_summary' from 'audit_store'
```

V6.5.1 restores both functions and adds an import-contract regression test so this type of missing-function packaging error is caught before release.

No audit features were removed. V6.5.1 keeps:

- Boss SIMPLE CSV
- Advanced TECHNICAL CSV
- Boss VISUAL EXCEL with embedded images
- complete Boss Report Package
- automatic pagination audit
- persistent multi-category master audit


---

# V6.5.2 False-Product Detection Hotfix

V6.5.2 fixes website UI graphics being incorrectly analyzed as products.

Examples that are now rejected before computer-vision analysis:

- `Follow Us` graphics
- Google Play badges
- Apple App Store badges
- social-media icons / graphics
- footer and header logos
- payment / trust badges
- newsletter / subscribe graphics
- QR-code graphics
- images linked to external social or app-store websites

The scanner now evaluates the DOM context around each image, including:

- parent HTML tags
- container class names and IDs
- ARIA labels / roles
- image ALT / title text
- image URL
- surrounding link URL
- whether the image sits inside a product-card-like container

A website footer or mobile-app download section is therefore filtered before it reaches the appliance detector.

This prevents UI graphics from appearing in:

- Detection Test
- Advanced Audit
- page consistency scoring
- master CSV reports
- visual audit reports


---

# V6.5.3 Exact Page Membership + Product-Link Audit

V6.5.3 fixes an important Advanced Audit problem: products could sometimes be
reported on the wrong pagination page.

## Why it happened

Many ecommerce pages contain product-looking data that is not part of the
visible catalog listing for the current page, including:

- JSON-LD / structured product data
- related products
- recommended products
- recently viewed products
- upsells / cross-sells
- sliders / carousels
- featured-product sections

Older scanners could discover those objects and treat them as if they belonged
to Page 1, Page 2, etc.

## Strict Page Membership

V6.5.3 changes the rule:

> A product must be independently verified inside the current page's catalog
> listing DOM before it can enter the audit.

JSON-LD is now enrichment-only. It may improve the name or product URL of a
product already verified in the listing, but it cannot create an additional
page product by itself.

The scanner also rejects product-looking cards inside:

- Related
- Recommended
- Recently Viewed
- Similar Products
- You May Also Like
- Upsell / Cross-sell
- Carousel / Slider
- Featured Products

sections.

## Exact Product URL in reports

Boss and Advanced audit exports now use:

```text
Product URL
```

instead of:

```text
Page URL
```

The full direct product-detail URL is included so a reviewer can open the exact
coffee machine, refrigerator, dishwasher, AC, oven, or other appliance being
reported.

The page label is still retained so the audit remains organized as:

```text
Category → Subcategory → Page → Product
```

The actual listing-page URL remains internal for page identity / replacement
logic, but it is not shown as the main report link.

## Visual Excel

The Product URL column in the Visual Excel report is clickable and displays the
complete URL.



---

# Federal Bureau of Framing Branding

The application is now officially named:

```text
Federal Bureau of Framing
```

Descriptive subtitle:

```text
Product Image Audit & Normalization Tool
```

The humorous name is branding only. Functional UI labels remain clear and descriptive:

- Detection Test
- Advanced Audit
- Page Showcase
- Normalize Images
- Reports / exports

This keeps the app memorable without making the workflow confusing for users or management.


---

# V6.5.5 False-Negative / Skipped-Product Fix

V6.5.5 addresses a regression where products that were correctly flagged in an
older build could disappear from the strict audit or pass because the newer
scanner had an incomplete comparison group.

## 1. Strict membership now has a safe recovery lane

V6.5.3 correctly stopped JSON-LD, related products and recommendations from
being assigned to the wrong page, but the strict DOM rules could be too
aggressive for custom ecommerce themes.

V6.5.5 keeps the hard exclusions, while recovering a legitimate product when:

- it is visibly present in the current page HTML
- its image is linked to a same-site product-detail page
- it has a real product name
- it is not inside a related/recommended/recently-viewed/upsell section
- it is not footer/header/social/app-store UI

Generic `carousel`, `slider` and `swiper` class names are no longer enough by
themselves to reject a product, because some real catalog grids use those
wrappers.

## 2. Hybrid issue detection

The old audit mostly judged scale and vertical placement relative to the median
of other products in the same subgroup.

That can create a false pass when:

- the strict scanner accidentally has too few peers
- multiple bad products are wrong in the same direction
- the peer group itself is not a good visual standard

V6.5.5 therefore uses two signals:

1. **Peer consistency** — comparison with compatible products on the page
2. **Canonical framing sanity** — a conservative check against the expected
   profile/category framing target

A product can now be flagged for being clearly too small, too large, too high
or too low even if its small peer group would otherwise make the error look
normal.

The canonical check is deliberately broader than the peer tolerance to reduce
false positives, and accessory-heavy / multipart products receive additional
slack.

## 3. Audit diagnostics

Advanced Audit now reports how many legitimate products were recovered from
non-standard catalog markup, and each analyzed item carries a canonical size
ratio for debugging.


---

# V6.5.6 Normalizer-Agreement Audit Fix

V6.5.6 fixes the specific false-pass pattern seen with products such as the
**Bunn VP17A-2**: the audit could say "Original kept / PASS", while
**Normalize every image** visibly improved the same source.

That disagreement is no longer allowed.

## Three independent audit signals

A product is now judged using:

1. **Peer consistency**
   - comparison with compatible products on the page

2. **Canonical framing sanity**
   - broad expected category/profile framing

3. **Normalizer agreement**
   - predicts what the actual normalizer would do to the product on a square
     catalog card

If the real normalization target would:

- enlarge the product by more than about 5.5%
- reduce it by more than about 5.5%
- move it horizontally by more than about 2.8% of the card
- move it vertically by more than about 2.8% of the card

the audit cannot call that source perfectly framed.

## Why this catches narrow/tall products

Earlier logic mainly checked the controlling profile axis and used a deliberately
wide canonical tolerance. A narrow/tall brewer could therefore be only
moderately undersized and still pass.

V6.5.6 converts the raw source framing into the same square-card coordinate
system used by the catalog, applies the exact normalizer profile rules, and
computes the recommended visual scale/anchor change.

This means the audit decision now agrees much more closely with the visible
Before → After result.

## Diagnostics

Advanced Audit now carries:

- `normalizer_recommended_scale`
- `normalizer_scale_delta`
- `normalizer_x_shift`
- `normalizer_y_shift`
- `normalizer_anchor_mode`

so a future false pass can be diagnosed directly.


---

# V6.6 Home-Appliance Composition + Showcase Upgrade

V6.6 addresses the first major mixed Home Appliances test.

## Fixed-size Page Showcase cards

Every Current / Corrected image is now rendered inside the same square preview
frame before Streamlit displays it.

This prevents portrait, wide, transparent or corrected images from physically
changing the card height.

The product-title area and Corrected / Original-kept status area are also
height-controlled for a more even grid.

## Advanced Audit pages can open in Page Showcase

Page Showcase can now use either:

- Latest Detection Test
- Advanced Audit page

For an Advanced Audit crawl, choose Page 1, Page 2, Page 3, etc. from a
selector and preview only that page.

The app does not need to render the entire multi-page crawl at once.

## Per-product categories on mixed pages

A broad Home Appliances page can contain unrelated product families.

V6.6 therefore infers the technical category from each individual product name
when possible. Examples include:

- Vacuum cleaner
- Juicer
- Waffle maker
- Ice cream maker
- Citrus press
- Hood

Page-level category remains the fallback.

Comparison groups also include the per-product family so a waffle maker is not
used as a visual baseline for a robot vacuum simply because both happen to have
a similar geometric profile.

## Correct image/name association

Product names are now taken from the same DOM product card as the image.

This prevents a product image from accidentally inheriting the title of a
neighboring card in a complex grid.

## Multipart compositions

Multi-part sold compositions now use the complete composition as the scaling
reference.

Examples:

- juicer + supplied jug
- robot vacuum + docking station
- structural multi-module appliances

The previous primary-body scaling could make an already-good composition much
too small.

## Bundle layouts

V6.6 detects distributed accessory layouts such as a floor-care system shown
with multiple included heads/tools.

These use the complete visible composition for:

- scale
- horizontal centering
- vertical centering

rather than centering only the largest upright object.

This is intended to fix large one-sided negative space in bundle images.

## Visual-mass centering

`visual_center` is a new anchor mode.

It centers the foreground mask's visual mass rather than the bounding-box
center.

The default Hood policy uses this because a tall, narrow chimney above a wide
hood canopy can make geometric bbox centering leave the visually important
canopy too low.

The manual per-product tuner also exposes:

**Visual mass center — asymmetric product**


---

# V6.6.1 Exact Same-Archetype Pixel Lock

V6.6.1 addresses the remaining small size mismatch visible between very similar
products such as ECOVACS robot vacuums with docking stations.

## Why a 1–2 px mismatch could survive

The normalizer already locked the product anchor to exact output pixels, but the
foreground mask itself is discrete.

After proportional resizing, two equivalent source images could land at:

```text
819 px
820 px
```

even when both were aiming at the same target.

Small source-crop differences, soft floor shadows and mask resampling could also
push one product across a geometric profile threshold.

## Exact integer size lock

The normalizer now performs:

1. proportional target scaling
2. up to six correction passes
3. a small discrete scale search around the result
4. selection of the candidate with the lowest integer-pixel size error

No non-uniform stretching is used.

New normalization metrics:

```text
size_lock_target_px
size_lock_actual_px
size_lock_error_px
size_lock_ok
```

When the product can safely fit at the target, `size_lock_error_px` should be 0.

## Stable robot-vacuum grouping

Vacuum-cleaner products with similar robot-vacuum/station geometry now use a
stable boxy visual profile instead of being allowed to switch between different
profiles because of a tiny crop/aspect-ratio difference.

The detector also adds a lightweight grouping archetype such as:

```text
robot_vacuum_station
robot_vacuum
cordless_floorcare
```

This keeps equivalent products together for page-level consistency checks
without forcing unrelated vacuum formats to use the same visual standard.


---

# V6.6.2 Filtered Product Inspector

V6.6.2 makes the category-filter stage fully inspectable.

The page scout has always counted products that were removed before visual
analysis. The new **Filtered Product Inspector** now shows exactly what those
products were and why they were excluded.

For each filtered product the app can show:

- product image preview
- product name
- expected page category
- category detected for the product
- filter confidence
- exact category phrase(s) that triggered the decision
- complete category score breakdown
- human-readable skip reason
- scouting source
- page-membership method
- direct product-page link

Example:

```text
Expected page category: Coffee machine
Detected category:      Refrigerator
Filter confidence:      High

Evidence:
'refrigerator'
'fridge'

Reason:
Detected as Refrigerator while page category is Coffee machine.
No Coffee machine category phrase was found in the product name/URLs.
```

The inspector appears in:

- Detection Test
- each page inside Advanced Audit

It also includes a downloadable:

```text
filtered_products.csv
```

so skipped items can be reviewed outside the app.

This is a diagnostics feature only. It does not change the normalization or
category-filter decision thresholds.


---

# V6.6.3 Editable Filter Overrides

V6.6.3 turns the Filtered Product Inspector into an editable review workflow.

## Include a wrongly filtered product

Each filtered product now has:

**Include in audit**

When clicked, the app:

1. records a manual include override for that exact product
2. optionally applies a manually selected product category
3. reruns only that page
4. sends the product through normal image analysis
5. removes it from the filtered list if it was successfully included

The user can choose:

```text
Use page / automatic category
Coffee machine
Vacuum cleaner
Juicer
Hood
...
```

when the category itself was classified incorrectly.

## Reset manual includes

If a manual inclusion was wrong, use:

**Reset manual includes**

The page is rerun using the automatic category gate again.

## Separate filtered-products CSV button

The Filtered Product Inspector now exposes a prominent standalone:

**Filtered Products CSV (N)**

button before the details expander.

The CSV contains:

- product name
- expected category
- detected category
- filter confidence
- category score
- evidence terms
- complete filter reason
- product URL
- image URL
- scout source
- page-membership method

A second detailed CSV button also remains inside the inspector.

## Detection Test and Advanced Audit

Manual filtering controls work in:

- Detection Test
- each individual Advanced Audit page

For Advanced Audit, only the selected page is rerun after an override; the
entire multi-page crawl does not need to run again.


---

# V6.6.4 Prominent Filter Review + Dedicated Tab

V6.6.4 makes filtered products much easier to find after an audit.

## Prominent audit toggle

Advanced Audit now places these controls together:

```text
Show passing products too
Show filtered-out products (N)
```

Turning on **Show filtered-out products** displays the filtered-product editor
under the relevant audit pages.

Detection Test also gets a prominent:

```text
Show filtered-out products (N)
```

toggle directly below the scan summary.

## Dedicated `Filtered Out Images` tab

A new main application tab is available:

```text
🚫 Filtered Out Images
```

The tab can show filtered products from:

- Latest Detection Test
- Latest Advanced Audit

For Advanced Audit it supports:

```text
All pages
Page 1
Page 2
Page 3
...
```

### All pages

Shows a read-only visual gallery of every filtered product from the crawl and
provides one dedicated CSV containing all filtered products.

### Individual page

Opens the complete editable Filtered Product Inspector for that page, including:

- image preview
- product name
- expected category
- detected category
- filter confidence
- evidence terms
- complete reason
- direct product link
- manual category selector
- `Include in audit`
- `Reset manual includes`

Only that page is rerun after an override.

## Dedicated filtered CSV

The new tab exposes prominent download buttons such as:

```text
Download ALL Advanced Audit filtered products (N)
Download Detection Test filtered products (N)
```

These are separate from the normal audit CSV/report exports.


---

# V6.6.5 Audit Category-Gate Accuracy Fix

This release fixes misleading filter messages such as:

```text
Detected as Juicer while page category is Kettle
```

on broad mixed collections.

## Root cause

Many Shopify themes render product titles as `<h2>`. Older builds included the
first few H1/H2 headings when inferring the page category.

That meant a broad **Small Appliances** page could accidentally become a
**Kettle** audit page simply because one of the first product cards was a
kettle.

The individual product classifier could still correctly identify another item
as **Juicer**, producing the confusing mismatch.

## New behavior

Page-level category-gate inference now uses only listing-level evidence:

- collection URL
- document title
- primary H1
- breadcrumb / collection-title / category-title / page-title

Product-card H2/H3 headings are excluded.

For broad or mixed pages, FBF can now report:

```text
Audit category gate: Mixed / disabled
```

The products are then classified individually.

The UI now uses **Audit category gate** instead of **Page category** because
this field is an internal filtering rule, not the website's real Shopify /
collection category.


---

# V6.7 Single Product Inspector + Remembered Exception Tuning

V6.7 adds two major workflows.

## 1. Single product / appliance detection

Detection Test now starts with:

```text
Entire catalog / category page
Single product / appliance
```

In single-product mode, paste a direct product-detail URL.

FBF resolves the main product name and hero image from:

1. Product JSON-LD
2. OpenGraph hero image + H1
3. DOM fallback

The product is then analyzed without requiring a full catalog page.

Because there is no peer group, single-product detection uses:

- product category/profile standard
- canonical framing sanity
- normalizer-agreement prediction
- horizontal/vertical alignment
- crop/edge safety
- segmentation confidence

The single-product result can be opened directly in **Page Showcase**, which
means one problematic appliance can be tuned without rescanning the whole
category.

## 2. Product exception tuner memory

The old V6 exception tuner was a session-only override tool.

It allowed one product to override:

```text
Body interpretation
Alignment reference
Extra size adjustment
Horizontal nudge
Vertical nudge
Baseline trim
```

V6.7 keeps those controls and adds:

### Live tuned preview

Every slider/dropdown change immediately rebuilds one preview so the operator
can visually finish the product before saving it.

### Apply to this page

Stores the tuning in the current Streamlit session and applies it to Page
Showcase + one-click export.

### Save & remember

Also stores the tuning in:

```text
audit_data/product_exception_library.json
```

The remembered library is local to the FBF installation.

### Future suggestion

When the same appliance appears again, FBF checks:

1. exact canonical product URL
2. exact normalized product name

If a saved tuning is found, the tuner displays:

```text
Saved tuning found
Apply remembered tuning
Forget remembered tuning
```

Remembered tuning is never silently auto-applied. The operator explicitly
chooses whether to reuse it.

This gives FBF a lightweight operator-approved correction memory without
turning old manual decisions into hidden automatic rules.


---

## V6.8 Showcase Editor Upgrade

V6.8 improves the Page Showcase workflow for manual finishing.

### New in Page Showcase

- **Tune this product** button under every product card
- **Use as stencil** button under every product card
- **Neighbour comparison rail** in the exception tuner
- **Stencil / ghost overlay helper** with adjustable opacity
- **Download current showcase images** ZIP button
- **Quick export shortcut** near the top of Detection Test

### Why this matters

Some edge cases cannot be solved perfectly with automatic rules alone,
especially when:

- two appliances are visually almost identical but end up 1–2 pixels off
- one product should match its nearby neighbour almost exactly
- the operator wants to override a “correct” result that still looks wrong

The new flow lets the operator:

1. click a product directly from Page Showcase
2. compare it against left/right products
3. use another product as a stencil reference
4. tune scale/position manually
5. save the result and optionally remember it for future use

### Showcase ZIP

The new showcase ZIP is intentionally simple and fast:

```text
showcase_images/
showcase_manifest.csv
```

It downloads the images exactly as the current Page Showcase is displaying them.
For the larger automation package with product folders, comparison previews and
manifest, use **Detection Test → One-click page automation**.


---

## V6.8.1 Streamlit Showcase Editor Hotfix

V6.8's per-card **Tune this product** and **Use as stencil** buttons could raise:

```text
StreamlitAPIException:
st.session_state.v6_tune_product cannot be modified after the widget
with key v6_tune_product is instantiated.
```

The root cause was Streamlit's widget lifecycle rule: once a widget using a
session-state key has been created during a run, that same key cannot be
programmatically changed later in that run.

V6.8.1 fixes this using a pending-selection workflow:

```text
Card button
  → save pending product key
  → st.rerun()
  → consume pending key before selectbox creation
  → safely update widget selection
```

The same fix is applied to the stencil selector.


---

# V6.9 Amazon A Mode — Default Square 1:1 Catalogue Standard

Amazon A Mode is now the default:

```text
Amazon A Mode — Square 1:1
Standard
Strict / page-calibrated
```

On a 1000×1000 square output:

- Tall: 880 px target height
- Boxy: 840 px controlling envelope
- Wide: 900 px target width
- Flat: 900 px target width
- Compact: 800 px controlling envelope

Default minimum safe padding is 4%.

Tall products share height/alignment and naturally keep side padding. Wide/flat
products share width/alignment and naturally keep top/bottom padding. Boxy and
compact products keep balanced all-side whitespace.

Only proportional scaling is used; products are never stretched or squashed.


---

# V6.10 Advanced Audit Image Review Package

Advanced Audit now provides two separate download workflows:

```text
📄 CSV / spreadsheet data
🖼️ Images + issue data
```

The image workflow builds a production handoff ZIP containing three top-level
directories:

```text
01_NEEDS_REVIEW/
02_PASSED/
03_ALL_PRODUCTS/
```

Each directory is organized by audit page and then by the product name from the
website.

Example:

```text
01_NEEDS_REVIEW/
  Page 1/
    Sage BJE430SILUK the Nutri Juicer Cold/
      01_ORIGINAL.png
      02_FIXED.png
      03_BEFORE_AFTER.jpg
      issue_and_fix.txt
      fix_prompt.txt
```

`01_NEEDS_REVIEW` contains only products flagged by the audit.

`02_PASSED` contains products that require no review. For passed products,
`02_FIXED.png` is intentionally the original image because no replacement is
required.

`03_ALL_PRODUCTS` contains a complete mixture of both groups for full-catalogue
handoff.

The package also contains `PACKAGE_MANIFEST.csv`, but the visual workflow does
not depend on the CSV.

The issue TXT includes:

- exact website product name
- direct product URL
- page
- audit status
- framing score
- detected profile and subtype
- product category
- detected problems
- suggested fix
- selected normalization mode
- generated normalization QA
- error details when a fix could not be generated

The prompt TXT provides a concise correction brief that can be handed to a
designer, editor, or image-generation/editing workflow.

The package is built directly from the latest Advanced Audit and does not
require saving the crawl into the persistent master audit first.


---

# V7.1.0 — Page Showcase Review + Editing Rework

V7.1.0 starts the V7 branch and focuses on human-in-the-loop visual approval.

## Press-and-hold old/new comparison

Changed Page Showcase cards now use an interactive browser-side comparison:

```text
FIXED is visible
PRESS & HOLD: ORIGINAL
release → FIXED returns immediately
```

If the card is flipped to Original, the direction reverses.

There is also a normal `Flip to original / Flip to fixed` button for repeated
click-based comparison.

## Manual image decision

Each changed card now has:

```text
Keep original
Use fixed
Reset manual review decision
```

`Keep original` means the generated replacement is rejected.

`Use fixed` means the generated normalized replacement is approved.

The decision is synchronized across every currently loaded copy of the product:

- Page Showcase
- Detection Test scan state
- Advanced Audit scan state
- current Advanced Audit CSV downloads
- Page Showcase exports
- Advanced Audit image-review package

The app preserves the original automatic detector findings internally so a
manual approval does not erase the audit trail.

## CSV changes

CSV exports now include:

```text
Operator Decision
```

Manual decisions appear as:

```text
APPROVED ORIGINAL
APPROVED FIX
```

The problem summary keeps the original automatic findings while making the
operator's decision explicit.

## V7.1 Focused Product Editor

The old V6 Product Exception Tuner has been replaced by a focused editor.

Card buttons now work as direct selectors:

```text
Tune this product → exact card becomes EDIT TARGET
Use as stencil    → exact card becomes STENCIL REFERENCE
```

Both are visibly highlighted in the Page Showcase grid.

The editor displays:

- exact edit target
- exact stencil reference
- original target
- live tuned target
- stencil image
- stencil + tuned ghost overlay
- body interpretation
- alignment reference
- fine 0.5% size controls
- fine horizontal / vertical / baseline controls
- live size-lock and anchor QA
- remembered tuning load / forget
- Apply tuning
- Apply + remember
- Reset tuning

The stencil may be the same product as the edit target; in that case the
original image acts as the reference for the newly tuned version.

## Export behavior

`Keep original` is honored by Page Showcase and page exports.

`Use fixed` is honored even after the manual decision changes the audit status
to an approved/pass state.

The V6.10 Advanced Audit visual package also understands both decisions.


---

# V7.1.1 — Direct Drag / Resize Product Editing

V7.1.1 upgrades the Page Showcase editor from slider-first editing to direct
visual manipulation.

## Drag the appliance

The focused editor now contains an interactive square editing canvas.

```text
drag product body     → move product
drag blue corner      → zoom/resize proportionally
```

The product foreground is isolated from the normalized image and rendered as a
transparent movable layer.

The selected stencil appears underneath as a ghost reference.

Four corner handles resize proportionally; the image is never stretched.

## No external frontend dependency

The draggable editor is implemented as a local Streamlit custom component in:

```text
product_transform_component.py
components/product_transform/index.html
```

The frontend communicates with Streamlit through the component protocol
directly. No npm package or CDN is required.

## Simpler wording

The main editor controls are now:

```text
Zoom / size
Move left / right
Move up / down
```

Advanced options are moved into a separate expander:

```text
What should move together?
What should be centered?
Raise / lower the product floor line
Stencil visibility
```

## Scale / zoom

`Zoom / size` is presented as a direct multiplier:

```text
1.00× = automatic size
1.10× = 10% larger
0.90× = 10% smaller
```

The underlying FBF exception format still stores `scale_bias`, so existing saved
tuning remains compatible.

## Drag conversion

A finished drag gesture is converted back into FBF's existing tuning variables:

- proportional scale
- horizontal position
- vertical position

The normalizer remains the source of truth; the direct editor is simply a much
easier visual controller for the same system.

## Apply behavior

Dragging and sliders update the live editor state.

The operator still explicitly chooses:

```text
Apply this edit
Apply + remember
Reset edit
```

before the edit becomes the Page Showcase/export override.


---

# V7.1.2 — Page Showcase Display + Performance Hotfix

V7.1.2 fixes the Page Showcase regression introduced by the V7 comparison UI.

## Full products visible again

The per-card press-and-hold comparison used an embedded iframe with a fixed
height. On wider product cards, the square image area could become taller than
the iframe itself, which visually clipped the lower part of the appliance.

V7.1.2 removes the iframe from every catalogue card.

Cards now use the normal cached square Streamlit preview again, so the entire
product stays inside the 1:1 image well.

## Faster old / new comparison

The catalogue grid now uses the lightweight:

```text
Flip to original
Flip to fixed
```

workflow.

The expensive per-card JavaScript iframe comparison has been removed from the
grid. Direct visual editing remains available in the single V7.1.1 product
editor where only one custom component is needed.

## Cleaner cards

The following large card banners were removed:

```text
EDIT TARGET
STENCIL REFERENCE
MANUAL DECISION — KEEP ORIGINAL
MANUAL DECISION — USE FIXED
```

Target and stencil context is shown inside the focused editor, where it belongs.

The large `Keep original / Use fixed` button pair was also removed from every
card.

Final output choice is now managed once inside the focused editor:

```text
Automatic
Original image
Edited image
```

## Performance work

1. Square card previews are now cached with `st.cache_data`.
2. Preview PNGs use faster compression instead of `optimize=True`.
3. The showcase ZIP is generated only after pressing `Prepare showcase download`.
4. Full before-vs-fixed duplicate grids are disabled by default and rendered
   only when explicitly requested.
5. Dozens of per-card iframe components are no longer created on every rerun.

These changes are especially important when a page contains many commercial
appliances or high-resolution product images.


---

# V7.1.3 — Fragment Cards + Dedicated Product Editor

V7.1.3 targets interaction speed and layout stability.

## Page Showcase cards are isolated fragments

Each card now uses `st.fragment`.

`Flip to original`, `Flip to fixed`, and `Keep original` rerun only the card
that was clicked instead of rerunning the entire app.

This removes the previous full-page scroll/jump/"earthquake" effect.

## Keep Original is back on the card

Every changed card again has:

```text
Keep original
```

The action updates only that product, switches its card to the original image,
syncs the human decision into Detection Test/Advanced Audit state, invalidates
only report/download packages, and preserves the expensive normalized showcase
cache.

## Human decisions no longer rebuild normalization

The showcase cache signature now uses the original automatic audit status.

Original-vs-Edited approval is treated as a display/export decision over the
already generated images.

## Dedicated Product Editor tab

A new main tab is available:

```text
🎛️ Product Editor
```

The large drag/resize/tuning UI has been removed from Page Showcase.

Clicking `Tune` or `Stencil` on a card selects that exact product and performs
one app-level navigation rerun. A small one-shot browser helper attempts to
switch directly to Product Editor.

Inside Product Editor, the entire editing workspace is itself a fragment. Drag,
resize, slider and stencil interactions therefore do not rerun the rest of the
application.

## Streamlit version

V7.1.3 requires Streamlit 1.37+ for fragment support.

The BAT launcher now verifies `st.fragment`; an older existing `.venv` will
trigger a requirements update.


---

# V7.1.4 — Product Editor Stability Hotfix

V7.1.4 fixes two issues in the direct product editor.

## Infinite-scroll fix

The old custom component calculated its iframe height using:

```text
document.documentElement.scrollHeight
```

Because iframe height can itself affect `scrollHeight`, repeated Streamlit
renders could create a positive feedback loop where the editor became taller
and taller forever.

V7.1.4 now:

- measures only the actual editor root element
- ignores the iframe/document scroll height
- sends a new frame height only when the measured root changes materially
- sets `html, body { overflow: hidden; }`

The editor therefore has a finite layout instead of continuously extending the
page.

## Snap-back fix

The old component copied `initial_box` into the browser transform state on every
`streamlit:render` event.

A drag emitted a value → Streamlit rerendered the fragment → the component
received `initial_box` again → the product visibly snapped back.

V7.1.4 uses a `reset_token`.

The transform is initialized only when:

- a different target product is opened
- body interpretation changes
- centering mode changes
- Reset Editor is pressed
- remembered tuning is explicitly loaded

Normal rerenders no longer reset the browser transform.

## No second rerun after drag

A completed drag is converted into FBF scale/X/Y values and stored as the
editor draft during the same fragment run.

The old immediate second `st.rerun(scope="fragment")` was removed.

This reduces latency and prevents the browser transform from being interrupted.

## Controls moved beside the image

The direct editing component now contains its main controls beside the canvas:

```text
Zoom / size
Move left / right
Move up / down
Stencil visibility
Center
Reset
```

Dragging the product updates these same controls, and moving the controls edits
the same draggable box.

Streamlit-only settings such as body interpretation and centering policy stay
in a compact Advanced section to the right of the editor.

## Apply behavior

Direct dragging edits a draft first.

Use:

```text
Apply
Apply + remember
```

to commit it to Page Showcase / exports.

This preserves the human-in-the-loop workflow without forcing a normalizer
rebuild on every mouse movement.


---

# V7.1.5 — Refreshed CSV + Selected-Page Showcase Cache

## Refresh CSV from current manual edits

Advanced Audit now uses an explicit two-step spreadsheet workflow:

```text
1. Refresh CSV from current edits
2. Download REFRESHED SIMPLE CSV
```

This prevents the operator from accidentally downloading the old pre-edit CSV
after manually tuning products in Product Editor.

The refreshed Simple CSV includes the current manual editor parameters:

```text
Manual Edit
Manual Zoom
Manual Left / Right
Manual Up / Down
Manual Floor Line
Manual Body Mode
Manual Center Mode
Operator Decision
```

The separate per-page CSV ZIP is refreshed by the same button.

New Advanced Audit crawls and manual edit/decision changes invalidate the
prepared CSV snapshot.

Persistent Advanced Audit visual media and the Images + Issue Data package now
also use Product Editor overrides when generating normalized images.

## Page Showcase no longer runs the 1→N audit path on page changes

Page Showcase is now one isolated Streamlit fragment.

Changing:

```text
Advanced Audit page to preview
Page 1 → Page 2 → Page 3
```

reruns only Page Showcase. The Advanced Audit tab and its 14-page product
rendering loop are not executed again.

## Per-page normalized showcase cache

Normalized showcase results are cached separately per selected page.

The first time Page 2 is opened, FBF processes Page 2 only.

If the operator later returns to Page 2 with the same settings, its prepared
showcase is reused immediately.

The cache keeps up to 24 page/configuration combinations per session.

A real tuning change invalidates only the currently edited showcase page rather
than throwing away every cached page.

Human Original/Edited approval still does not rebuild normalization.


---

# V7.1.6 — Undo Keep Original

## Page Showcase accidental-click recovery

Page Showcase now supports a direct undo path for accidental **Keep original** clicks.

When a product is marked as **Keep original**, the same card button changes to:

```text
↺ Undo keep original
```

Clicking it clears the manual decision for that product and returns the card to the edited/fixed preview path.

## Manual tuning is preserved

Choosing **Keep original** no longer deletes saved Product Editor overrides.
That means if the operator changes their mind later, the existing tuning can still be reused instead of being lost.


---

# V7.1.7 — Complete Rework Flag

V7.1.7 adds a human-review state for product images that are unusable in both their original and generated-normalized forms.

## New decision

```text
COMPLETE REWORK
```

Use this when the source image itself is too damaged, badly composed, incorrect, low quality, or otherwise unsuitable to rescue with framing/normalization.

The flag is available in **Page Showcase**, **Advanced Audit**, and **Product Editor**. It can also be undone.

## CSV behavior

Refreshed Simple/Advanced CSV rows show:

```text
Product Status: COMPLETE REWORK
Operator Decision: Complete rework required
```

The problem summary explicitly says that neither the original nor generated fix should be used and that a replacement/rebuild is required.

## Advanced Image Review ZIP

A COMPLETE REWORK product remains under `01_NEEDS_REVIEW` and `03_ALL_PRODUCTS`, preserving the existing three-directory structure. Its product folder is intentionally minimal:

```text
Product Name/
  01_ORIGINAL.png
  COMPLETE_REWORK_PROMPT.txt
```

No `02_FIXED.png`, no before/after image, and no generated-fix report are included for these products. The prompt explains that a completely new/reworked source image is required.

## Export behavior

The normal page-export ZIP also routes COMPLETE REWORK products into a `complete_rework/` folder with only the source reference image and replacement brief.


---

# V7.1.8 — Product Editor / Showcase State Sync

## Editor Apply now updates Page Showcase immediately

`Apply` and `Apply + remember` now normalize only the selected product and patch that product into the current Page Showcase cache. The complete page is not rebuilt. Applying an edit also selects the edited version as the active final image, while **Keep original** remains available if the operator changes their mind.

## Tune / Stencil routing no longer uses browser tab-click JavaScript

The top-level app now uses a lightweight server-side workspace router. Clicking **Tune this product** or **Use as stencil** stores the exact product key, requests Product Editor, and performs one lightweight route rerun. Detection Test, Advanced Audit, Filtered Images and Page Showcase are not executed on that navigation run.

This removes the old behavior where clicking Tune appeared to reload/reprocess the entire application just to change tabs.

## Stencil selection repaired

The editor validates target and stencil keys against the currently selected showcase page. A stale target from another page can no longer force the editor back to the first product. If **Use as stencil** is the first action for a page, that exact clicked product becomes the reference and, when necessary, the initial edit target too.


---

# V7.2.0 — Reliability + Crop Editor

V7.2.0 is a broader reliability pass over the V7 workflow rather than one
single hotfix.

## Page Showcase scroll stability

Page Showcase now keeps a lightweight browser-side scroll bookmark per selected
catalogue page. Fragment reruns restore the viewer to the same vertical
position, so clicking Flip / Keep Original / Undo / Complete Rework should no
longer leave the operator looking at a different appliance farther down the
page.

The scroll bookmark is scoped to the selected page. Opening a different audit
page does not inherit the previous page's scroll position.

## Crop / trim in Product Editor

The direct editor now includes:

```text
Crop left
Crop right
Crop top
Crop bottom
```

The crop is relative to the detected foreground product envelope. The red crop
guide shows what remains.

The normalizer applies the crop to the segmentation mask before component
analysis, then performs the normal scale/alignment pipeline on the remaining
product. Crop values therefore persist into Page Showcase, page exports,
Advanced Audit image packages, CSV reports and remembered product exceptions.

Safety validation prevents a crop that removes essentially the whole product.

## Product Editor Undo / Redo

The Product Editor now keeps up to 60 draft states per product during the
session.

```text
Undo
Redo
```

covers drag, resize, crop and advanced alignment/body changes. Making a new edit
after Undo starts a new history branch, as expected in a normal editor.

## Showcase cache logic fix

Manual Product Editor values are no longer part of the page-level normalization
cache identity. A manual Apply already patches the affected product image into
prepared page caches directly, so including those same values in the page key
was causing an unnecessary whole-page rebuild when returning from Product
Editor.

V7.2.0 patches every prepared cache entry containing that product. Returning to
an already visited page cannot resurrect an older cached version of the edited
image.

## Additional reliability fixes

- Showcase ZIP preparation now reruns only the Showcase fragment.
- Local Showcase actions preserve the page's viewing position.
- Crop values are included in Simple and Advanced CSV exports.
- Apply + Remember stores crop values in the product exception library.
- Existing Keep Original, Undo Keep Original and Complete Rework workflows are
  preserved.


---

# V7.2.1 — Editor Preview + Showcase Sync Reliability

## Canonical applied-edit registry

Product Editor now writes every successful Apply into a session-level
`applied_edit_registry` keyed by the stable product key.

That registry is the first source of truth for the edited image. Page Showcase
cards, showcase downloads and rebuilt page caches therefore cannot silently
fall back to an older normalized image just because a cached object was stale.

## Product Editor preview

The middle Product Editor preview now shows:

```text
Live draft preview
```

before Apply, and:

```text
Applied edited image
```

after Apply / Apply + remember.

If a drag/crop operation changes the draft after the fragment's first
normalization, FBF recomputes only that live preview so the image shown below the
editor matches the current draft rather than the previous automatic base.

## Apply vs Apply + remember

`Apply` stores the tuning in the current Streamlit session and updates the
current product's edited image/showcase state.

`Apply + remember` does the same thing and additionally writes a persistent
product exception entry to `audit_data/product_exception_library.json`.

Remembering is product-specific. Matching is:

1. exact canonical product URL (highest confidence)
2. exact normalized product name if the URL changed/missing
3. category and archetype only increase confidence for that exact-name fallback

FBF does not currently apply remembered tuning to visually-similar products by
shape/image similarity, and remembered settings are offered as a suggestion
rather than silently applied.


---

# V7.2.2 — Streamlit iframe API migration

V7.2.2 removes the deprecated `st.components.v1.html` calls that Streamlit
1.56+ warns about.

The two tiny JavaScript helpers used for Page Showcase scroll-position
preservation now use the native:

```python
st.iframe(...)
```

API instead.

This keeps the same isolated JavaScript behavior while removing the warning:

```text
Please replace st.components.v1.html with st.iframe.
st.components.v1.html will be removed after 2026-06-01.
```

`app.py` no longer imports `streamlit.components.v1` for these helpers.

The custom bi-directional Product Editor component is unchanged. Its
`declare_component` integration is a different API and is still required.

V7.2.2 now requires:

```text
streamlit>=1.56
```

The Windows launcher verifies that both `st.fragment` and `st.iframe` are
available before starting the app.
\n\n---\n\n# V7.3.0 — Reliability, Security & Regression-Test Pass\n\nV7.3.0 is a focused reliability/security release. It preserves the established\nnormalization geometry for valid inputs while fixing state, persistence, export,\nscanner, filename, scoring and configuration-consistency problems.\n\nKey changes:\n\n- Product Editor Apply is transactional: normalization/validation happens before committed state changes.\n- Audit media directories use a stable product identifier so duplicate display names cannot overwrite one another.\n- Web requests use SSRF-aware URL/DNS/redirect validation, timeouts, response-size limits and bounded concurrent image downloading.\n- Export ZIP media references are constrained to `audit_data/`; traversal, absolute escapes and symlink escapes are skipped.\n- XLSX untrusted strings are explicitly written as text and workbook formula auto-detection is disabled.\n- Pages with zero successful analyses receive `N/A` score and `failed` status rather than 100/100.\n- Master-audit append deduplicates both incoming batches and existing records using canonical product identity.\n- Corrupted remembered-tuning JSON is preserved/backed up and normal writes are blocked instead of destroying history.\n- Remembered-tuning lookup is deterministic: exact URL first, then exact-name fallback, newest record within the same match level.\n- Editor reset synchronization includes scale, X/Y, baseline, crop, body/anchor mode, product, category, canvas and normalization mode.\n- `profile_config.py` is the runtime source of truth; `profiles.json` is generated from it.\n- One Windows-safe path sanitizer is shared by audit storage and application exports.\n- Crop range is consistently 0–45% in Python, persistence and the browser editor.\n- Scanner image fetching uses at most six workers and downloads each identical image URL once per scan.\n- Failed Detection/Advanced Audit retries keep the previous successful result.\n\nDevelopment regression tests are in `tests/`. Install `requirements-dev.txt` and run:\n\n```text\npython -m pytest -q\n```\n\nSee `RELIABILITY_REPORT_V7_3_0.md` for root causes, test coverage, compatibility notes,\nremaining security considerations and browser-level manual QA items.\n

---

# V7.3.1 — Filtered Inspector Fragment-Rerun Hotfix

V7.3.1 fixes a Streamlit runtime crash encountered when manually including a
filtered product from the Detection Test / Advanced Audit / Filtered Out Images
inspector.

The failing path called:

```python
st.rerun(scope="fragment")
```

from `render_filtered_product_inspector()`, which is a normal helper rendered by
the workspace and is not an `@st.fragment` function. Current Streamlit rejects
that call with `StreamlitInvalidLayoutContextError`.

The action now uses a normal `st.rerun()` because including a previously
filtered product mutates the underlying Detection/Advanced Audit scan and the
surrounding workspace must refresh as well.

A structural regression test now parses `app.py` and fails if a future
`scope="fragment"` rerun is placed outside an `@st.fragment`-decorated function.
# Federal_Bureau_Of_Framing
