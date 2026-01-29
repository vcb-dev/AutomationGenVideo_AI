-- =====================================================
-- Migration Script: AI Database to BE Database
-- =====================================================
-- This script migrates all tables from video_production_ai 
-- to video_production database
-- 
-- IMPORTANT: Run this script AFTER updating Django settings
-- and running Django migrations on the BE database
-- =====================================================

-- Step 1: Connect to the BE database (video_production)
\c video_production

-- Step 2: Check if tables already exist
-- If they exist, you may want to backup first

-- Step 3: Copy data from AI database to BE database
-- Note: Adjust table names if Django uses different naming conventions

-- Copy SearchHistory data
INSERT INTO video_management_searchhistory 
    (id, created_at, updated_at, platform, keyword, status, min_likes, min_views, 
     max_results, results_count, raw_results, task_id, error_message, execution_time, expires_at)
SELECT 
    id, created_at, updated_at, platform, keyword, status, min_likes, min_views,
    max_results, results_count, raw_results, task_id, error_message, execution_time, expires_at
FROM video_production_ai.video_management_searchhistory
ON CONFLICT (id) DO NOTHING;

-- Copy ScrapedVideo data
INSERT INTO video_management_scrapedvideo
    (id, created_at, updated_at, platform, video_id, title, description, 
     author_username, author_name, likes_count, views_count, comments_count, 
     shares_count, video_url, download_url, thumbnail_url, published_at, 
     hashtags, music_info, raw_data, search_history_id)
SELECT 
    id, created_at, updated_at, platform, video_id, title, description,
    author_username, author_name, likes_count, views_count, comments_count,
    shares_count, video_url, download_url, thumbnail_url, published_at,
    hashtags, music_info, raw_data, search_history_id
FROM video_production_ai.video_management_scrapedvideo
ON CONFLICT (video_id) DO UPDATE SET
    likes_count = EXCLUDED.likes_count,
    views_count = EXCLUDED.views_count,
    comments_count = EXCLUDED.comments_count,
    shares_count = EXCLUDED.shares_count,
    updated_at = EXCLUDED.updated_at;

-- Copy TrackedChannel data (AI version)
-- Note: BE has a different TrackedChannel schema with user_id
-- We'll need to handle this carefully
INSERT INTO video_management_trackedchannel
    (id, created_at, updated_at, platform, channel_id, username, display_name,
     is_active, check_interval_minutes, min_likes_threshold, last_checked_at, follower_count)
SELECT 
    id, created_at, updated_at, platform, channel_id, username, display_name,
    is_active, check_interval_minutes, min_likes_threshold, last_checked_at, follower_count
FROM video_production_ai.video_management_trackedchannel
ON CONFLICT (platform, channel_id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    follower_count = EXCLUDED.follower_count,
    last_checked_at = EXCLUDED.last_checked_at,
    updated_at = EXCLUDED.updated_at;

-- Copy VideoCollection data (if exists)
INSERT INTO video_management_videocollection
    (id, created_at, updated_at, name, description, color)
SELECT 
    id, created_at, updated_at, name, description, color
FROM video_production_ai.video_management_videocollection
ON CONFLICT (id) DO NOTHING;

-- Copy CollectionVideo data (if exists)
INSERT INTO video_management_collectionvideo
    (id, created_at, updated_at, collection_id, video_id, notes, "order")
SELECT 
    id, created_at, updated_at, collection_id, video_id, notes, "order"
FROM video_production_ai.video_management_collectionvideo
ON CONFLICT (collection_id, video_id) DO NOTHING;

-- Step 4: Update sequences to prevent ID conflicts
SELECT setval('video_management_searchhistory_id_seq', 
    (SELECT MAX(id) FROM video_management_searchhistory), true);

SELECT setval('video_management_scrapedvideo_id_seq', 
    (SELECT MAX(id) FROM video_management_scrapedvideo), true);

SELECT setval('video_management_trackedchannel_id_seq', 
    (SELECT MAX(id) FROM video_management_trackedchannel), true);

SELECT setval('video_management_videocollection_id_seq', 
    (SELECT MAX(id) FROM video_management_videocollection), true);

SELECT setval('video_management_collectionvideo_id_seq', 
    (SELECT MAX(id) FROM video_management_collectionvideo), true);

-- Step 5: Verify migration
SELECT 'SearchHistory' as table_name, COUNT(*) as record_count 
FROM video_management_searchhistory
UNION ALL
SELECT 'ScrapedVideo', COUNT(*) 
FROM video_management_scrapedvideo
UNION ALL
SELECT 'TrackedChannel', COUNT(*) 
FROM video_management_trackedchannel
UNION ALL
SELECT 'VideoCollection', COUNT(*) 
FROM video_management_videocollection
UNION ALL
SELECT 'CollectionVideo', COUNT(*) 
FROM video_management_collectionvideo;

-- =====================================================
-- NOTES:
-- 1. Make sure to backup both databases before running
-- 2. The BE TrackedChannel has user_id field which AI doesn't have
--    You may need to manually assign user_ids after migration
-- 3. Run Django migrations first: python manage.py migrate
-- 4. Test thoroughly after migration
-- =====================================================
