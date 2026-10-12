---
version: 1
slug: "docs-index-html"
primary_target: "docs/index.html"
related_targets: []
---

# Surface: web-app de stock (docs/index.html)

Mode: Operate. Single user, mobile first (S23 FE, PWA), often arriving from an ntfy notification; job: see what just came in and buy fast; second job: compare price across shops.

## Direction contract

THESIS: A price-comparison app that opens on what just arrived. Refuses the generic offer-per-card grid: one card per product, "desde" price, shops inside.
OWN-WORLD: Vinted/Wallapop catalog grammar (white ground, grey image wells, bold tabular price, chip filters, underline category tabs) plus Idealo offer sheet (shops ranked by price, "Ir a la tienda"). One green accent for actions and "Nuevo"; red for price drops; amber for below Cardmarket.
STORY: Sees what is new and when, compares the cheapest shop, taps through to buy.
FIRST VIEWPORT: Compact bar (icon, name, "comprobado hace X min"), search, collection tabs; then "Novedades" rows with time, price and Comprar; product grid starts below.
FORM: Category canon pinned by the user (references Vinted/Wallapop + Idealo/PriceRunner); no seed roll.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
