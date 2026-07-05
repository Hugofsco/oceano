---
name: schema-markup
description: implement, audit, or validate JSON-LD structured data (schema.org) for rich results and AI-search legibility — use for "structured data", "schema markup", "FAQ schema", "rich results", or when Search Console reports structured-data errors
status: published
notes: ported from claude-skills marketing-skill/schema-markup (MIT); script copied verbatim, stdlib-only
---
# Schema markup

Use JSON-LD, always — Google recommends it, and it's the only kind worth adding new
(Microdata/RDFa are legacy). Placement: a `<script type="application/ld+json">` block
in `<head>`.

```
python3 skills/schema-markup/scripts/schema_validator.py <page.html>
```
Extracts JSON-LD blocks, checks required fields per type, scores completeness 0-100.
Also test at `search.google.com/test/rich-results` and `validator.schema.org` before
publishing — the local script catches structure, those catch real-world eligibility.

Type by page: homepage → `Organization` + `WebSite`; blog post → `Article` +
`BreadcrumbList`; FAQ content → `FAQPage`; product page → `Product` + `Offer` +
`AggregateRating`. Never add `Product` schema to a page that doesn't sell a product —
Google penalizes the mismatch.

Mistakes that actually break rich results: missing `@context`, a relative `image` URL
(must be absolute), `dateModified` older than `datePublished`, a `Product` with no
`Offer` block, schema that doesn't match the visible page content.

For AI-search citation (not just Google rich results): add `FAQPage` to any Q&A content,
`author` with a real `sameAs` profile link, and keep `dateModified` accurate — this
complements [[aeo-content-citations]] and [[seo-technical-audit]] rather than replacing
either.
