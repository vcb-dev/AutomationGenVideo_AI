from django.apps import AppConfig


class VideoManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'video_management'

    def ready(self):
        """
        Auto-resume pre-generation when server starts.
        If there are indexed videos without cached clips,
        automatically start background clip generation.
        """
        import threading
        import os

        # Only run in the main process (not in autoreload child)
        # Django runs ready() twice with autoreload: once in parent, once in child
        # We only want to run in the child (the one that serves requests)
        if os.environ.get('RUN_MAIN') != 'true':
            return

        def _auto_resume_pregen():
            """Check and resume pre-gen after a short delay (let Django fully start)."""
            import time
            time.sleep(5)  # Wait for Django to fully initialize

            try:
                from video_management.models import IndexedVideo, VideoClipCache

                indexed_count = IndexedVideo.objects.filter(is_available=True).count()
                cached_ids = set(VideoClipCache.objects.values_list('source_video_id', flat=True))
                uncached_count = IndexedVideo.objects.filter(
                    is_available=True
                ).exclude(id__in=cached_ids).count()

                if indexed_count > 0 and uncached_count > 0:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.info(
                        f"🔄 Auto-resume pre-gen: {indexed_count} indexed, "
                        f"{len(cached_ids)} cached, {uncached_count} need clips"
                    )

                    from video_management.services.smart_preprocessing_service import (
                        start_background_pregen
                    )
                    start_background_pregen(clip_duration=12.0)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Auto-resume pregen error: {e}")

        # Start in daemon thread so it doesn't block server startup
        thread = threading.Thread(target=_auto_resume_pregen, daemon=True, name="auto_pregen")
        thread.start()
