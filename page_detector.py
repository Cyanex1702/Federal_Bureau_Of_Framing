from __future__ import annotations

import io
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import numpy as np
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

from normalizer import (
    CATEGORY_STANDARDS,
    PROFILES,
    _profile_for,
    analyze_source_bytes,
    resolve_anchor_mode,
    resolve_scale_basis,
)
from security_utils import NetworkFetchError, URLValidationError, safe_get, validate_public_url


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def _clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:240]


def _best_src_from_srcset(srcset: str) -> Optional[str]:
    if not srcset:
        return None
    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        score = 1
        if len(bits) > 1:
            descriptor = bits[-1]
            try:
                if descriptor.endswith("w"):
                    score = int(descriptor[:-1])
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 1000
            except Exception:
                score = 1
        candidates.append((score, url))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def _image_url_from_tag(img, base_url: str) -> Optional[str]:
    srcset = (
        img.get("srcset")
        or img.get("data-srcset")
        or img.get("data-lazy-srcset")
    )
    src = _best_src_from_srcset(srcset) if srcset else None
    if not src:
        for attr in (
            "data-zoom-image",
            "data-large_image",
            "data-large-image",
            "data-original",
            "data-lazy-src",
            "data-src",
            "src",
        ):
            value = img.get(attr)
            if value:
                src = value
                break
    if not src:
        return None
    src = src.strip()
    if src.startswith("data:"):
        return None
    return urljoin(base_url, src)


def _looks_like_noise_name(name: str) -> bool:
    n = _clean_text(name).lower().strip()

    # These are UI / website-chrome phrases, not catalog products.
    bad = (
        "logo", "icon", "arrow", "chevron",
        "facebook", "instagram", "linkedin", "youtube", "twitter", "x.com",
        "placeholder", "spinner", "payment", "visa", "mastercard",
        "whatsapp", "search", "menu", "account", "cart",
        "follow us", "follow-us", "follow_us",
        "google play", "get it on google play", "play store",
        "app store", "download on the app store", "download app",
        "download our app", "mobile app", "app badge", "store badge",
        "newsletter", "subscribe", "copyright", "social media",
        "qr code", "scan qr", "contact us",
    )

    return (not n) or any(token in n for token in bad)



NON_PRODUCT_CONTAINER_RE = re.compile(
    r"""
    footer|header|navbar|nav-bar|navigation|
    social|follow|share|
    app[-_ ]?store|google[-_ ]?play|play[-_ ]?store|
    app[-_ ]?download|download[-_ ]?app|mobile[-_ ]?app|
    store[-_ ]?badge|app[-_ ]?badge|
    newsletter|subscribe|
    payment|payments|payment[-_ ]?method|
    copyright|legal|
    cookie|consent|
    contact[-_ ]?info|
    trust[-_ ]?badge|
    qr[-_ ]?code
    """,
    re.I | re.X,
)

NON_PRODUCT_URL_RE = re.compile(
    r"""
    facebook|instagram|linkedin|youtube|twitter|tiktok|
    whatsapp|pinterest|
    play\.google\.com|google[-_ ]?play|play[-_ ]?store|
    apps\.apple\.com|itunes\.apple\.com|app[-_ ]?store|
    social|follow|
    logo|icon|badge|
    newsletter|subscribe|
    payment|visa|mastercard|
    qr[-_ ]?code
    """,
    re.I | re.X,
)

PRODUCT_CONTAINER_RE = re.compile(
    r"product|catalog|listing|grid[-_ ]?item|product[-_ ]?item|product[-_ ]?card|woocommerce",
    re.I,
)


def _dom_semantic_text(node) -> str:
    if node is None:
        return ""

    attrs = []

    try:
        attrs.extend(node.get("class", []) or [])
        attrs.append(node.get("id", "") or "")
        attrs.append(node.get("role", "") or "")
        attrs.append(node.get("aria-label", "") or "")
        attrs.append(node.get("data-testid", "") or "")
    except Exception:
        pass

    return " ".join(str(x) for x in attrs if x)


def _has_product_like_ancestor(img, max_depth: int = 7) -> bool:
    parent = img

    for _ in range(max_depth):
        parent = getattr(parent, "parent", None)

        if parent is None:
            break

        semantic = _dom_semantic_text(parent)

        if PRODUCT_CONTAINER_RE.search(semantic):
            return True

    return False


def _has_non_product_ancestor(img, max_depth: int = 8) -> bool:
    parent = img

    for _ in range(max_depth):
        parent = getattr(parent, "parent", None)

        if parent is None:
            break

        tag_name = (getattr(parent, "name", "") or "").lower()

        # Semantic page chrome should never become a product candidate.
        if tag_name in {"footer", "header"}:
            return True

        semantic = _dom_semantic_text(parent)

        if NON_PRODUCT_CONTAINER_RE.search(semantic):
            return True

    return False


def _same_site_url(base_url: str, target_url: Optional[str]) -> bool:
    if not target_url:
        return True

    base = urlparse(base_url)
    target = urlparse(target_url)

    if not target.netloc:
        return True

    return (
        base.scheme.lower(),
        base.netloc.lower(),
    ) == (
        target.scheme.lower(),
        target.netloc.lower(),
    )


def _looks_like_non_product_image(
    img,
    base_url: str,
    image_url: str,
    name: str,
    product_url: Optional[str],
) -> tuple[bool, str]:
    """
    Reject website UI assets before CV analysis.

    This specifically prevents things such as:
      - "Follow Us" blocks
      - Google Play / App Store badges
      - footer logos
      - social-media graphics
      - payment/trust badges
      - newsletter/contact graphics

    from being analyzed as appliances.
    """
    searchable = " ".join(
        [
            name or "",
            image_url or "",
            product_url or "",
            img.get("alt", "") or "",
            img.get("title", "") or "",
        ]
    )

    if _looks_like_noise_name(searchable):
        return True, "UI / website-chrome text"

    if NON_PRODUCT_URL_RE.search(searchable):
        return True, "UI / social / store-badge URL"

    if _has_non_product_ancestor(img):
        return True, "Image is inside footer/header/social/app UI"

    # Product cards on the catalog itself normally link inside the same site.
    # External app-store/social links are not product pages.
    if product_url and not _same_site_url(base_url, product_url):
        return True, "External non-catalog link"

    return False, ""




# ------------------------------------------------------------------
# V6.5.3 STRICT PAGE MEMBERSHIP
#
# Advanced/page scanning must answer a very specific question:
#     "Is this product actually in the catalog listing on THIS page?"
#
# Product structured data, related-product blocks, recently-viewed sliders,
# upsells, and recommendations are NOT accepted as page membership evidence.
# ------------------------------------------------------------------

NON_LISTING_PRODUCT_CONTAINER_RE = re.compile(
    r"""
    related|recommend|recommended|recommendation|
    recently[-_ ]?viewed|recent[-_ ]?products?|
    upsell|up[-_ ]?sell|cross[-_ ]?sell|crosssell|
    similar|you[-_ ]?may[-_ ]?also[-_ ]?like|
    also[-_ ]?like|suggested|
    featured[-_ ]?products?|
    people[-_ ]?also[-_ ]?bought
    """,
    re.I | re.X,
)

IMAGE_ASSET_RE = re.compile(
    r"\.(?:jpe?g|png|webp|gif|svg|avif|bmp|tiff?)(?:$|[?#])",
    re.I,
)

NON_PRODUCT_LINK_RE = re.compile(
    r"""
    add[-_ ]?to[-_ ]?cart|wishlist|compare|
    quick[-_ ]?view|lightbox|
    login|account|cart|checkout|
    wp-content|uploads?/
    """,
    re.I | re.X,
)


def _has_non_listing_product_ancestor(img, max_depth: int = 9) -> bool:
    """
    Reject product-looking cards that are actually recommendations, related
    products, recently viewed products, sliders, etc.
    """
    parent = img

    for _ in range(max_depth):
        parent = getattr(parent, "parent", None)

        if parent is None:
            break

        semantic = _dom_semantic_text(parent)

        if NON_LISTING_PRODUCT_CONTAINER_RE.search(semantic):
            return True

        # Some themes have generic classes but explicit section headings.
        if getattr(parent, "name", None) in {
            "section",
            "aside",
        }:
            heading = None

            try:
                heading = parent.find(
                    ["h1", "h2", "h3", "h4", "h5", "h6"]
                )
            except Exception:
                heading = None

            if heading:
                heading_text = _clean_text(
                    heading.get_text(" ", strip=True)
                )

                if NON_LISTING_PRODUCT_CONTAINER_RE.search(
                    heading_text
                ):
                    return True

    return False


def _find_product_card(img, max_depth: int = 9):
    """
    Find the smallest ancestor that behaves like a catalog product card.

    V6.5.5 is deliberately tolerant here. Some real ecommerce grids do not
    include "product" in the card class name; they use generic wrappers such as
    "item", "tile", "grid-cell", "card", or custom data-product attributes.
    """
    parent = img

    for _ in range(max_depth):
        parent = getattr(parent, "parent", None)

        if parent is None:
            break

        semantic = _dom_semantic_text(parent)
        tag_name = (
            getattr(parent, "name", "")
            or ""
        ).lower()

        attrs = getattr(parent, "attrs", {}) or {}

        # Strong explicit ecommerce evidence.
        if any(
            key in attrs
            for key in (
                "data-product-id",
                "data-product_id",
                "data-product",
                "data-sku",
                "data-product-sku",
            )
        ):
            return parent

        if PRODUCT_CONTAINER_RE.search(semantic):
            return parent

        # Generic cards/tiles are common in custom sites.
        if re.search(
            r"\b(card|tile|grid[-_ ]?cell|grid[-_ ]?item|catalog[-_ ]?item|item)\b",
            semantic,
            re.I,
        ):
            try:
                has_link = bool(parent.find("a", href=True))
                has_name = bool(
                    parent.find(
                        ["h1", "h2", "h3", "h4", "h5", "h6"]
                    )
                    or parent.select_one(
                        "[class*='title'],[class*='name']"
                    )
                )
            except Exception:
                has_link = False
                has_name = False

            if has_link and has_name:
                return parent

        # Common semantic wrappers even with no useful classes.
        if tag_name in {"li", "article"}:
            try:
                has_heading = bool(
                    parent.find(
                        ["h2", "h3", "h4", "h5", "h6"]
                    )
                )
                has_link = bool(
                    parent.find("a", href=True)
                )
            except Exception:
                has_heading = False
                has_link = False

            if has_heading and has_link:
                return parent

    return None


def _is_plausible_product_detail_url(
    base_url: str,
    href: Optional[str],
) -> bool:
    if not href:
        return False

    absolute = urljoin(base_url, href)

    if not _same_site_url(
        base_url,
        absolute,
    ):
        return False

    parsed = urlparse(absolute)
    lowered = absolute.lower()

    if parsed.scheme not in {
        "http",
        "https",
    }:
        return False

    if IMAGE_ASSET_RE.search(lowered):
        return False

    if NON_PRODUCT_LINK_RE.search(lowered):
        return False

    if parsed.fragment and not parsed.path.strip("/"):
        return False

    return True


def _product_url_from_card(
    img,
    card,
    base_url: str,
) -> Optional[str]:
    """
    Resolve the actual product-detail URL, not an image/lightbox/add-to-cart URL.
    """
    candidates = []

    # The image link is often the product URL, so try it first.
    anchor = img.find_parent("a", href=True)

    if anchor:
        candidates.append(
            anchor
        )

    if card is not None:
        try:
            candidates.extend(
                card.find_all(
                    "a",
                    href=True,
                )
            )
        except Exception:
            pass

    scored = []

    for anchor in candidates:
        href = anchor.get("href")

        if not _is_plausible_product_detail_url(
            base_url,
            href,
        ):
            continue

        absolute = urljoin(
            base_url,
            href,
        )
        semantic = _dom_semantic_text(
            anchor
        ).lower()
        text = _clean_text(
            anchor.get_text(
                " ",
                strip=True,
            )
        ).lower()

        score = 0

        if "product" in semantic:
            score += 5
        if "woocommerce-loop-product" in semantic:
            score += 5
        if "title" in semantic:
            score += 3
        if anchor.find("img") is not None:
            score += 2
        if text and not any(
            token in text
            for token in (
                "read more",
                "learn more",
                "view",
                "details",
            )
        ):
            score += 1

        # Product URLs tend to have meaningful path depth.
        path_parts = [
            p
            for p in urlparse(
                absolute
            ).path.split("/")
            if p
        ]
        score += min(
            len(path_parts),
            3,
        )

        scored.append(
            (
                score,
                len(absolute),
                absolute,
            )
        )

    if not scored:
        return None

    scored.sort(
        key=lambda x: (
            x[0],
            x[1],
        ),
        reverse=True,
    )

    return scored[0][2]


def _dom_membership_key(
    item: dict,
) -> tuple[str, str]:
    """
    Stable matching key for JSON-LD enrichment without letting JSON-LD create
    new products on its own.
    """
    product_url = (
        item.get("product_url")
        or ""
    ).split("#")[0].rstrip("/").casefold()

    image_url = (
        item.get("image_url")
        or ""
    ).split("#")[0].casefold()

    return (
        product_url,
        image_url,
    )


def _enrich_dom_with_jsonld(
    dom_items: List[dict],
    jsonld_items: List[dict],
) -> List[dict]:
    """
    DOM establishes page membership.
    JSON-LD may improve a DOM item's name or product URL only when it matches
    that same DOM product by URL/image. JSON-LD is never allowed to inject an
    additional page product in strict mode.
    """
    json_by_product = {}
    json_by_image = {}

    for item in jsonld_items:
        product_url = (
            item.get("product_url")
            or ""
        ).split("#")[0].rstrip("/").casefold()
        image_url = (
            item.get("image_url")
            or ""
        ).split("#")[0].casefold()

        if product_url:
            json_by_product[
                product_url
            ] = item

        if image_url:
            json_by_image[
                image_url
            ] = item

    enriched = []

    for item in dom_items:
        product_key, image_key = (
            _dom_membership_key(
                item
            )
        )

        match = (
            json_by_product.get(
                product_key
            )
            if product_key
            else None
        )

        if match is None and image_key:
            match = json_by_image.get(
                image_key
            )

        merged = dict(item)

        if match:
            json_name = _clean_text(
                match.get("name", "")
            )

            if (
                json_name
                and json_name
                != "Unnamed product"
                and (
                    merged.get("name")
                    == "Unnamed product"
                    or len(json_name)
                    > len(
                        merged.get(
                            "name",
                            "",
                        )
                    )
                )
            ):
                merged["name"] = (
                    json_name
                )

            if (
                not merged.get(
                    "product_url"
                )
                and match.get(
                    "product_url"
                )
            ):
                merged[
                    "product_url"
                ] = match[
                    "product_url"
                ]

            merged[
                "structured_data_match"
            ] = True
        else:
            merged[
                "structured_data_match"
            ] = False

        enriched.append(
            merged
        )

    return enriched


def _name_near_image(img) -> str:
    alt = _clean_text(img.get("alt", ""))
    if alt and not _looks_like_noise_name(alt) and len(alt) >= 4:
        return alt

    # Walk up through likely product-card containers and prefer headings/links.
    parent = img
    for _ in range(6):
        parent = getattr(parent, "parent", None)
        if parent is None:
            break

        for selector in ("h1", "h2", "h3", "h4", "h5", "[class*='title']", "[class*='name']"):
            node = parent.select_one(selector) if hasattr(parent, "select_one") else None
            if node:
                text = _clean_text(node.get_text(" ", strip=True))
                if 4 <= len(text) <= 180 and not _looks_like_noise_name(text):
                    return text

        if getattr(parent, "name", None) in {"article", "li"} or (
            getattr(parent, "name", None) == "div"
            and re.search(r"(product|card|item|tile)", " ".join(parent.get("class", [])), re.I)
        ):
            text = _clean_text(parent.get_text(" ", strip=True))
            if 4 <= len(text) <= 180 and not _looks_like_noise_name(text):
                return text

    title = _clean_text(img.get("title", ""))
    if title and not _looks_like_noise_name(title):
        return title

    return "Unnamed product"


def _product_name_from_card(img, card) -> str:
    """
    Keep image/name association inside the same product card.

    The old fallback could walk up into a whole grid and accidentally take the
    heading from a neighboring product. That is how a robot-vacuum image could
    end up carrying a Sage waffle-maker title.
    """
    alt = _clean_text(img.get("alt", ""))
    if alt and not _looks_like_noise_name(alt) and len(alt) >= 4:
        return alt

    if card is not None:
        selectors = (
            "[class*='product'][class*='title']",
            "[class*='title']",
            "[class*='product'][class*='name']",
            "[class*='name']",
            "h1", "h2", "h3", "h4", "h5", "h6",
        )
        for selector in selectors:
            try:
                nodes = card.select(selector)
            except Exception:
                nodes = []

            for node in nodes:
                text = _clean_text(node.get_text(" ", strip=True))
                if (
                    4 <= len(text) <= 220
                    and not _looks_like_noise_name(text)
                ):
                    return text

        # Product-link text is a safer fallback than text from an outer grid.
        try:
            for anchor in card.find_all("a", href=True):
                text = _clean_text(anchor.get_text(" ", strip=True))
                if (
                    4 <= len(text) <= 220
                    and not _looks_like_noise_name(text)
                    and not re.search(
                        r"add to cart|get a quote|quick view|compare|wishlist",
                        text,
                        re.I,
                    )
                ):
                    return text
        except Exception:
            pass

    title = _clean_text(img.get("title", ""))
    if title and not _looks_like_noise_name(title):
        return title

    # Only use the broad legacy search when no product-card boundary exists.
    if card is None:
        return _name_near_image(img)

    return "Unnamed product"


def _iter_jsonld_objects(obj) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_jsonld_objects(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_jsonld_objects(item)


def _jsonld_products(soup: BeautifulSoup, base_url: str) -> List[dict]:
    products = []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        text = script.string or script.get_text()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            # Some sites place multiple JSON objects or malformed whitespace.
            continue

        for obj in _iter_jsonld_objects(payload):
            typ = obj.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if "Product" not in types:
                continue

            name = _clean_text(str(obj.get("name", "")))
            image = obj.get("image")
            if isinstance(image, dict):
                image = image.get("url") or image.get("contentUrl")
            if isinstance(image, list):
                image = next((x for x in image if isinstance(x, str)), None)
            if not isinstance(image, str):
                continue

            image_url = urljoin(base_url, image)
            product_url = obj.get("url")
            products.append(
                {
                    "name": name or "Unnamed product",
                    "image_url": image_url,
                    "product_url": urljoin(base_url, product_url) if isinstance(product_url, str) else None,
                    "source": "jsonld",
                }
            )
    return products



# ------------------------------------------------------------------
# V6.1 CATEGORY / PAGE-CONTEXT FILTER
#
# Some category pages contain "related products", footer carousels, or
# recommendation cards from other appliance categories. V6 treated those as
# normal page candidates. V6.1 filters obvious cross-category products before
# image analysis.
# ------------------------------------------------------------------
CATEGORY_TERMS = {
    "Coffee machine": [
        "coffee machine", "coffee maker", "coffee brewer", "espresso",
        "bean to cup", "bean-to-cup", "cappuccino", "coffee vending",
    ],
    "Microwave": [
        "microwave", "micro wave",
    ],
    "Oven": [
        "built in oven", "built-in oven", "electric oven", "convection oven",
        "combi oven", "commercial oven",
    ],
    "Refrigerator": [
        "refrigerator", "refrigeration cabinet", "fridge",
    ],
    "Freezer": [
        "freezer", "freezing cabinet",
    ],
    "Air conditioner": [
        "air conditioner", "air conditioning", "split ac", "split a/c",
        "cassette ac", "portable ac", "ducted ac",
    ],
    "Water heater": [
        "water heater", "geyser", "calorifier", "hot water boiler",
    ],
    "Hob": [
        "hob", "induction hob", "gas hob",
    ],
    "Citrus press": [
        "citrus press", "citrus juicer", "orange press",
    ],
    "Cooktop": [
        "cooktop", "cook top",
    ],
    "Hood": [
        "extractor hood", "range hood", "cooker hood", "chimney hood",
        "kitchen hood",
    ],
    "Dishwasher": [
        "dishwasher", "dish washer",
    ],
    "Laundry equipment": [
        "washer extractor", "washer-extractor", "washing machine",
        "tumble dryer", "tumble drier", "spin washer", "laundry machine",
        "industrial dryer", "commercial dryer",
    ],
    "Cart / trolley": [
        "cart", "trolley", "utility cart", "laundry cart", "linen cart",
    ],
    "Ice cream maker": [
        "ice cream maker", "ice-cream maker", "smart scoop",
    ],
    "Ice machine": [
        "ice machine", "ice maker", "ice-maker",
    ],
    "Juicer": [
        "juicer", "nutri juicer", "juice extractor",
    ],
    "Water dispenser": [
        "water dispenser", "water cooler",
    ],
    "Blender": [
        "blender",
    ],
    "Kettle": [
        "kettle",
    ],
    "Vacuum cleaner": [
        "vacuum cleaner", "robot vacuum", "robotic vacuum", "cordless vacuum",
        "wet and dry vacuum", "floor cleaner", "deebot", "roborock", "dyad",
    ],
    "Waffle maker": [
        "waffle maker", "waffle machine",
    ],
}


def _normalized_catalog_text(value: str) -> str:
    value = (value or "").lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^a-z0-9+/ ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _category_scores(text: str) -> dict:
    text = _normalized_catalog_text(text)
    scores = {}

    for category, terms in CATEGORY_TERMS.items():
        score = 0
        for term in terms:
            normalized_term = _normalized_catalog_text(term)
            if normalized_term and normalized_term in text:
                # Longer phrases are more trustworthy.
                score += max(2, len(normalized_term.split()))
        if score:
            scores[category] = score

    # "microwave oven" is still a microwave. Avoid simultaneously treating
    # the generic "oven" word as a conflicting oven category.
    if "Microwave" in scores:
        scores.pop("Oven", None)

    return scores


def _category_evidence(text: str) -> dict:
    """
    Return the exact catalog/category phrases that contributed to each category
    decision.

    Example:
        {
            "Vacuum cleaner": ["robot vacuum", "deebot"],
            "Hood": ["cooker hood"]
        }
    """
    normalized_text = _normalized_catalog_text(text)
    evidence = {}

    for category, terms in CATEGORY_TERMS.items():
        matches = []

        for term in terms:
            normalized_term = _normalized_catalog_text(term)

            if normalized_term and normalized_term in normalized_text:
                matches.append(term)

        if matches:
            # preserve order, remove duplicates
            deduped = []
            seen = set()

            for match in matches:
                key = match.casefold()

                if key in seen:
                    continue

                seen.add(key)
                deduped.append(match)

            evidence[category] = deduped

    if "Microwave" in evidence:
        evidence.pop("Oven", None)

    return evidence


def _filter_confidence(
    detected_score: int,
    all_scores: dict,
) -> str:
    other_scores = [
        score
        for score in all_scores.values()
        if score != detected_score
    ]
    second = max(other_scores) if other_scores else 0
    margin = detected_score - second

    if detected_score >= 5 and margin >= 2:
        return "high"
    if detected_score >= 3:
        return "medium"
    return "low"



def infer_item_archetype(
    item: dict,
    item_category: Optional[str],
) -> str:
    """
    A lightweight visual/product archetype used only for grouping equivalent
    catalog products. It does NOT replace category detection.

    This prevents near-identical products from landing in different comparison
    groups because one source crop crosses a geometric threshold by a few pixels.
    """
    text = " ".join(
        str(x).lower()
        for x in (
            item.get("name", ""),
            item.get("product_url", ""),
        )
        if x
    )

    if item_category == "Vacuum cleaner":
        if any(
            token in text
            for token in (
                "deebot",
                "robot vacuum",
                "robotic vacuum",
                "robot cleaner",
            )
        ):
            if any(
                token in text
                for token in (
                    "omni",
                    "station",
                    "self-empty",
                    "self empty",
                    "wash/dry",
                    "washing",
                    "hot air drying",
                    "mop",
                )
            ):
                return "robot_vacuum_station"
            return "robot_vacuum"

        if any(
            token in text
            for token in (
                "cordless",
                "wet and dry",
                "dyad",
                "stick vacuum",
            )
        ):
            return "cordless_floorcare"

    if item_category == "Hood":
        if "chimney" in text or "wall mounted" in text:
            return "chimney_hood"
        return "hood"

    return "default"


def infer_item_category(item: dict) -> Optional[str]:
    """
    Infer a technical normalization category from the individual product, not
    only from the listing page.

    This is essential on mixed Home Appliances pages where a hood, juicer,
    waffle maker and robot vacuum should not share one page-level geometry rule.
    """
    text = " | ".join(
        str(x)
        for x in (
            item.get("name", ""),
            item.get("product_url", ""),
        )
        if x
    )
    scores = _category_scores(text)

    if not scores:
        return None

    ranked = sorted(
        scores.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top_category, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    # Require either a useful phrase score or a clear win over another category.
    if top_score >= 2 and top_score >= second_score + 1:
        return top_category

    return None


def _page_context(soup: BeautifulSoup, final_url: str) -> str:
    """
    Build listing-level context only.

    Many Shopify themes render every product-card title as <h2>. Older builds
    included the first few H2 tags in page context, which could make a broad
    Small Appliances page look like a Kettle page merely because a kettle
    appeared near the top of the product grid.
    """
    pieces = [final_url]

    if soup.title:
        pieces.append(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )

    # Primary page heading only. Never product-card H2/H3 headings.
    for node in soup.find_all("h1", limit=3):
        pieces.append(
            node.get_text(
                " ",
                strip=True,
            )
        )

    # Breadcrumbs and collection-level titles remain strong signals.
    for node in soup.find_all(
        attrs={
            "class": re.compile(
                r"breadcrumb|collection[-_ ]?title|category[-_ ]?title|page[-_ ]?title",
                re.I,
            )
        },
        limit=8,
    ):
        semantic = _dom_semantic_text(node)

        if re.search(
            r"product[-_ ]?(card|item|tile)|grid[-_ ]?item|catalog[-_ ]?item",
            semantic,
            re.I,
        ):
            continue

        pieces.append(
            node.get_text(
                " ",
                strip=True,
            )
        )

    cleaned = []
    seen = set()

    for piece in pieces:
        text = _clean_text(piece)

        if not text:
            continue

        key = text.casefold()

        if key in seen:
            continue

        seen.add(key)
        cleaned.append(text)

    return " | ".join(cleaned)


def infer_page_category(context: str) -> Optional[str]:
    """
    Infer an audit category gate, not the website's real collection category.

    Returning None is correct for broad mixed collections such as Home
    Appliances or Small Appliances. In that case products are classified
    individually and no single appliance-family gate is applied.
    """
    scores = _category_scores(context)

    if not scores:
        return None

    ordered = sorted(
        scores.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )

    best_category, best_score = ordered[0]
    second_score = (
        ordered[1][1]
        if len(ordered) >= 2
        else 0
    )

    # Ambiguous listing-level evidence -> don't apply a single-category gate.
    if (
        second_score
        and best_score < second_score + 2
    ):
        return None

    return best_category


def category_gate_mode(
    requested_category: Optional[str],
    effective_category: Optional[str],
) -> str:
    if requested_category:
        return "manual"

    if effective_category:
        return "auto_listing_context"

    return "disabled_mixed_or_unknown"



def candidate_override_key(item: dict) -> str:
    """
    Stable identifier for a scouted product used by manual filter overrides.

    Prefer the direct product URL, then the image URL, then the name.
    """
    product_url = (
        item.get("product_url")
        or ""
    ).split("#")[0].rstrip("/").strip().casefold()

    if product_url:
        return f"product::{product_url}"

    image_url = (
        item.get("image_url")
        or ""
    ).split("#")[0].strip().casefold()

    if image_url:
        return f"image::{image_url}"

    return (
        "name::"
        + _clean_text(
            item.get("name", "Unnamed product")
        ).casefold()
    )


def filter_product_candidates(
    items: List[dict],
    requested_category: Optional[str],
    page_context: str,
    forced_include_keys: Optional[set[str]] = None,
    item_category_overrides: Optional[dict[str, str]] = None,
) -> tuple[List[dict], List[dict], Optional[str]]:
    """
    Keep the requested/inferred page category and remove only products with
    evidence that they belong to another known category.

    V6.6.2 records the exact category evidence used for every exclusion so the
    UI can show *what* was filtered and *why*.
    """
    effective_category = requested_category or infer_page_category(page_context)

    forced_include_keys = set(
        forced_include_keys
        or set()
    )
    item_category_overrides = dict(
        item_category_overrides
        or {}
    )

    if not effective_category:
        return items, [], None

    kept = []
    excluded = []

    for item in items:
        override_key = candidate_override_key(item)

        if override_key in forced_include_keys:
            manual_category = item_category_overrides.get(
                override_key
            )

            kept.append(
                {
                    **item,
                    "manual_filter_override": True,
                    "manual_category_override": manual_category,
                    "manual_override_key": override_key,
                }
            )
            continue
        searchable = " ".join(
            [
                item.get("name", ""),
                item.get("product_url") or "",
                item.get("image_url") or "",
            ]
        )

        scores = _category_scores(searchable)
        evidence = _category_evidence(searchable)

        if effective_category in scores:
            kept.append(item)
            continue

        conflicting = [
            (cat, score)
            for cat, score in scores.items()
            if cat != effective_category
        ]

        if conflicting:
            conflicting.sort(
                key=lambda kv: kv[1],
                reverse=True,
            )
            detected_category, detected_score = conflicting[0]

            detected_terms = evidence.get(
                detected_category,
                [],
            )
            expected_terms = evidence.get(
                effective_category,
                [],
            )

            reason_detail = (
                f"Detected as {detected_category} while the audit category gate "
                f"is {effective_category}."
            )

            if detected_terms:
                reason_detail += (
                    " Evidence: "
                    + ", ".join(
                        f"'{term}'"
                        for term in detected_terms
                    )
                    + "."
                )

            if not expected_terms:
                reason_detail += (
                    f" No {effective_category} category phrase was found "
                    "in the product name/URLs. This does not mean the website "
                    "placed the product in the wrong collection; it only "
                    "explains why the audit gate excluded it."
                )

            excluded.append(
                {
                    **item,
                    "filter_stage": "category_gate",
                    "filter_reason": reason_detail,
                    "expected_category": effective_category,
                    "detected_category": detected_category,
                    "detected_category_score": int(detected_score),
                    "filter_confidence": _filter_confidence(
                        int(detected_score),
                        scores,
                    ),
                    "detected_category_evidence": detected_terms,
                    "expected_category_evidence": expected_terms,
                    "category_scores": {
                        category: int(score)
                        for category, score in sorted(
                            scores.items(),
                            key=lambda kv: kv[1],
                            reverse=True,
                        )
                    },
                }
            )
            continue

        # No category evidence either way -> keep cautiously.
        kept.append(item)

    return kept, excluded, effective_category




# ------------------------------------------------------------------
# V6.4 PAGINATION DISCOVERY
#
# Given one category/subcategory URL, discover its server-rendered pagination
# links and return the complete set of pages in that same listing.
#
# It deliberately follows actual pagination UI / rel=next links instead of
# blindly guessing ?page=2, ?page=3, ... so it is less likely to crawl
# unrelated pages.
# ------------------------------------------------------------------
PAGINATION_CONTAINER_RE = re.compile(
    r"pagination|pager|page-numbers|page_numbers|nav-links|nav_links|pages",
    re.I,
)

PAGE_QUERY_KEYS = {
    "page",
    "paged",
    "pageno",
    "page_no",
    "page-number",
    "page_number",
    "pg",
}

NEXT_LABELS = {
    "next",
    "next page",
    "older",
    "›",
    "»",
    ">",
}


def _canonical_page_url(url: str) -> str:
    parsed = urlparse(url)
    query = urlencode(
        sorted(parse_qsl(parsed.query, keep_blank_values=True)),
        doseq=True,
    )
    path = re.sub(r"/+", "/", parsed.path or "/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path.rstrip("/") or "/",
            "",
            query,
            "",
        )
    )


def _same_origin(a: str, b: str) -> bool:
    pa = urlparse(a)
    pb = urlparse(b)
    return (
        pa.scheme.lower(),
        pa.netloc.lower(),
    ) == (
        pb.scheme.lower(),
        pb.netloc.lower(),
    )


def _looks_paginated_url(url: str) -> bool:
    parsed = urlparse(url)

    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in PAGE_QUERY_KEYS:
            return True
        if key.lower() in {"product-page", "product_page"}:
            return True

    path = parsed.path.lower()

    return bool(
        re.search(r"/page/\d+/?$", path)
        or re.search(r"/page-\d+/?$", path)
        or re.search(r"/p/\d+/?$", path)
    )


def _page_number_from_url(url: str) -> Optional[int]:
    parsed = urlparse(url)

    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in PAGE_QUERY_KEYS | {"product-page", "product_page"}:
            match = re.search(r"\d+", value)
            if match:
                return int(match.group())

    for pattern in (
        r"/page/(\d+)(?:/|$)",
        r"/page-(\d+)(?:/|$)",
        r"/p/(\d+)(?:/|$)",
    ):
        match = re.search(pattern, parsed.path, re.I)
        if match:
            return int(match.group(1))

    return None


def _pagination_links_from_soup(
    soup: BeautifulSoup,
    current_url: str,
) -> List[str]:
    links = []

    def add_href(href: Optional[str]):
        if not href:
            return
        absolute = urljoin(current_url, href)
        if not _same_origin(current_url, absolute):
            return
        canonical = _canonical_page_url(absolute)
        if canonical != _canonical_page_url(current_url):
            links.append(canonical)

    # rel=next is the strongest signal.
    for anchor in soup.find_all("a", href=True):
        rel = anchor.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        if any(str(x).lower() == "next" for x in rel):
            add_href(anchor.get("href"))

    # Dedicated pagination containers.
    containers = []

    for node in soup.find_all(["nav", "div", "ul", "ol"]):
        classes = " ".join(node.get("class", []))
        node_id = node.get("id", "")
        aria = node.get("aria-label", "")
        role = node.get("role", "")

        searchable = " ".join([classes, node_id, aria, role])

        if PAGINATION_CONTAINER_RE.search(searchable):
            containers.append(node)

    for container in containers:
        for anchor in container.find_all("a", href=True):
            text = _clean_text(anchor.get_text(" ", strip=True)).lower()

            if (
                text.isdigit()
                or text in NEXT_LABELS
                or _looks_paginated_url(
                    urljoin(current_url, anchor.get("href"))
                )
            ):
                add_href(anchor.get("href"))

    # Some themes don't wrap pagination in a semantic container.
    # Accept explicit page-shaped URLs whose anchor labels are numeric/next.
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(current_url, anchor.get("href"))
        text = _clean_text(anchor.get_text(" ", strip=True)).lower()

        if _looks_paginated_url(absolute) and (
            text.isdigit() or text in NEXT_LABELS
        ):
            add_href(anchor.get("href"))

    # Dedupe in discovery order.
    seen = set()
    result = []

    for link in links:
        if link not in seen:
            seen.add(link)
            result.append(link)

    return result


def pagination_links_from_html(
    html_text: str,
    current_url: str,
) -> List[str]:
    """
    Public helper mainly for tests and advanced tooling.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    return _pagination_links_from_soup(soup, current_url)


def discover_pagination_urls(
    start_url: str,
    max_pages: int = 25,
    timeout: int = 20,
) -> List[dict]:
    """
    Crawl the pagination graph beginning from one category-page URL.

    Returns:
      [
        {"url": "...", "page_number": 1, "page_label": "Page 1"},
        ...
      ]

    If the site exposes only one server-rendered page, the list contains only
    the supplied page.
    """
    validate_public_url(start_url)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.8",
    }

    start = _canonical_page_url(start_url)

    queue = [start]
    visited = set()
    discovered = []

    while queue and len(discovered) < max_pages:
        current = queue.pop(0)

        if current in visited:
            continue

        visited.add(current)

        response = safe_get(
            current,
            headers=headers,
            timeout=(6.0, float(timeout)),
            max_redirects=5,
            max_bytes=8 * 1024 * 1024,
        )
        response.raise_for_status()

        final_url = _canonical_page_url(response.url)

        if final_url in {
            item["url"] for item in discovered
        }:
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        discovered.append(
            {
                "url": final_url,
                "page_number": _page_number_from_url(final_url),
            }
        )

        for link in _pagination_links_from_soup(soup, final_url):
            if (
                link not in visited
                and link not in queue
                and len(visited) + len(queue) < max_pages * 4
            ):
                queue.append(link)

    # Stable page numbering:
    # - use actual numbers where present
    # - ensure the start page is Page 1 if it has no explicit number
    # - give remaining unknown pages discovery-order numbers
    used_numbers = {
        item["page_number"]
        for item in discovered
        if item["page_number"] is not None
    }

    next_unknown = 1

    for idx, item in enumerate(discovered):
        if item["page_number"] is None:
            if idx == 0 and 1 not in used_numbers:
                item["page_number"] = 1
                used_numbers.add(1)
                continue

            while next_unknown in used_numbers:
                next_unknown += 1

            item["page_number"] = next_unknown
            used_numbers.add(next_unknown)

    discovered.sort(
        key=lambda item: (
            item["page_number"],
            item["url"],
        )
    )

    for item in discovered:
        item["page_label"] = f"Page {item['page_number']}"

    return discovered[:max_pages]

def _dom_products(
    soup: BeautifulSoup,
    base_url: str,
    strict_page_membership: bool = True,
) -> List[dict]:
    items = []

    for img in soup.find_all("img"):
        image_url = _image_url_from_tag(
            img,
            base_url,
        )

        if not image_url:
            continue

        width = img.get("width")
        height = img.get("height")

        try:
            if width and int(float(width)) < 90:
                continue
            if height and int(float(height)) < 90:
                continue
        except Exception:
            pass

        # Hard exclusions remain strict: footer/header/social/app badges and
        # explicit related/recommended/recently-viewed/upsell sections.
        if _has_non_listing_product_ancestor(img):
            continue

        card = _find_product_card(img)
        name = _product_name_from_card(img, card)

        # Resolve a product-detail URL from a recognized card first.
        product_url = _product_url_from_card(
            img,
            card,
            base_url,
        )

        # Recovery lane:
        # If the site uses unusual markup and no card was recognized, a strong
        # same-site image-link + real product name is still valid page evidence.
        if card is None:
            direct_anchor = img.find_parent("a", href=True)
            direct_href = (
                direct_anchor.get("href")
                if direct_anchor
                else None
            )

            if _is_plausible_product_detail_url(
                base_url,
                direct_href,
            ):
                direct_url = urljoin(
                    base_url,
                    direct_href,
                )

                # Do not let a link back to the listing page count as product
                # membership.
                if (
                    _canonical_page_url(direct_url)
                    != _canonical_page_url(base_url)
                ):
                    product_url = direct_url

        reject, _reason = _looks_like_non_product_image(
            img=img,
            base_url=base_url,
            image_url=image_url,
            name=name,
            product_url=product_url,
        )

        if reject:
            continue

        membership_method = None
        membership_confidence = None

        if card is not None:
            membership_method = "catalog_card"
            membership_confidence = (
                "high"
                if product_url
                else "medium"
            )
        elif (
            product_url
            and name != "Unnamed product"
        ):
            membership_method = "strong_product_link_recovery"
            membership_confidence = "medium"
        else:
            # In strict mode we still refuse arbitrary decorative images.
            if strict_page_membership:
                continue

            membership_method = "legacy_dom"
            membership_confidence = "low"

        items.append(
            {
                "name": name,
                "image_url": image_url,
                "product_url": product_url,
                "source": "dom",
                "page_membership_verified": True,
                "page_membership_confidence": membership_confidence,
                "page_membership_method": membership_method,
            }
        )

    return items


def scrape_product_candidates(
    url: str,
    max_items: int = 50,
    timeout: int = 20,
    strict_page_membership: bool = True,
) -> dict:
    validate_public_url(url)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.8",
    }

    response = safe_get(
        url,
        headers=headers,
        timeout=(6.0, float(timeout)),
        max_redirects=5,
        max_bytes=8 * 1024 * 1024,
    )
    response.raise_for_status()

    content_type = response.headers.get(
        "content-type",
        "",
    )

    if (
        "html"
        not in content_type.lower()
        and not response.text.lstrip().startswith(
            "<"
        )
    ):
        raise ValueError(
            "The URL did not return an HTML page."
        )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    dom_candidates = _dom_products(
        soup,
        response.url,
        strict_page_membership=(
            strict_page_membership
        ),
    )
    jsonld_candidates = _jsonld_products(
        soup,
        response.url,
    )

    if strict_page_membership:
        # Critical V6.5.3 rule:
        # JSON-LD does NOT create page candidates. It can only enrich products
        # that were independently verified inside the page's listing DOM.
        candidates = _enrich_dom_with_jsonld(
            dom_candidates,
            jsonld_candidates,
        )
    else:
        candidates = list(
            jsonld_candidates
        )
        candidates.extend(
            dom_candidates
        )

    # Dedupe by product URL first, then image URL.
    # In strict mode all retained rows are backed by current-page DOM evidence.
    unique = {}

    for item in candidates:
        product_key = (
            item.get("product_url")
            or ""
        ).split("#")[0].rstrip("/").casefold()

        image_key = (
            item.get("image_url")
            or ""
        ).split("#")[0].casefold()

        key = (
            f"product::{product_key}"
            if product_key
            else f"image::{image_key}"
        )

        if not key:
            continue

        existing = unique.get(
            key
        )

        if existing is None:
            unique[key] = item
            continue

        # Prefer the item with an actual product URL and better name.
        existing_score = (
            int(
                bool(
                    existing.get(
                        "product_url"
                    )
                )
            )
            + int(
                existing.get(
                    "name"
                )
                != "Unnamed product"
            )
        )
        item_score = (
            int(
                bool(
                    item.get(
                        "product_url"
                    )
                )
            )
            + int(
                item.get(
                    "name"
                )
                != "Unnamed product"
            )
        )

        if item_score > existing_score:
            unique[
                key
            ] = item

    items = list(
        unique.values()
    )[:max_items]

    return {
        "final_url": response.url,
        "items": items,
        "html_length": len(
            response.text
        ),
        "page_context": _page_context(
            soup,
            response.url,
        ),
        "strict_page_membership": (
            strict_page_membership
        ),
        "dom_verified_count": len(
            items
        )
        if strict_page_membership
        else len(
            dom_candidates
        ),
        "jsonld_discovered_count": len(
            jsonld_candidates
        ),
    }


def _download_image(url: str, referer: Optional[str] = None, timeout: int = 20) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    r = safe_get(
        url,
        headers=headers,
        timeout=(6.0, float(timeout)),
        max_redirects=5,
        max_bytes=25 * 1024 * 1024,
    )
    r.raise_for_status()

    data = r.content
    if len(data) < 1500:
        raise ValueError("Image response is too small.")

    # Verify Pillow can open it and reject tiny icons.
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    if image.width < 120 or image.height < 120:
        raise ValueError(f"Image is too small ({image.width}×{image.height}).")
    return data



def _download_images_bounded(
    items: list[dict],
    *,
    referer: str | None,
    max_workers: int = 6,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Download each unique image URL once with conservative bounded concurrency."""
    urls = []
    seen = set()
    for item in items:
        url = str(item.get("image_url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    results: dict[str, bytes] = {}
    errors: dict[str, str] = {}
    if not urls:
        return results, errors
    workers = max(1, min(int(max_workers), 6, len(urls)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fbf-image") as pool:
        futures = {
            pool.submit(_download_image, url, referer): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                # Keep the user-facing failure concise; exception chaining/logging remains
                # available to developers when this helper is tested or wrapped.
                errors[url] = str(exc) or exc.__class__.__name__
    return results, errors


def _canonical_profile_standard(
    profile_name: str,
    category: Optional[str],
    subtype: str,
    normalization_mode: str = "standard",
) -> dict:
    return _profile_for(
        profile_name=profile_name,
        category=category,
        subtype=subtype,
        profile_overrides=None,
        normalization_mode=normalization_mode,
    )


def _canonical_framing_evidence(
    item: dict,
    category: Optional[str],
    scale_tolerance: float,
    alignment_tolerance: float,
    normalization_mode: str = "standard",
) -> dict:
    """
    Conservative absolute framing check.

    Result is intentionally broad so it catches clear errors rather than
    micromanaging every source image.
    """
    m = item["metrics"]
    standard = _canonical_profile_standard(
        item["profile"],
        category,
        item["subtype"],
        normalization_mode=normalization_mode,
    )

    scale_basis = resolve_scale_basis(item["subtype"])

    if "target_height" in standard:
        axis = (
            "full_height_occupancy"
            if scale_basis == "full"
            else "height_occupancy"
        )
        target = float(standard["target_height"])
    else:
        axis = (
            "full_width_occupancy"
            if scale_basis == "full"
            else "width_occupancy"
        )
        target = float(standard["target_width"])

    value = float(m[axis])
    ratio = value / max(target, 1e-6)

    # Broader than the page-relative tolerance. Accessory-heavy and multipart
    # sources get additional slack because the primary body may legitimately
    # occupy less of the raw canvas.
    if str(normalization_mode).casefold().startswith("amazon"):
        tolerance = max(
            0.055,
            min(0.10, float(scale_tolerance) * 0.55),
        )
        if item["subtype"] in {
            "accessory_heavy",
            "multipart",
            "bundle_layout",
        }:
            tolerance += 0.035
    else:
        tolerance = max(
            0.22,
            float(scale_tolerance) + 0.05,
        )

        if item["subtype"] in {
            "accessory_heavy",
            "multipart",
            "bundle_layout",
        }:
            tolerance += 0.07

    scale_issue = None

    if ratio < 1.0 - tolerance:
        scale_issue = (
            f"Primary body is clearly too small for the expected "
            f"{item['profile']} framing standard "
            f"({abs(1.0-ratio):.0%} below expected size)"
        )
    elif ratio > 1.0 + tolerance:
        scale_issue = (
            f"Primary body is clearly too large for the expected "
            f"{item['profile']} framing standard "
            f"({abs(ratio-1.0):.0%} above expected size)"
        )

    alignment_issue = None
    alignment_delta = 0.0

    if standard.get("alignment") == "bottom_center":
        expected = float(
            standard.get(
                "baseline",
                PROFILES[item["profile"]].get(
                    "baseline",
                    0.89,
                ),
            )
        )
        actual = float(m["baseline_y"])
        alignment_delta = abs(
            actual - expected
        )

        if alignment_delta > max(
            0.085,
            float(alignment_tolerance) * 1.55,
        ):
            alignment_issue = (
                "Primary-body baseline is clearly outside the expected "
                "framing range"
            )
    else:
        expected = 0.5
        actual = float(m["center_y"])
        alignment_delta = abs(
            actual - expected
        )

        if alignment_delta > max(
            0.085,
            float(alignment_tolerance) * 1.55,
        ):
            alignment_issue = (
                "Primary-body vertical centering is clearly outside the "
                "expected framing range"
            )

    return {
        "axis": axis,
        "target": target,
        "value": value,
        "scale_ratio": ratio,
        "scale_tolerance": tolerance,
        "scale_issue": scale_issue,
        "alignment_issue": alignment_issue,
        "alignment_delta": alignment_delta,
    }



def _square_card_source_metrics(item: dict) -> dict:
    """
    Convert raw-source measurements into the way the product actually appears
    when the complete source image is object-fit/contained inside a square
    catalog image box.

    This matters because the audit is judging what a customer sees on the
    website, not just the percentage of the original source bitmap.
    """
    m = item["metrics"]

    W = max(1.0, float(m.get("image_width", 1)))
    H = max(1.0, float(m.get("image_height", 1)))
    longest = max(W, H)

    image_scale_x = W / longest
    image_scale_y = H / longest

    image_offset_x = (1.0 - image_scale_x) / 2.0
    image_offset_y = (1.0 - image_scale_y) / 2.0

    body_w = float(m["body_width_occupancy"]) * image_scale_x
    body_h = float(m["body_height_occupancy"]) * image_scale_y

    body_center_x = (
        image_offset_x
        + float(m["center_x"]) * image_scale_x
    )
    body_center_y = (
        image_offset_y
        + float(m["center_y"]) * image_scale_y
    )
    body_baseline_y = (
        image_offset_y
        + float(m["body_baseline_y"]) * image_scale_y
    )

    full_w = float(m["full_width_occupancy"]) * image_scale_x
    full_h = float(m["full_height_occupancy"]) * image_scale_y

    full_left = (
        image_offset_x
        + float(m["left_padding"]) * image_scale_x
    )
    full_top = (
        image_offset_y
        + float(m["top_padding"]) * image_scale_y
    )

    full_center_x = full_left + full_w / 2.0
    full_center_y = full_top + full_h / 2.0

    full_centroid_x = (
        image_offset_x
        + float(m.get("full_centroid_x", m["center_x"])) * image_scale_x
    )
    full_centroid_y = (
        image_offset_y
        + float(m.get("full_centroid_y", m["center_y"])) * image_scale_y
    )

    return {
        "body_width": float(body_w),
        "body_height": float(body_h),
        "body_center_x": float(body_center_x),
        "body_center_y": float(body_center_y),
        "body_baseline_y": float(body_baseline_y),
        "full_width": float(full_w),
        "full_height": float(full_h),
        "full_center_x": float(full_center_x),
        "full_center_y": float(full_center_y),
        "full_centroid_x": float(full_centroid_x),
        "full_centroid_y": float(full_centroid_y),
        "image_scale_x": float(image_scale_x),
        "image_scale_y": float(image_scale_y),
    }


def _normalizer_agreement_evidence(
    item: dict,
    category: Optional[str],
    normalization_mode: str = "standard",
) -> dict:
    """
    Predict the correction the real normalizer would apply.

    The audit previously had an important blind spot:
      - peer groups could say an image was fine
      - conservative canonical thresholds could also say it was fine
      - but "Normalize every image" would visibly enlarge/reposition it

    That is exactly the Bunn VP17A-2 failure case.

    V6.5.6 adds a third signal:
        If the normalizer itself would make a meaningful visual correction,
        the audit must not call the source "perfect".
    """
    square = _square_card_source_metrics(item)

    profile = _profile_for(
        profile_name=item["profile"],
        category=category,
        subtype=item["subtype"],
        profile_overrides=None,
        normalization_mode=normalization_mode,
    )

    scale_basis = resolve_scale_basis(item["subtype"])
    body_w = max(
        square["full_width"] if scale_basis == "full" else square["body_width"],
        1e-6,
    )
    body_h = max(
        square["full_height"] if scale_basis == "full" else square["body_height"],
        1e-6,
    )

    if "target_height" in profile:
        target_h = float(profile["target_height"])
        max_w = float(profile.get("max_width", 0.98))

        scale_from_height = target_h / body_h
        scale_from_width = max_w / body_w
        recommended_scale = min(
            scale_from_height,
            scale_from_width,
        )
        controlling_axis = (
            "height"
            if scale_from_height <= scale_from_width
            else "width_limit"
        )
    else:
        target_w = float(profile["target_width"])
        max_h = float(profile.get("max_height", 0.98))

        scale_from_width = target_w / body_w
        scale_from_height = max_h / body_h
        recommended_scale = min(
            scale_from_width,
            scale_from_height,
        )
        controlling_axis = (
            "width"
            if scale_from_width <= scale_from_height
            else "height_limit"
        )

    # Anchor logic mirrors the normalizer policy.
    anchor_mode = resolve_anchor_mode(
        category=category,
        subtype=item["subtype"],
        accessory_area_ratio=float(
            item["metrics"].get(
                "accessory_area_ratio",
                0.0,
            )
        ),
        requested_anchor_mode="auto",
    )

    if anchor_mode == "full_center":
        current_anchor_x = square["full_center_x"]
        current_anchor_y = square["full_center_y"]
        target_anchor_x = 0.5
        target_anchor_y = 0.5
        vertical_anchor_kind = "center"
    elif anchor_mode == "visual_center":
        current_anchor_x = square["full_centroid_x"]
        current_anchor_y = square["full_centroid_y"]
        target_anchor_x = 0.5
        target_anchor_y = 0.5
        vertical_anchor_kind = "visual mass center"
    else:
        current_anchor_x = square["body_center_x"]
        target_anchor_x = 0.5

        if profile.get("alignment") == "bottom_center":
            current_anchor_y = square["body_baseline_y"]
            target_anchor_y = float(
                profile.get("baseline", 0.89)
            )
            vertical_anchor_kind = "baseline"
        else:
            current_anchor_y = square["body_center_y"]
            target_anchor_y = 0.5
            vertical_anchor_kind = "center"

    x_shift = target_anchor_x - current_anchor_x
    y_shift = target_anchor_y - current_anchor_y

    # 5.5% is intentionally more sensitive than the old 22% canonical guard.
    # It is still large enough to ignore tiny rounding / cosmetic differences.
    if str(normalization_mode).casefold().startswith("amazon"):
        meaningful_scale_delta = 0.025
        meaningful_position_delta = 0.015
    else:
        meaningful_scale_delta = 0.055
        meaningful_position_delta = 0.028

    scale_issue = None
    if recommended_scale > 1.0 + meaningful_scale_delta:
        scale_issue = (
            "Normalizer would visibly enlarge this product "
            f"by about {(recommended_scale - 1.0):.0%}; "
            "the source framing is too small on the catalog card"
        )
    elif recommended_scale < 1.0 - meaningful_scale_delta:
        scale_issue = (
            "Normalizer would visibly reduce this product "
            f"by about {(1.0 - recommended_scale):.0%}; "
            "the source framing is too large on the catalog card"
        )

    horizontal_issue = None
    if abs(x_shift) > meaningful_position_delta:
        direction = "right" if x_shift > 0 else "left"
        horizontal_issue = (
            "Normalizer would move the product "
            f"{direction} by about {abs(x_shift):.1%} of the card width"
        )

    vertical_issue = None
    if abs(y_shift) > meaningful_position_delta:
        direction = "down" if y_shift > 0 else "up"
        vertical_issue = (
            "Normalizer would move the product "
            f"{direction} by about {abs(y_shift):.1%} of the card height "
            f"to correct its {vertical_anchor_kind}"
        )

    return {
        "recommended_scale": float(recommended_scale),
        "scale_delta": float(recommended_scale - 1.0),
        "scale_issue": scale_issue,
        "horizontal_issue": horizontal_issue,
        "vertical_issue": vertical_issue,
        "x_shift": float(x_shift),
        "y_shift": float(y_shift),
        "anchor_mode": anchor_mode,
        "vertical_anchor_kind": vertical_anchor_kind,
        "controlling_axis": controlling_axis,
        "square_card": square,
    }



def _canonical_url_for_match(
    value: Optional[str],
) -> str:
    value = (
        value
        or ""
    ).strip()

    if not value:
        return ""

    parsed = urlparse(value)

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        )
    )


def _single_product_candidate(
    soup: BeautifulSoup,
    final_url: str,
) -> dict:
    """
    Resolve the primary product from an individual product-detail page.

    JSON-LD is preferred on product pages because it is usually the cleanest
    source of the canonical product name + hero image. Unlike collection mode,
    JSON-LD is allowed to create the candidate here because the URL itself is
    explicitly a single-product request.
    """
    jsonld = _jsonld_products(
        soup,
        final_url,
    )

    requested = _canonical_url_for_match(
        final_url
    )

    if jsonld:
        exact = [
            item
            for item in jsonld
            if _canonical_url_for_match(
                item.get(
                    "product_url"
                )
            )
            == requested
        ]

        chosen = (
            exact[0]
            if exact
            else jsonld[0]
        )

        return {
            **chosen,
            "product_url": (
                chosen.get(
                    "product_url"
                )
                or final_url
            ),
            "source": "single_product_jsonld",
            "page_membership_verified": True,
            "page_membership_confidence": "high",
            "page_membership_method": "explicit_product_page",
        }

    # Fallback: use primary page H1 + OpenGraph hero image.
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = _clean_text(
            h1.get_text(
                " ",
                strip=True,
            )
        )

    og_image = soup.find(
        "meta",
        attrs={
            "property": "og:image"
        },
    )
    if og_image and og_image.get(
        "content"
    ):
        return {
            "name": (
                name
                or "Unnamed product"
            ),
            "image_url": urljoin(
                final_url,
                og_image[
                    "content"
                ],
            ),
            "product_url": final_url,
            "source": "single_product_og",
            "page_membership_verified": True,
            "page_membership_confidence": "medium",
            "page_membership_method": "explicit_product_page",
        }

    # Last fallback: use a product-ish DOM image/card.
    dom = _dom_products(
        soup,
        final_url,
        strict_page_membership=False,
    )

    if dom:
        chosen = dom[0]
        return {
            **chosen,
            "name": (
                name
                or chosen.get(
                    "name"
                )
                or "Unnamed product"
            ),
            "product_url": (
                chosen.get(
                    "product_url"
                )
                or final_url
            ),
            "source": "single_product_dom",
            "page_membership_verified": True,
            "page_membership_confidence": "medium",
            "page_membership_method": "explicit_product_page",
        }

    raise ValueError(
        "Could not find a product hero image on this product page."
    )


def _score_single_product_item(
    item: dict,
    category: Optional[str],
    scale_tolerance: float,
    alignment_tolerance: float,
    normalization_mode: str = "standard",
) -> dict:
    """
    Single products have no peer median, so use:
      - category/profile canonical framing
      - normalizer-agreement evidence
      - centering / edge safety
      - segmentation confidence
    """
    m = item["metrics"]

    canonical = _canonical_framing_evidence(
        item,
        category,
        scale_tolerance,
        alignment_tolerance,
        normalization_mode=normalization_mode,
    )
    agreement = _normalizer_agreement_evidence(
        item,
        category,
        normalization_mode=normalization_mode,
    )

    issues = []

    if m.get(
        "segmentation_suspect"
    ):
        issues.append(
            "Foreground detection looks unreliable"
        )

    if (
        canonical.get(
            "scale_issue"
        )
        and not m.get(
            "segmentation_suspect"
        )
    ):
        issues.append(
            canonical[
                "scale_issue"
            ]
        )

    if (
        canonical.get(
            "alignment_issue"
        )
        and not m.get(
            "segmentation_suspect"
        )
    ):
        issues.append(
            canonical[
                "alignment_issue"
            ]
        )

    if not m.get(
        "segmentation_suspect"
    ):
        for key in (
            "scale_issue",
            "horizontal_issue",
            "vertical_issue",
        ):
            issue = agreement.get(
                key
            )
            if (
                issue
                and issue not in issues
            ):
                issues.append(
                    issue
                )

    if m.get(
        "center_offset_x",
        0.0,
    ) > alignment_tolerance:
        direction = (
            "right"
            if m.get(
                "center_x",
                0.5,
            ) > 0.5
            else "left"
        )
        issues.append(
            f"Primary product body is shifted too far {direction}"
        )

    if m.get(
        "min_edge_padding",
        1.0,
    ) < 0.012:
        issues.append(
            "Full product/accessory extent is touching or nearly touching the image edge"
        )

    score = 100.0
    score -= min(
        28.0,
        abs(
            canonical[
                "scale_ratio"
            ]
            - 1.0
        )
        * 70.0,
    )
    score -= min(
        22.0,
        abs(
            agreement[
                "scale_delta"
            ]
        )
        * 120.0,
    )
    score -= min(
        18.0,
        abs(
            agreement[
                "x_shift"
            ]
        )
        * 180.0,
    )
    score -= min(
        18.0,
        abs(
            agreement[
                "y_shift"
            ]
        )
        * 180.0,
    )

    if m.get(
        "segmentation_suspect"
    ):
        score -= 25.0

    score = max(
        0.0,
        min(
            100.0,
            score,
        ),
    )

    item[
        "comparison_group"
    ] = "single_product::canonical"
    item["scale_ratio"] = 1.0
    item[
        "canonical_scale_ratio"
    ] = float(
        canonical[
            "scale_ratio"
        ]
    )
    item[
        "canonical_target"
    ] = float(
        canonical[
            "target"
        ]
    )
    item[
        "canonical_axis"
    ] = canonical[
        "axis"
    ]
    item[
        "normalizer_recommended_scale"
    ] = float(
        agreement[
            "recommended_scale"
        ]
    )
    item[
        "normalizer_scale_delta"
    ] = float(
        agreement[
            "scale_delta"
        ]
    )
    item[
        "normalizer_x_shift"
    ] = float(
        agreement[
            "x_shift"
        ]
    )
    item[
        "normalizer_y_shift"
    ] = float(
        agreement[
            "y_shift"
        ]
    )
    item[
        "normalizer_anchor_mode"
    ] = agreement[
        "anchor_mode"
    ]
    item["issues"] = issues
    item["status"] = (
        "review"
        if issues
        else "pass"
    )
    item[
        "consistency_score"
    ] = float(
        score
    )

    return item


def scan_single_product(
    url: str,
    category: Optional[str] = None,
    segmentation_mode: str = "auto",
    scale_tolerance: float = 0.18,
    alignment_tolerance: float = 0.055,
    normalization_mode: str = "standard",
) -> dict:
    """
    Analyze one explicit product-detail URL.

    This is independent of collection-page comparison. It is designed for
    quick spot-checking and for feeding one product directly into Page Showcase
    + the per-product exception tuner.
    """
    validate_public_url(url)

    response = safe_get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.8",
        },
        timeout=(6.0, 20.0),
        max_redirects=5,
        max_bytes=8 * 1024 * 1024,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    candidate = _single_product_candidate(
        soup,
        response.url,
    )

    item_category = (
        category
        or infer_item_category(
            candidate
        )
    )

    image_bytes = _download_image(
        candidate[
            "image_url"
        ],
        referer=response.url,
    )

    analysis = analyze_source_bytes(
        image_bytes,
        category=item_category,
        segmentation_mode=segmentation_mode,
        body_mode="auto",
    )

    item = {
        **candidate,
        "item_category": item_category,
        "item_archetype": infer_item_archetype(
            candidate,
            item_category,
        ),
        "image_bytes": image_bytes,
        "profile": analysis[
            "profile"
        ],
        "subtype": analysis[
            "subtype"
        ],
        "group_key": (
            f"single::{item_category or 'Generic'}::"
            f"{analysis['profile']}::{analysis['subtype']}"
        ),
        "profile_group_key": (
            f"single::{item_category or 'Generic'}::"
            f"{analysis['profile']}"
        ),
        "metrics": analysis[
            "metrics"
        ],
        "mask": analysis[
            "mask"
        ],
        "body_mask": analysis[
            "body_mask"
        ],
    }

    item = _score_single_product_item(
        item,
        item_category,
        scale_tolerance,
        alignment_tolerance,
        normalization_mode=normalization_mode,
    )

    flagged = (
        1
        if item[
            "status"
        ] != "pass"
        else 0
    )

    return {
        "scan_mode": "single_product",
        "normalization_mode": normalization_mode,
        "url": response.url,
        "final_url": response.url,
        "items": [
            item
        ],
        "failures": [],
        "baselines": {},
        "raw_candidate_count": 1,
        "candidate_count": 1,
        "filtered_out_count": 0,
        "excluded_items": [],
        "manual_included_count": 0,
        "effective_category": None,
        "category_gate_mode": "disabled_single_product",
        "category_gate_label": "Not used for single-product detection",
        "page_context": (
            item.get(
                "name",
                ""
            )
            + " | "
            + response.url
        ),
        "analyzed_count": 1,
        "flagged_count": flagged,
        "page_score": float(
            item[
                "consistency_score"
            ]
        ),
        "strict_page_membership": False,
        "page_membership_mode": "explicit_product_page",
        "dom_verified_count": 1,
        "jsonld_discovered_count": 1 if candidate.get("source") == "single_product_jsonld" else 0,
        "recovered_membership_count": 0,
    }


def page_result_summary(
    analyzed_count: int,
    flagged_count: int,
    failed_count: int,
) -> dict:
    analyzed_count = max(0, int(analyzed_count))
    flagged_count = max(0, min(int(flagged_count), analyzed_count))
    failed_count = max(0, int(failed_count))
    if analyzed_count == 0:
        return {
            "scan_status": "failed",
            "page_score": None,
            "confidence": "insufficient_data",
        }
    score = 100.0 * (analyzed_count - flagged_count) / analyzed_count
    return {
        "scan_status": "partial_success" if failed_count else "success",
        "page_score": float(score),
        "confidence": "reduced" if failed_count else "full",
    }


def scan_page(
    url: str,
    category: Optional[str] = None,
    segmentation_mode: str = "auto",
    max_items: int = 40,
    scale_tolerance: float = 0.18,
    alignment_tolerance: float = 0.055,
    forced_include_keys: Optional[set[str]] = None,
    item_category_overrides: Optional[dict[str, str]] = None,
    normalization_mode: str = "standard",
) -> dict:
    """
    V6 page scanner.

    Products are compared inside a subgroup made from:
        resolved geometric profile + detected structural subtype

    This prevents a floor-standing coffee tower from being calibrated against a
    small countertop machine, while preserving the same architecture for
    refrigerators, water heaters, ACs, hobs, hoods, ovens, etc.
    """
    # Scout more candidates than the requested final count so category
    # filtering can remove footer/recommendation products without starving
    # the real category grid.
    scraped = scrape_product_candidates(
        url,
        max_items=max(max_items * 3, max_items),
        strict_page_membership=True,
    )

    raw_candidate_count = len(scraped["items"])
    filtered_items, excluded_items, effective_category = filter_product_candidates(
        scraped["items"],
        requested_category=category,
        page_context=scraped.get("page_context", ""),
        forced_include_keys=forced_include_keys,
        item_category_overrides=item_category_overrides,
    )
    filtered_items = filtered_items[:max_items]

    # Page category is only a fallback. Mixed home-appliance pages now resolve
    # technical geometry per product.
    analysis_category = category or effective_category

    valid = []
    failures = []

    downloaded_images, download_errors = _download_images_bounded(
        filtered_items,
        referer=scraped["final_url"],
        max_workers=6,
    )

    for item in filtered_items:
        try:
            item_category = (
                item.get("manual_category_override")
                or category
                or infer_item_category(item)
                or effective_category
            )

            image_url = item["image_url"]
            if image_url not in downloaded_images:
                raise ValueError(
                    download_errors.get(
                        image_url,
                        "Image could not be downloaded.",
                    )
                )
            image_bytes = downloaded_images[image_url]
            analysis = analyze_source_bytes(
                image_bytes,
                category=item_category,
                segmentation_mode=segmentation_mode,
                body_mode="auto",
            )
            m = analysis["metrics"]

            family = item_category or "Generic"
            item_archetype = infer_item_archetype(
                item,
                item_category,
            )

            group_key = (
                f"{family}::{item_archetype}::"
                f"{analysis['profile']}::{analysis['subtype']}"
            )
            profile_group_key = (
                f"{family}::{item_archetype}::{analysis['profile']}"
            )

            valid.append(
                {
                    **item,
                    "item_category": item_category,
                    "item_archetype": item_archetype,
                    "image_bytes": image_bytes,
                    "profile": analysis["profile"],
                    "subtype": analysis["subtype"],
                    "group_key": group_key,
                    "profile_group_key": profile_group_key,
                    "metrics": m,
                    "mask": analysis["mask"],
                    "body_mask": analysis["body_mask"],
                }
            )
        except Exception as exc:
            failures.append({**item, "error": str(exc)})

    # ----------------------------------------------------------
    # Build both subgroup and profile fallback baselines.
    # ----------------------------------------------------------
    subgroup_groups: Dict[str, List[dict]] = {}
    profile_groups: Dict[str, List[dict]] = {}

    for item in valid:
        subgroup_groups.setdefault(item["group_key"], []).append(item)
        profile_groups.setdefault(
            item.get("profile_group_key", item["profile"]),
            [],
        ).append(item)

    baselines = {}

    def make_baseline(key: str, items: List[dict], profile_name: str):
        profile = PROFILES[profile_name]
        axis = (
            "height_occupancy"
            if "target_height" in profile
            else "width_occupancy"
        )
        scale_values = [x["metrics"][axis] for x in items]
        align_axis = (
            "baseline_y"
            if profile.get("alignment") == "bottom_center"
            else "center_y"
        )
        align_values = [x["metrics"][align_axis] for x in items]
        baselines[key] = {
            "axis": axis,
            "scale_median": float(np.median(scale_values)),
            "align_axis": align_axis,
            "align_median": float(np.median(align_values)),
            "count": len(items),
            "profile": profile_name,
        }

    for key, items in subgroup_groups.items():
        make_baseline(key, items, items[0]["profile"])

    for profile_group_key, items in profile_groups.items():
        make_baseline(
            f"profile::{profile_group_key}",
            items,
            items[0]["profile"],
        )

    # ----------------------------------------------------------
    # Score each item against the most specific reliable group.
    # ----------------------------------------------------------
    for item in valid:
        subgroup = baselines[item["group_key"]]
        fallback = baselines[
            f"profile::{item.get('profile_group_key', item['profile'])}"
        ]

        # A subgroup with 2+ examples is usable. Otherwise use the broader
        # geometric family rather than comparing against a single item.
        b = subgroup if subgroup["count"] >= 2 else fallback
        m = item["metrics"]

        scale_value = m[b["axis"]]
        scale_ratio = scale_value / max(b["scale_median"], 1e-6)

        item_analysis_category = item.get("item_category") or analysis_category

        canonical = _canonical_framing_evidence(
            item,
            item_analysis_category,
            scale_tolerance,
            alignment_tolerance,
            normalization_mode=normalization_mode,
        )

        normalizer_agreement = _normalizer_agreement_evidence(
            item,
            item_analysis_category,
            normalization_mode=normalization_mode,
        )

        issues = []
        severity = "pass"

        if m["segmentation_suspect"]:
            issues.append("Foreground detection looks unreliable")
            severity = "review"

        if b["count"] >= 2:
            if scale_ratio < 1.0 - scale_tolerance:
                issues.append(
                    f"Primary body looks too zoomed out ({abs(1-scale_ratio):.0%} smaller than subgroup median)"
                )
            elif scale_ratio > 1.0 + scale_tolerance:
                issues.append(
                    f"Primary body looks too zoomed in ({abs(scale_ratio-1):.0%} larger than subgroup median)"
                )

            alignment_delta = abs(m[b["align_axis"]] - b["align_median"])
            if alignment_delta > alignment_tolerance:
                if b["align_axis"] == "baseline_y":
                    issues.append("Primary-body baseline does not match comparable products")
                else:
                    issues.append("Primary-body vertical centering does not match comparable products")

        # Absolute sanity check: prevents a small/incomplete peer group from
        # declaring collectively bad framing "normal".
        has_scale_issue = any(
            "zoomed" in issue.lower()
            or "too small" in issue.lower()
            or "too large" in issue.lower()
            for issue in issues
        )
        has_vertical_issue = any(
            "baseline" in issue.lower()
            or "vertical centering" in issue.lower()
            for issue in issues
        )

        if (
            canonical["scale_issue"]
            and not has_scale_issue
            and not m["segmentation_suspect"]
        ):
            issues.append(canonical["scale_issue"])

        if (
            canonical["alignment_issue"]
            and not has_vertical_issue
            and not m["segmentation_suspect"]
        ):
            issues.append(canonical["alignment_issue"])

        # V6.5.6 normalizer-agreement guard.
        # If "Normalize every image" would make a visible correction, the
        # Detection/Audit result must not claim the source is already perfect.
        if not m["segmentation_suspect"]:
            has_scale_issue = any(
                "zoomed" in issue.lower()
                or "too small" in issue.lower()
                or "too large" in issue.lower()
                or "visibly enlarge" in issue.lower()
                or "visibly reduce" in issue.lower()
                for issue in issues
            )

            has_horizontal_issue = any(
                "shifted too far" in issue.lower()
                or "move the product left" in issue.lower()
                or "move the product right" in issue.lower()
                for issue in issues
            )

            has_vertical_issue = any(
                "baseline" in issue.lower()
                or "vertical centering" in issue.lower()
                or "move the product up" in issue.lower()
                or "move the product down" in issue.lower()
                for issue in issues
            )

            if (
                normalizer_agreement["scale_issue"]
                and not has_scale_issue
            ):
                issues.append(
                    normalizer_agreement["scale_issue"]
                )

            if (
                normalizer_agreement["horizontal_issue"]
                and not has_horizontal_issue
            ):
                issues.append(
                    normalizer_agreement["horizontal_issue"]
                )

            if (
                normalizer_agreement["vertical_issue"]
                and not has_vertical_issue
            ):
                issues.append(
                    normalizer_agreement["vertical_issue"]
                )

        if m["center_offset_x"] > alignment_tolerance:
            direction = "right" if m["center_x"] > 0.5 else "left"
            issues.append(f"Primary product body is shifted too far {direction}")

        if m["min_edge_padding"] < 0.012:
            issues.append("Full product/accessory extent is touching or nearly touching the image edge")

        if issues:
            severity = "review"

        score = 100.0
        score -= min(38.0, abs(scale_ratio - 1.0) * 120.0)
        score -= min(
            22.0,
            abs(canonical["scale_ratio"] - 1.0) * 62.0,
        )
        score -= min(
            18.0,
            abs(normalizer_agreement["scale_delta"]) * 115.0,
        )
        score -= min(
            12.0,
            abs(normalizer_agreement["x_shift"]) * 180.0,
        )
        score -= min(
            12.0,
            abs(normalizer_agreement["y_shift"]) * 180.0,
        )
        score -= min(24.0, m["center_offset_x"] * 260.0)
        if b["count"] >= 2:
            score -= min(
                20.0,
                abs(m[b["align_axis"]] - b["align_median"]) * 180.0,
            )
        if m["segmentation_suspect"]:
            score -= 25.0
        score = max(0.0, min(100.0, score))

        item["comparison_group"] = (
            item["group_key"]
            if subgroup["count"] >= 2
            else f"profile::{item.get('profile_group_key', item['profile'])}"
        )
        item["scale_ratio"] = float(scale_ratio)
        item["canonical_scale_ratio"] = float(
            canonical["scale_ratio"]
        )
        item["canonical_target"] = float(
            canonical["target"]
        )
        item["canonical_axis"] = canonical["axis"]
        item["normalizer_recommended_scale"] = float(
            normalizer_agreement["recommended_scale"]
        )
        item["normalizer_scale_delta"] = float(
            normalizer_agreement["scale_delta"]
        )
        item["normalizer_x_shift"] = float(
            normalizer_agreement["x_shift"]
        )
        item["normalizer_y_shift"] = float(
            normalizer_agreement["y_shift"]
        )
        item["normalizer_anchor_mode"] = (
            normalizer_agreement["anchor_mode"]
        )
        item["issues"] = issues
        item["status"] = severity
        item["consistency_score"] = float(score)

    total = len(valid)
    flagged = sum(1 for x in valid if x["status"] != "pass")
    failed_count = len(failures)
    result_summary = page_result_summary(
        total,
        flagged,
        failed_count,
    )

    return {
        "url": scraped["final_url"],
        "normalization_mode": normalization_mode,
        "items": valid,
        "failures": failures,
        "baselines": baselines,
        "raw_candidate_count": raw_candidate_count,
        "candidate_count": len(filtered_items),
        "page_membership_mode": "strict_dom_listing_with_recovery",
        "dom_verified_count": scraped.get("dom_verified_count", raw_candidate_count),
        "recovered_membership_count": sum(
            1
            for x in scraped.get("items", [])
            if x.get("page_membership_method")
            == "strong_product_link_recovery"
        ),
        "jsonld_discovered_count": scraped.get("jsonld_discovered_count", 0),
        "filtered_out_count": len(excluded_items),
        "excluded_items": excluded_items,
        "manual_included_count": sum(
            1
            for item in filtered_items
            if item.get("manual_filter_override")
        ),
        "manual_include_keys": sorted(
            set(forced_include_keys or set())
        ),
        "manual_category_overrides": dict(
            item_category_overrides
            or {}
        ),
        "effective_category": effective_category,
        "category_gate_mode": category_gate_mode(
            category,
            effective_category,
        ),
        "category_gate_label": (
            effective_category
            if effective_category
            else "Mixed / no single category gate"
        ),
        "page_context": scraped.get("page_context", ""),
        "analyzed_count": total,
        "failed_count": failed_count,
        "flagged_count": flagged,
        "scan_status": result_summary["scan_status"],
        "scan_confidence": result_summary["confidence"],
        "page_score": result_summary["page_score"],
    }
