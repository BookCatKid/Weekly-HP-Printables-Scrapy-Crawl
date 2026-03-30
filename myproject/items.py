import scrapy


class QuoteItem(scrapy.Item):
    text = scrapy.Field()
    author = scrapy.Field()
    tags = scrapy.Field()


class PrintableItem(scrapy.Item):
    collection = scrapy.Field()
    category = scrapy.Field()
    uid = scrapy.Field()
    title = scrapy.Field()
    about = scrapy.Field()
    url = scrapy.Field()
    tags = scrapy.Field()
    premium = scrapy.Field()
    pdf_url = scrapy.Field()
    thumbnail_url = scrapy.Field()
    preview_image_url = scrapy.Field()


class LearningPrintableItem(scrapy.Item):
    grade_group = scrapy.Field()
    grade_uid = scrapy.Field()
    grade_title = scrapy.Field()
    subject_uid = scrapy.Field()
    subject_title = scrapy.Field()
    topic_uid = scrapy.Field()
    topic_title = scrapy.Field()
    topic_activity_count = scrapy.Field()
    activity_uid = scrapy.Field()
    title_raw = scrapy.Field()
    title = scrapy.Field()
    about = scrapy.Field()
    url = scrapy.Field()
    tags = scrapy.Field()
    premium = scrapy.Field()
    pdf_url = scrapy.Field()
    thumbnail_url = scrapy.Field()
    preview_image_url = scrapy.Field()
