"""
API Views for Video Duplicate Detection
High-accuracy duplicate detection using deep learning
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache
from ..services.deep_learning_fingerprint_service import get_fingerprint_service
import os
import logging

logger = logging.getLogger(__name__)


@api_view(['POST'])
def generate_fingerprint(request):
    """
    Generate deep learning fingerprint for a video
    
    POST /api/ai/duplicate-detection/generate-fingerprint
    Body: {
        "video_path": "/path/to/video.mp4"
    }
    
    Returns: {
        "feature_vector": [512 floats],
        "duration_seconds": 120,
        "frame_count": 3600,
        "resolution": "1920x1080",
        "method": "deep_learning",
        "model": "r2plus1d_18"
    }
    """
    try:
        video_path = request.data.get('video_path')
        
        if not video_path:
            return Response(
                {'error': 'video_path is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not os.path.exists(video_path):
            return Response(
                {'error': f'Video file not found: {video_path}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get fingerprint service
        service = get_fingerprint_service()
        
        # Generate fingerprint
        logger.info(f"Generating fingerprint for: {video_path}")
        fingerprint = service.generate_fingerprint(video_path)
        
        logger.info(f"✅ Fingerprint generated successfully")
        
        return Response(fingerprint, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error generating fingerprint: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
def check_duplicate(request):
    """
    Check if a video is duplicate of existing videos
    
    POST /api/ai/duplicate-detection/check-duplicate
    Body: {
        "video_path": "/path/to/new_video.mp4",
        "existing_videos": [
            {
                "id": "uuid",
                "title": "Old video",
                "feature_vector": [512 floats]
            }
        ]
    }
    
    Returns: {
        "is_duplicate": true/false,
        "matched_video": {...} or null,
        "similarity": 0.95,
        "confidence": "high",
        "needs_review": false
    }
    """
    try:
        video_path = request.data.get('video_path')
        existing_videos = request.data.get('existing_videos', [])
        
        if not video_path:
            return Response(
                {'error': 'video_path is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not os.path.exists(video_path):
            return Response(
                {'error': f'Video file not found: {video_path}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get fingerprint service
        service = get_fingerprint_service()
        
        # Extract features from new video
        logger.info(f"Extracting features from: {video_path}")
        new_features = service.extract_features(video_path)
        
        # Compare with existing videos
        best_match = None
        highest_similarity = 0.0
        
        for old_video in existing_videos:
            if 'feature_vector' not in old_video:
                continue
            
            old_features = old_video['feature_vector']
            
            # Compare
            comparison = service.compare_features(new_features, old_features)
            
            if comparison['similarity'] > highest_similarity:
                highest_similarity = comparison['similarity']
                best_match = {
                    'video': old_video,
                    'comparison': comparison
                }
        
        # Prepare response
        if best_match and best_match['comparison']['similarity'] > 0.85:
            return Response({
                'is_duplicate': best_match['comparison']['is_duplicate'],
                'matched_video': best_match['video'],
                'similarity': best_match['comparison']['similarity'],
                'confidence': best_match['comparison']['confidence'],
                'needs_review': best_match['comparison']['needs_review']
            }, status=status.HTTP_200_OK)
        else:
            return Response({
                'is_duplicate': False,
                'matched_video': None,
                'similarity': highest_similarity,
                'confidence': 'low',
                'needs_review': False
            }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error checking duplicate: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
def compare_videos(request):
    """
    Compare two specific videos
    
    POST /api/ai/duplicate-detection/compare-videos
    Body: {
        "video1_path": "/path/to/video1.mp4",
        "video2_path": "/path/to/video2.mp4"
    }
    
    Returns: {
        "similarity": 0.95,
        "is_duplicate": true,
        "confidence": "high",
        "needs_review": false
    }
    """
    try:
        video1_path = request.data.get('video1_path')
        video2_path = request.data.get('video2_path')
        
        if not video1_path or not video2_path:
            return Response(
                {'error': 'Both video1_path and video2_path are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not os.path.exists(video1_path):
            return Response(
                {'error': f'Video 1 not found: {video1_path}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if not os.path.exists(video2_path):
            return Response(
                {'error': f'Video 2 not found: {video2_path}'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get fingerprint service
        service = get_fingerprint_service()
        
        # Extract features from both videos
        logger.info(f"Comparing videos: {video1_path} vs {video2_path}")
        features1 = service.extract_features(video1_path)
        features2 = service.extract_features(video2_path)
        
        # Compare
        comparison = service.compare_features(features1, features2)
        
        logger.info(f"✅ Comparison result: {comparison['similarity']:.2%} similarity")
        
        return Response(comparison, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error comparing videos: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
