import json
import re
from urllib.parse import quote

import scrapy

from myproject.items import LearningPrintableItem

BASE = "https://printables.hp.com"
LOCALE = "us"
LANG = "en"
FORMATTED_LANG = "en-us"

# Next.js server action IDs observed in current production bundles.
# If HP deploys new bundles, these can change.
ACTION_GET_SUBJECT = "606213f5f08c659f8958127c507abccc6ee54f16d8"
ACTION_GET_TOPICS_FROM_LIST = "60184eaa87a7a8fdbade27b7c19ed8a97a7b0bf968"
ACTION_GET_PRINTABLES = "78bef5789ac980cb32e09939df143312b46130438f"
GET_PRINTABLES_MAX_BATCH = 25


class HpLearningSpider(scrapy.Spider):
    name = "hp_learning"
    allowed_domains = ["printables.hp.com"]
    start_urls = [f"{BASE}/{LOCALE}/{LANG}/learning"]
    custom_settings = {
        "DOWNLOAD_DELAY": 0,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "CONCURRENT_REQUESTS": 24,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 8,
    }

    def parse(self, response):
        """Parse learning landing page and discover grade + subject UIDs."""
        rsc_blocks = self._extract_rsc_blocks(response.text)

        for content in rsc_blocks:
            if "gradeList" not in content or "group_title" not in content:
                continue

            group_title_match = re.search(r'"group_title":"([^"]+)"', content)
            if not group_title_match:
                continue

            grade_group = group_title_match.group(1)
            grade_list = self._extract_json_array(content, "gradeList") or []

            for grade in grade_list:
                if not isinstance(grade, dict):
                    continue

                grade_uid = grade.get("uid")
                grade_title = grade.get("grade_display_title")
                subjects = grade.get("subjects", [])

                if not grade_uid or not grade_title:
                    continue

                for subject_ref in subjects:
                    if not isinstance(subject_ref, dict):
                        continue

                    subject_uid = subject_ref.get("uid")
                    if not subject_uid:
                        continue

                    subject_url = f"{BASE}/{LOCALE}/{LANG}/learning/{grade_uid}/{subject_uid}"
                    yield scrapy.Request(
                        url=subject_url,
                        method="POST",
                        headers=self._build_headers(
                            referer=subject_url,
                            action_id=ACTION_GET_SUBJECT,
                            state_tree=self._build_learning_state_tree(
                                grade_uid=grade_uid,
                                subject_uid=subject_uid,
                            ),
                        ),
                        body=json.dumps([subject_uid, FORMATTED_LANG]),
                        callback=self.parse_subject,
                        cb_kwargs={
                            "grade_group": grade_group,
                            "grade_uid": grade_uid,
                            "grade_title": grade_title,
                            "subject_uid": subject_uid,
                        },
                        dont_filter=True,
                    )

    def parse_subject(self, response, grade_group, grade_uid, grade_title, subject_uid):
        """Resolve subject metadata and topic refs via getSubject action."""
        subject = self._parse_rsc_second_line_json(response.text)
        if not isinstance(subject, dict):
            return

        subject_title = subject.get("subject_display_title") or subject.get("title", "")
        topic_refs = subject.get("topics", [])
        if not topic_refs:
            return

        subject_url = f"{BASE}/{LOCALE}/{LANG}/learning/{grade_uid}/{subject_uid}"
        yield scrapy.Request(
            url=subject_url,
            method="POST",
            headers=self._build_headers(
                referer=subject_url,
                action_id=ACTION_GET_TOPICS_FROM_LIST,
                state_tree=self._build_learning_state_tree(
                    grade_uid=grade_uid,
                    subject_uid=subject_uid,
                ),
            ),
            body=json.dumps([topic_refs, FORMATTED_LANG]),
            callback=self.parse_topics,
            cb_kwargs={
                "grade_group": grade_group,
                "grade_uid": grade_uid,
                "grade_title": grade_title,
                "subject_uid": subject_uid,
                "subject_title": subject_title,
            },
            dont_filter=True,
        )

    def parse_topics(
        self,
        response,
        grade_group,
        grade_uid,
        grade_title,
        subject_uid,
        subject_title,
    ):
        """Resolve topics and fetch full printable records in subject-level batches."""
        topics = self._parse_rsc_second_line_json(response.text)
        if not isinstance(topics, list):
            return

        subject_url = f"{BASE}/{LOCALE}/{LANG}/learning/{grade_uid}/{subject_uid}"
        topic_by_activity_uid = {}
        all_activity_uids = []

        for topic in topics:
            if not isinstance(topic, dict):
                continue

            topic_uid = topic.get("uid", "")
            topic_title = topic.get("topic_display_title") or topic.get("title", "")
            activity_refs = topic.get("activities", [])
            activity_uids = [
                ref.get("uid") for ref in activity_refs if isinstance(ref, dict) and ref.get("uid")
            ]
            topic_activity_count = len(activity_uids)

            for activity_uid in activity_uids:
                if activity_uid not in topic_by_activity_uid:
                    topic_by_activity_uid[activity_uid] = {
                        "topic_uid": topic_uid,
                        "topic_title": topic_title,
                        "topic_activity_count": topic_activity_count,
                    }
                    all_activity_uids.append(activity_uid)

        if not all_activity_uids:
            return

        # getPrintables currently returns up to 25 items per request.
        for activity_uid_batch in self._chunked(all_activity_uids, GET_PRINTABLES_MAX_BATCH):
            yield scrapy.Request(
                url=subject_url,
                method="POST",
                headers=self._build_headers(
                    referer=subject_url,
                    action_id=ACTION_GET_PRINTABLES,
                    state_tree=self._build_learning_state_tree(
                        grade_uid=grade_uid,
                        subject_uid=subject_uid,
                    ),
                ),
                body=json.dumps([activity_uid_batch, FORMATTED_LANG, 0]),
                callback=self.parse_subject_activities,
                cb_kwargs={
                    "grade_group": grade_group,
                    "grade_uid": grade_uid,
                    "grade_title": grade_title,
                    "subject_uid": subject_uid,
                    "subject_title": subject_title,
                    "topic_by_activity_uid": topic_by_activity_uid,
                },
                dont_filter=True,
            )

    def parse_subject_activities(
        self,
        response,
        grade_group,
        grade_uid,
        grade_title,
        subject_uid,
        subject_title,
        topic_by_activity_uid,
    ):
        """Emit one output item per learning activity printable."""
        activities = self._parse_rsc_second_line_json(response.text)
        if not isinstance(activities, list):
            return

        for entry in activities:
            if not isinstance(entry, dict):
                continue

            activity_uid = entry.get("uid", "")
            topic_data = topic_by_activity_uid.get(activity_uid, {})
            title_raw = entry.get("title", "")

            item = LearningPrintableItem()
            item["grade_group"] = grade_group
            item["grade_uid"] = grade_uid
            item["grade_title"] = grade_title
            item["subject_uid"] = subject_uid
            item["subject_title"] = subject_title
            item["topic_uid"] = topic_data.get("topic_uid", "")
            item["topic_title"] = topic_data.get("topic_title", "")
            item["topic_activity_count"] = topic_data.get("topic_activity_count", 0)
            item["activity_uid"] = activity_uid
            item["title_raw"] = title_raw
            item["title"] = self._clean_learning_title(title_raw)
            item["about"] = entry.get("about")
            item["url"] = self._normalize_url(entry.get("url", ""))
            item["tags"] = entry.get("tags", [])
            item["premium"] = entry.get("premium", False)
            item["pdf_url"] = entry.get("printable_file", {}).get("url")
            item["thumbnail_url"] = entry.get("printable_thumbnail_image", {}).get("url")
            item["preview_image_url"] = entry.get("printable_preview_image", {}).get("url")
            yield item

    def _normalize_url(self, value):
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("/"):
            return BASE + value
        return BASE + "/" + LOCALE + "/" + LANG + "/" + value

    def _chunked(self, values, size):
        for i in range(0, len(values), size):
            yield values[i : i + size]

    def _clean_learning_title(self, value):
        if not value:
            return ""

        title = str(value).strip()
        title = re.sub(r"^\([a-z]{2}-[a-z]{2}\)\s*", "", title, flags=re.IGNORECASE)

        # Strip CMS-internal trailing descriptors like:
        # (B4_Reading and Writing (2-4)) or (3-4_Literacy_..._WAVE1)
        suffix_match = re.search(r"\s*\((.*)\)\s*$", title)
        if suffix_match:
            suffix = suffix_match.group(1)
            if (
                "_" in suffix
                or re.search(r"\bB\d+\b", suffix, flags=re.IGNORECASE)
                or "wave" in suffix.lower()
            ):
                title = title[: suffix_match.start()].strip()

        return title.strip()

    def _build_headers(self, referer, action_id, state_tree):
        return {
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": referer,
            "Origin": BASE,
            "next-action": action_id,
            "next-router-state-tree": quote(state_tree, safe=""),
        }

    def _build_learning_state_tree(self, grade_uid, subject_uid):
        return json.dumps(
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
                                        "learning",
                                        {
                                            "children": [
                                                ["grade", grade_uid, "d"],
                                                {
                                                    "children": [
                                                        ["subject", subject_uid, "d"],
                                                        {
                                                            "children": [
                                                                "__PAGE__",
                                                                {},
                                                                None,
                                                                None,
                                                            ]
                                                        },
                                                    ]
                                                },
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                    ]
                },
                None,
                None,
                True,
            ]
        )

    def _parse_rsc_second_line_json(self, text):
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return None

        try:
            return json.loads(lines[1].split(":", 1)[1])
        except (ValueError, json.JSONDecodeError):
            return None

    def _extract_rsc_blocks(self, html):
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
                continue
        return blocks

    def _extract_json_array(self, content, key):
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
