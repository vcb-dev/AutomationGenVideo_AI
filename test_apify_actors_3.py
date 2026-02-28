import os
import sys
import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings
from apify_client import ApifyClient

client = ApifyClient(settings.APIFY_API_TOKEN)
# ApifyClient doesn't have a store search.
print("Client initialized")
