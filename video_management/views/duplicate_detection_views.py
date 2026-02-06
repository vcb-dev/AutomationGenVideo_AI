"""
API Views for Video Duplicate Detection
High-accuracy duplicate detection using deep learning
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache
from ..services.deep_learning_fingerprint_service import get_fingerprint_service
import tempfile
import shutil
import os
import logging

logger = logging.getLogger(__name__)

def save_uploaded_file(uploaded_file):
    """Save uploaded file to temp path"""
    fd, path = tempfile.mkstemp(suffix='.mp4')
    try:
        with os.fdopen(fd, 'wb') as tmp:
            # Handle both chunked and non-chunked uploads
            if hasattr(uploaded_file, 'chunks'):
                for chunk in uploaded_file.chunks():
                    tmp.write(chunk)
            else:
                # For small files that are fully in memory
                tmp.write(uploaded_file.read())
        return path
    except Exception as e:
        # Clean up temp file on error
        try:
            os.close(fd)
            os.unlink(path)
        except:
            pass
        raise e

@api_view(['POST'])
def generate_fingerprint(request):
    """
    Generate deep learning fingerprint for a video
    Supports both 'video_path' (local) and 'video_file' (upload)
    """
    temp_path = None
    try:
        video_path = request.data.get('video_path')
        
        # Priority: Check for file upload
        if 'video_file' in request.FILES:
            upload = request.FILES['video_file']
            temp_path = save_uploaded_file(upload)
            video_path = temp_path
            logger.info(f"Received file upload. Saved to temp: {temp_path}")
        
        if not video_path:
            return Response(
                {'error': 'video_path or video_file is required'},
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
    finally:
        # Cleanup temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.info(f"Cleaned up temp file: {temp_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temp file: {e}")


from ..models import ScrapedVideo, Platform

@api_view(['POST'])
def check_duplicate(request):
    """
    Check if a video is duplicate of existing videos (Internal or Scraped)
    Supports both 'video_path' (local) and 'video_file' (upload)
    """
    temp_path = None
    try:
        video_path = request.data.get('video_path')
        existing_videos = request.data.get('existing_videos', [])
        check_source = request.data.get('check_source', 'internal')
        channel_info = request.data.get('channel_info', {})
        
        # Parse JSON fields if they are strings (Multipart/Form-data support)
        import json
        if isinstance(channel_info, str):
            try:
                channel_info = json.loads(channel_info)
            except Exception:
                logger.warning(f"Failed to parse channel_info JSON: {channel_info}")
                channel_info = {}
                
        if isinstance(existing_videos, str):
            try:
                existing_videos = json.loads(existing_videos)
            except Exception:
                existing_videos = []

        # Priority: Check for file upload
        if 'video_file' in request.FILES:
            upload = request.FILES['video_file']
            temp_path = save_uploaded_file(upload)
            video_path = temp_path
            logger.info(f"Received file upload. Saved to temp: {temp_path}")
        
        if not video_path:
            return Response({'error': 'video_path or video_file is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not os.path.exists(video_path):
            return Response({'error': f'Video file not found: {video_path}'}, status=status.HTTP_404_NOT_FOUND)
            
        # ... logic continues ...
        
        # Get fingerprint service
        service = get_fingerprint_service()
        
        # Extract features & Info from new video
        logger.info(f"Extracting features from: {video_path}")
        new_features = service.extract_features(video_path)
        new_video_info = service.get_video_info(video_path)
        new_duration = new_video_info['duration'] if new_video_info else 0
        
        # --- LOAD CHANNEL VIDEOS IF REQUESTED ---
        if check_source == 'channel_scan' and channel_info:
            try:
                username = channel_info.get('username')
                platform_str = channel_info.get('platform', '').upper()
                
                if username and platform_str in Platform.names:
                    # Normalize username: try both 'user' and '@user' formats
                    clean_username = username.lstrip('@')
                    usernames_to_check = [clean_username, f"@{clean_username}"]
                    
                    logger.info(f"Fetching ScrapedVideos for {usernames_to_check} on {platform_str}...")
                    
                    scraped_objs = ScrapedVideo.objects.filter(
                        author_username__in=usernames_to_check,
                        platform=Platform[platform_str]
                    ).order_by('-created_at')[:100] # Increased limit to 100
                    
                    if not scraped_objs:
                        # Fallback: Try case-insensitive search if exact match fails
                        scraped_objs = ScrapedVideo.objects.filter(
                            author_username__iexact=clean_username,
                            platform=Platform[platform_str]
                        ).order_by('-created_at')[:100]

                    for vid in scraped_objs:
                        # Extract duration from raw_data
                        dur = 0
                        if isinstance(vid.raw_data, dict):
                            dur = vid.raw_data.get('duration', 0)
                            if not dur: dur = vid.raw_data.get('video', {}).get('duration', 0)
                            # TikHub specific structure check
                            if not dur: dur = vid.raw_data.get('video_duration', 0)
                        
                        # Deserialize feature vector if available
                        feat_vec = None
                        if getattr(vid, 'feature_vector', None):
                            try:
                                import numpy as np
                                feat_vec = np.frombuffer(vid.feature_vector, dtype=np.float32)
                            except Exception as vec_err:
                                logger.warning(f"Failed to load vector for {vid.video_id}: {vec_err}")

                        existing_videos.append({
                            'id': vid.video_id,
                            'title': vid.title,
                            'duration': float(dur) if dur else 0,
                            'feature_vector': feat_vec, # Include vector for Deep Learning check
                            'is_scraped': True,
                            'platforms': [vid.platform]
                        })
                    logger.info(f"Added {len(scraped_objs)} scraped videos to comparison list.")
            except Exception as e:
                logger.error(f"Error loading channel videos: {e}")
        # ----------------------------------------
        
        # Compare with existing videos
        best_match = None
        highest_similarity = 0.0
        
        for old_video in existing_videos:
            sim = 0.0
            confidence = 'low'
            old_dur = old_video.get('duration', 0)
            
            # CASE A: Vector Comparison (Accurate)
            if old_video.get('feature_vector') is not None:
                try:
                    old_vec = old_video['feature_vector']
                    # Ensure both are numpy arrays for the service
                    import numpy as np
                    if isinstance(old_vec, list):
                        old_vec = np.array(old_vec, dtype=np.float32)
                    
                    comparison = service.compare_features(new_features, old_vec)
                    sim = comparison['similarity']
                    confidence = 'high'
                    logger.info(f"Vector Scan against {old_video.get('id')}: Sim={sim:.4f}")
                except Exception as comp_err:
                    logger.error(f"Vector comparison failed for {old_video.get('id')}: {comp_err}")
                    sim = 0.0
            
            # CASE B: Metadata Comparison (Fallback for Scraped Videos)
            else:
                # old_dur is already initialized
                
                
                # Get titles for fallback
                old_title = old_video.get('title', '')
                new_title = request.data.get('video_title', '')
                
                # Metadata Logic Enhancement
                if new_duration > 0 and old_dur > 0:
                    time_diff = abs(new_duration - old_dur)
                    
                    # 1. Exact Duration Match (High Risk)
                    if time_diff < 1.0:
                        sim = 0.85 
                        confidence = 'medium'
                        logger.info(f"Duration Exact Match! {new_duration}s vs {old_dur}s -> Sim: 0.85")
                    
                    # 2. Partial Duration Match (Smart Estimation)
                    else:
                        ratio = min(new_duration, old_dur) / max(new_duration, old_dur)
                        
                        # Dynamic Confidence Multiplier
                        if ratio > 0.6:
                            # High duration overlap (>60%) -> Likely significant reuse
                            multiplier = 0.85
                        elif ratio > 0.4:
                            # Medium overlap -> Ambiguous
                            multiplier = 0.6
                        else:
                            # Low overlap -> Conservative estimate
                            multiplier = 0.4
                            
                        sim = ratio * multiplier
                        confidence = 'low'
                        logger.info(f"Smart Duration Est: {new_duration}s vs {old_dur}s (Ratio: {ratio:.2f}) * {multiplier} -> Est Sim: {sim:.2f}")
                
                # 3. FALLBACK: Title/Text Matching (When duration is missing)
                # If scrapped video lacks duration data, we check if Titles depend on each other.
                elif old_title and new_title:
                     from difflib import SequenceMatcher
                     title_sim = SequenceMatcher(None, new_title.lower(), old_title.lower()).ratio()
                     
                     if title_sim > 0.4:
                         # If titles are somewhat similar (>40%), give a low-confidence score
                         sim = title_sim * 0.6 # Discount factor
                         confidence = 'low'
                         logger.info(f"Fallback Title Match: '{new_title}' vs '{old_title}' ({title_sim:.2f}) -> Est Sim: {sim:.2f}")

            
            # --- DURATION PENALTY LOGIC (Apply ONLY to Deep Learning Matches) ---
            # For Metadata matches, we already incorporated duration logic above.
            final_similarity = sim
            
            # Only apply penalty if it was a deep learning match (sim based on visual content)
            # Otherwise we double-penalize metadata matches
            # Only apply penalty if it was a deep learning match (sim based on visual content)
            # Otherwise we double-penalize metadata matches
            if confidence == 'high' and new_duration > 0 and old_dur > 0:
                # If checking against Scraped Video, use strict ratio
                # If checking against Internal Video, ratio logic is already handled or same
                ratio = min(new_duration, old_dur) / max(new_duration, old_dur)
                
                if ratio < 0.8:
                    final_similarity = sim * ratio
            
            # Update best match
            if final_similarity > highest_similarity:
                highest_similarity = final_similarity
                best_match = {
                    'video': old_video,
                    'comparison': {'similarity': final_similarity},
                    'matched_method': 'metadata' if confidence == 'medium' else 'deep_learning'
                }
        
        # Prepare response
        if best_match:
            similarity = best_match['comparison']['similarity']
            
            # Strict duplicate (Lowered from 0.85 to 0.80)
            if similarity > 0.80:
                 return Response({
                    'is_duplicate': True,
                    'matched_video': best_match['video'],
                    'similarity': similarity,
                    'confidence': 'high',
                    'needs_review': False,
                    'scanned_count': len(existing_videos)
                }, status=status.HTTP_200_OK)
            
            # Partial match / Edited video (Lowered to 0.40 as requested)
            elif similarity > 0.40:
                 return Response({
                    'is_duplicate': False, 
                    'matched_video': best_match['video'],
                    'similarity': similarity,
                    'confidence': 'medium',
                    'needs_review': True,
                    'scanned_count': len(existing_videos)
                }, status=status.HTTP_200_OK)
            
        logger.info(f"No match found. Highest similarity: {highest_similarity}")

        # No significant match found
        return Response({
            'is_duplicate': False,
            'matched_video': None,
            'similarity': highest_similarity,
            'confidence': 'low',
            'needs_review': False,
            'scanned_count': len(existing_videos)
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
