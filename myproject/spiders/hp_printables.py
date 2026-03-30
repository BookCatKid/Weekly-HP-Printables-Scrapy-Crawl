import json
import re
from urllib.parse import quote

import scrapy

from myproject.items import PrintableItem

BASE = "https://printables.hp.com"
LOCALE = "us"
LANG = "en"


class HpPrintablesSpider(scrapy.Spider):
    name = "hp_printables"
    allowed_domains = ["printables.hp.com"]
    start_urls = [f"{BASE}/{LOCALE}/{LANG}/collections"]

    custom_settings = {
        "DOWNLOAD_DELAY": 0,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "CONCURRENT_REQUESTS": 24,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 8,
    }

    def parse(self, response):
        """Parse the collections index page to discover all collection slugs."""
        rsc_blocks = self._extract_rsc_blocks(response.text)
        slugs = set()
        for content in rsc_blocks:
            slugs.update(re.findall(r"/collection/([a-z0-9][a-z0-9-]+)", content))

        for slug in sorted(slugs):
            yield scrapy.Request(
                url=f"{BASE}/{LOCALE}/{LANG}/collections/{slug}",
                callback=self.parse_collection,
                cb_kwargs={"slug": slug},
            )

    def parse_collection(self, response, slug):
        """Parse a collection page HTML to extract item UIDs, action ID, and categories."""
        rsc_blocks = self._extract_rsc_blocks(response.text)

        entry_uids = None
        action_id = None
        category_map = {}

        for content in rsc_blocks:
            # Extract the entryName array (item UIDs) and collectionCategories
            if "entryName" in content and entry_uids is None:
                entry_uids = self._extract_json_array(content, "entryName")

                # Extract category uid -> title mapping
                if "collectionCategories" in content:
                    cats = self._extract_json_array(content, "collectionCategories")
                    if cats:
                        for cat in cats:
                            if isinstance(cat, dict) and "uid" in cat and "title" in cat:
                                category_map[cat["uid"]] = cat["title"]

                # Extract the $F reference for getPrintablesFunction
                ref_match = re.search(
                    r'"getPrintablesFunction":"\$F([0-9a-zA-Z]+)"', content
                )
                if ref_match:
                    func_ref = ref_match.group(1)
                    # Search all blocks for the action ID definition
                    for other in rsc_blocks:
                        id_match = re.search(
                            rf'^{func_ref}:\{{"id":"([a-f0-9]+)"',
                            other,
                            re.MULTILINE,
                        )
                        if not id_match:
                            id_match = re.search(
                                rf'{func_ref}:\{{"id":"([a-f0-9]+)"', other
                            )
                        if id_match:
                            action_id = id_match.group(1)
                            break

        if not entry_uids or not action_id:
            self.logger.warning(
                "Could not extract data for collection %s (uids=%s, action=%s)",
                slug,
                bool(entry_uids),
                bool(action_id),
            )
            return

        # Build the next-router-state-tree for this collection
        state_tree = json.dumps(
            [
                "",
                {
                    "children": [
                        ["locale", LOCALE, "d"],
                        {
                            "children": [
                                ["lang", LANG, "d"],
                                {
                                    "children": [
                                        "collections",
                                        {
                                            "children": [
                                                ["collectionName", slug, "d"],
                                                {
                                                    "children": [
                                                        "__PAGE__",
                                                        {},
                                                        None,
                                                        None,
                                                    ]
                                                },
                                                None,
                                                None,
                                            ]
                                        },
                                        None,
                                        None,
                                    ]
                                },
                                None,
                                None,
                            ]
                        },
                        None,
                        None,
                    ]
                },
                None,
                None,
                True,
            ]
        )

        collection_url = f"{BASE}/{LOCALE}/{LANG}/collections/{slug}"
        yield scrapy.Request(
            url=collection_url,
            method="POST",
            headers={
                "Accept": "text/x-component",
                "Content-Type": "text/plain;charset=UTF-8",
                "Referer": collection_url,
                "Origin": BASE,
                "next-action": action_id,
                "next-router-state-tree": quote(state_tree, safe=""),
            },
            body=json.dumps([entry_uids, f"{LANG}-{LOCALE}", 0]),
            callback=self.parse_items,
            cb_kwargs={"slug": slug, "category_map": category_map},
            dont_filter=True,
        )

    def parse_items(self, response, slug, category_map):
        """Parse the Server Action JSON response into PrintableItems."""
        lines = response.text.strip().split("\n")
        if len(lines) < 2:
            self.logger.warning("Unexpected response format for collection %s", slug)
            return

        json_str = lines[1].split(":", 1)[1]
        items = json.loads(json_str)

        for entry in items:
            if not isinstance(entry, dict):
                continue

            # Resolve category UIDs to names
            categories = []
            for cat_ref in entry.get("category", []):
                cat_uid = cat_ref.get("uid", "")
                categories.append(category_map.get(cat_uid, cat_uid))

            item = PrintableItem()
            item["collection"] = slug
            item["category"] = categories
            item["uid"] = entry.get("uid")
            item["title"] = entry.get("title")
            item["about"] = entry.get("about")
            item["url"] = BASE + "/" + LOCALE + "/" + LANG + entry.get("url", "")
            item["tags"] = entry.get("tags", [])
            item["premium"] = entry.get("premium", False)
            item["pdf_url"] = entry.get("printable_file", {}).get("url")
            item["thumbnail_url"] = entry.get("printable_thumbnail_image", {}).get(
                "url"
            )
            item["preview_image_url"] = entry.get("printable_preview_image", {}).get(
                "url"
            )
            yield item

    def _extract_rsc_blocks(self, html):
        """Extract React Server Component data blocks from __next_f script tags."""
        blocks = []
        for match in re.finditer(
            r"<script[^>]*>self\.__next_f\.push\((\[.*?\])\)</script>",
            html,
            re.DOTALL,
        ):
            try:
                arr = json.loads(match.group(1))
                content = str(arr[1]) if len(arr) > 1 else str(arr)
                blocks.append(content)
            except (json.JSONDecodeError, IndexError):
                pass
        return blocks

    def _extract_json_array(self, content, key):
        """Extract a JSON array value for a given key from RSC content string."""
        try:
            idx = content.index(f'"{key}"')
            bracket_start = content.index("[", idx)
            depth = 0
            for i in range(bracket_start, len(content)):
                if content[i] == "[":
                    depth += 1
                elif content[i] == "]":
                    depth -= 1
                    if depth == 0:
                        return json.loads(content[bracket_start : i + 1])
        except (ValueError, json.JSONDecodeError):
            return None
