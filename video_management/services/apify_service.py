"""Apify service REMOVED — stub to prevent import crashes.

All scraping now uses BrightData. These stubs exist only so old views
that haven't been migrated yet don't crash on import.
"""


class _Removed:
    def __init__(self, *a, **kw):
        raise NotImplementedError("Apify has been removed. Use BrightData scraper instead.")

    def __getattr__(self, name):
        raise NotImplementedError("Apify has been removed. Use BrightData scraper instead.")


ApifyScraperService = _Removed


def create_scraper(*a, **kw):
    raise NotImplementedError("Apify has been removed. Use BrightData scraper instead.")


def fetch_tiktok_user_profile(*a, **kw):
    raise NotImplementedError("Apify has been removed. Use BrightData scraper instead.")
