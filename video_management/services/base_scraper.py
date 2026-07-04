"""Base scraper REMOVED — stub to prevent import crashes."""


class ScraperException(Exception):
    pass


class ScraperTimeoutException(ScraperException):
    pass


class ScraperRateLimitException(ScraperException):
    pass


class ScraperNotFoundException(ScraperException):
    pass


class BaseScraperService:
    def __init__(self, *a, **kw):
        raise NotImplementedError("Old scraper removed. Use BrightData.")
