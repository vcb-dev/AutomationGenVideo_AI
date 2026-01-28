"""
Views for managing video collections.

Provides CRUD operations for collections and adding/removing videos.
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from ..models import VideoCollection, CollectionVideo, ScrapedVideo
from ..serializers import (
    VideoCollectionSerializer,
    VideoCollectionListSerializer,
    AddVideoToCollectionSerializer,
    AddVideosToCollectionSerializer,
    VideoSerializer,
)


class VideoCollectionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing video collections.
    
    Provides:
    - List all collections
    - Create new collection
    - Get collection detail with videos
    - Update collection
    - Delete collection
    - Add video to collection
    - Remove video from collection
    """
    queryset = VideoCollection.objects.all().prefetch_related('collection_videos__video')
    
    def get_serializer_class(self):
        """Use lightweight serializer for list, full for detail."""
        if self.action == 'list':
            return VideoCollectionListSerializer
        return VideoCollectionSerializer
    
    def list(self, request):
        """List all collections."""
        collections = self.get_queryset()
        serializer = self.get_serializer(collections, many=True)
        return Response({
            'success': True,
            'count': collections.count(),
            'collections': serializer.data
        })
    
    def create(self, request):
        """Create a new collection."""
        serializer = VideoCollectionSerializer(data=request.data)
        if serializer.is_valid():
            collection = serializer.save()
            return Response({
                'success': True,
                'message': 'Collection created successfully',
                'collection': VideoCollectionSerializer(collection).data
            }, status=status.HTTP_201_CREATED)
        return Response({
            'success': False,
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    
    def retrieve(self, request, pk=None):
        """Get collection detail with videos."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        serializer = VideoCollectionSerializer(collection)
        return Response({
            'success': True,
            'collection': serializer.data
        })
    
    def update(self, request, pk=None, **kwargs):
        """Update collection."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        partial = kwargs.pop('partial', False)
        serializer = VideoCollectionSerializer(collection, data=request.data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Collection updated successfully',
                'collection': serializer.data
            })
        return Response({
            'success': False,
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    
    def destroy(self, request, pk=None):
        """Delete collection."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        collection_name = collection.name
        collection.delete()
        return Response({
            'success': True,
            'message': f'Collection "{collection_name}" deleted successfully'
        })
    
    @action(detail=True, methods=['post'])
    def add_video(self, request, pk=None):
        """Add a video to collection."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        serializer = AddVideoToCollectionSerializer(data=request.data)
        
        if not serializer.is_valid():
            return Response({
                'success': False,
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)
        
        video_id = serializer.validated_data['video_id']
        video = get_object_or_404(ScrapedVideo, pk=video_id)
        
        # Check if video already in collection
        if CollectionVideo.objects.filter(collection=collection, video=video).exists():
            return Response({
                'success': False,
                'error': 'Video already in this collection'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Add video to collection
        collection_video = CollectionVideo.objects.create(
            collection=collection,
            video=video,
            notes=serializer.validated_data.get('notes', ''),
            order=serializer.validated_data.get('order', 0)
        )
        
        return Response({
            'success': True,
            'message': f'Video added to "{collection.name}"',
            'collection': VideoCollectionSerializer(collection).data
        }, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['delete'], url_path='remove-video/(?P<video_id>[^/.]+)')
    def remove_video(self, request, pk=None, video_id=None):
        """Remove a video from collection."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        video = get_object_or_404(ScrapedVideo, pk=video_id)
        
        collection_video = CollectionVideo.objects.filter(
            collection=collection,
            video=video
        ).first()
        
        if not collection_video:
            return Response({
                'success': False,
                'error': 'Video not in this collection'
            }, status=status.HTTP_404_NOT_FOUND)
        
        collection_video.delete()
        
        return Response({
            'success': True,
            'message': f'Video removed from "{collection.name}"'
        })
    
    @action(detail=True, methods=['get'])
    def videos(self, request, pk=None):
        """Get all videos in collection."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        videos = collection.videos.all()
        serializer = VideoSerializer(videos, many=True)
        return Response({
            'success': True,
            'collection_name': collection.name,
            'count': videos.count(),
            'videos': serializer.data
        })

    @action(detail=True, methods=['post'], url_path='add_videos')
    def add_videos(self, request, pk=None):
        """Add multiple videos to collection."""
        collection = get_object_or_404(VideoCollection, pk=pk)
        serializer = AddVideosToCollectionSerializer(data=request.data)
        
        if not serializer.is_valid():
            return Response({
                'success': False,
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)
        
        video_ids = serializer.validated_data['video_ids']
        notes = serializer.validated_data.get('notes', '')
        
        # Get all videos
        videos = ScrapedVideo.objects.filter(id__in=video_ids)
        
        added_count = 0
        existing_count = 0
        
        for video in videos:
            if not CollectionVideo.objects.filter(collection=collection, video=video).exists():
                CollectionVideo.objects.create(
                    collection=collection,
                    video=video,
                    notes=notes
                )
                added_count += 1
            else:
                existing_count += 1
                
        return Response({
            'success': True,
            'message': f'Added {added_count} videos. {existing_count} already existed.',
            'added_count': added_count,
            'collection': VideoCollectionSerializer(collection).data
        }, status=status.HTTP_201_CREATED)
