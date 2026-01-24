from rest_framework import viewsets
from ..models import TrackedChannel
from ..serializers import TrackedChannelSerializer

class TrackedChannelViewSet(viewsets.ModelViewSet):
    """
    ViewSet for viewing and editing tracked channels.
    """
    queryset = TrackedChannel.objects.all()
    serializer_class = TrackedChannelSerializer
