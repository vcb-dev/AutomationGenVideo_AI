"""
Instagram mock data generator for demo purposes.
Since Instagram Apify actors are limited on free tier.
"""

def generate_instagram_mock_data(keyword: str, count: int = 10):
    """Generate mock Instagram posts for testing."""
    import random
    from datetime import datetime, timedelta
    
    mock_posts = []
    base_id = 100000000
    
    for i in range(count):
        post_id = str(base_id + i)
        
        # Random engagement
        likes = random.randint(500, 50000)
        comments = random.randint(10, 500)
        views = random.randint(likes * 2, likes * 10)
        
        # Random timestamp in last 30 days
        days_ago = random.randint(0, 30)
        timestamp = datetime.now() - timedelta(days=days_ago)
        
        mock_posts.append({
            'id': post_id,
            'shortCode': f'ABC{post_id}',
            'caption': f'Beautiful {keyword} content #{keyword} #instagram #viral',
            'ownerUsername': f'user_{i}',
            'ownerFullName': f'User {i}',
            'likesCount': likes,
            'commentsCount': comments,
            'videoViewCount': views,
            'displayUrl': f'https://picsum.photos/400/600?random={i}',
            'videoUrl': f'https://www.instagram.com/p/{post_id}/',
            'url': f'https://www.instagram.com/p/{post_id}/',
            'type': 'Video' if i % 2 == 0 else 'Image',
            'timestamp': timestamp.isoformat(),
            'hashtags': [keyword, 'instagram', 'viral'],
            'alt': f'Instagram post about {keyword}',
        })
    
    return mock_posts


# Test
if __name__ == '__main__':
    import json
    
    posts = generate_instagram_mock_data('fashion', 5)
    print(json.dumps(posts, indent=2, ensure_ascii=False))
