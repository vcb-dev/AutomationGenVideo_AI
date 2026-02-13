"""
Views for product catalog management.
"""
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from video_management.models import ProductList, Product
from video_management.serializers import (
    ProductListSerializer,
    ProductListSummarySerializer,
    ProductSerializer
)
from video_management.services.product_catalog_service import ProductCatalogService
import os
import logging

logger = logging.getLogger(__name__)


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def upload_product_catalog(request):
    """
    Upload and parse an Excel product catalog.
    
    POST /api/products/upload/
    Form Data:
        - file: Excel file (.xlsx, .xls)
        - name: Optional name for the product list
        - description: Optional description
    """
    try:
        # Get uploaded file
        file = request.FILES.get('file')
        if not file:
            return Response(
                {'error': 'No file provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate file extension
        if not file.name.endswith(('.xlsx', '.xls')):
            return Response(
                {'error': 'Invalid file type. Please upload an Excel file (.xlsx or .xls)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get optional parameters
        list_name = request.data.get('name')
        description = request.data.get('description', '')
        
        # Parse Excel file
        product_list = ProductCatalogService.parse_excel_file(
            file=file,
            list_name=list_name,
            description=description
        )
        
        # Serialize and return
        serializer = ProductListSerializer(product_list)
        
        return Response({
            'success': True,
            'message': f'Successfully uploaded {product_list.total_products} products',
            'product_list': serializer.data
        }, status=status.HTTP_201_CREATED)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Error uploading product catalog: {str(e)}", exc_info=True)
        return Response(
            {'error': 'Failed to upload product catalog'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def list_product_catalogs(request):
    """
    Get all product catalogs.
    
    GET /api/products/catalogs/
    """
    try:
        catalogs = ProductList.objects.all().order_by('-created_at')
        serializer = ProductListSummarySerializer(catalogs, many=True)
        
        return Response({
            'success': True,
            'catalogs': serializer.data
        })
        
    except Exception as e:
        logger.error(f"Error listing product catalogs: {str(e)}", exc_info=True)
        return Response(
            {'error': 'Failed to list product catalogs'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def get_product_catalog(request, catalog_id):
    """
    Get a specific product catalog with all products.
    
    GET /api/products/catalogs/<catalog_id>/
    """
    try:
        catalog = get_object_or_404(ProductList, id=catalog_id)
        serializer = ProductListSerializer(catalog)
        
        return Response({
            'success': True,
            'catalog': serializer.data
        })
        
    except Exception as e:
        logger.error(f"Error getting product catalog: {str(e)}", exc_info=True)
        return Response(
            {'error': 'Failed to get product catalog'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def get_products_by_category(request, catalog_id):
    """
    Get products grouped by category.
    
    GET /api/products/catalogs/<catalog_id>/by-category/
    """
    try:
        catalog = get_object_or_404(ProductList, id=catalog_id)
        grouped_products = ProductCatalogService.get_products_by_category(catalog_id)
        
        # Serialize grouped products
        result = {}
        for category, products in grouped_products.items():
            result[category] = ProductSerializer(products, many=True).data
        
        return Response({
            'success': True,
            'catalog_id': catalog_id,
            'catalog_name': catalog.name,
            'categories': result
        })
        
    except Exception as e:
        logger.error(f"Error getting products by category: {str(e)}", exc_info=True)
        return Response(
            {'error': 'Failed to get products by category'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['DELETE'])
def delete_product_catalog(request, catalog_id):
    """
    Delete a product catalog and all its products.
    
    DELETE /api/products/catalogs/<catalog_id>/
    """
    try:
        catalog = get_object_or_404(ProductList, id=catalog_id)
        catalog_name = catalog.name
        catalog.delete()
        
        return Response({
            'success': True,
            'message': f'Successfully deleted catalog: {catalog_name}'
        })
        
    except Exception as e:
        logger.error(f"Error deleting product catalog: {str(e)}", exc_info=True)
        return Response(
            {'error': 'Failed to delete product catalog'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def get_product_detail(request, product_id):
    """
    Get details of a specific product.
    
    GET /api/products/<product_id>/
    """
    try:
        product = get_object_or_404(Product, id=product_id)
        serializer = ProductSerializer(product)
        
        return Response({
            'success': True,
            'product': serializer.data
        })
        
    except Exception as e:
        logger.error(f"Error getting product detail: {str(e)}", exc_info=True)
        return Response(
            {'error': 'Failed to get product detail'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['GET'])
def find_product_video_path(request):
    """
    Find a video file path in 'IndexedVideo' or 'Sản phẩm' folder based on SKU.
    
    GET /api/products/find-video/?sku=...
    """
    try:
        sku = request.query_params.get('sku')
        if not sku:
             return Response({'error': 'SKU is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        sku_normalized = sku.strip()
        
        from video_management.models import IndexedVideo, Product
        
        # 1. Search in IndexedVideo (exact match on filename or partial)
        # Filter for folder_type="Sản phẩm"
        
        # Build query:
        # Priority 1: Filename contains SKU exactly
        videos = IndexedVideo.objects.filter(
            folder_type="Sản phẩm",
            is_available=True,
            file_path__icontains=sku_normalized
        )
        
        if videos.exists():
            # If multiple found, pick the one with shortest filename (likely exact match)
            # or just first one
            video = sorted(videos, key=lambda v: len(v.file_path))[0]
            
            return Response({
                'success': True,
                'sku': sku,
                'video_path': video.file_path,
                'video_id': video.id,
                'source': 'indexed_db'
            })
            
        # 2. If not found in DB, try Real-time Scan
        logger.info(f"⚠️ SKU '{sku}' not found in DB. Trying real-time scan...")
        
        from video_management.services.smart_preprocessing_service import get_preprocessing_service
        service = get_preprocessing_service()
        
        # Determine product folder path - use a set of known paths or infer from existing DB entries
        known_paths = [
             r"\\VCB_MEDIA\MEDIA VCB folder\VIDEO Sản Phẩm",
             r"\\192.168.1.250\MEDIA VCB folder\VIDEO Sản Phẩm",
             r"Z:\VIDEO Sản Phẩm",
             r"D:\VIDEO Sản Phẩm",
             r"E:\VIDEO Sản Phẩm"
        ]
        
        # Try to infer path from any existing "Sản phẩm" videos
        existing_sample = IndexedVideo.objects.filter(folder_type="Sản phẩm").first()
        if existing_sample:
            # Try to get the root folder part (e.g. up to 'VIDEO Sản Phẩm')
            path_str = existing_sample.file_path
            if "VIDEO Sản Phẩm" in path_str:
                root_part = path_str.split("VIDEO Sản Phẩm")[0] + "VIDEO Sản Phẩm"
                if root_part not in known_paths:
                    known_paths.insert(0, root_part)

        prod_folder_path = None
        for p in known_paths:
             if os.path.exists(p):
                 prod_folder_path = p
                 break
        
        if prod_folder_path:
            logger.info(f"Using product video path for scan: {prod_folder_path}")
            video_id = service.scan_and_index_specific_sku(sku_normalized, prod_folder_path)
            
            if video_id:
                 video = IndexedVideo.objects.get(id=video_id)
                 return Response({
                    'success': True,
                    'sku': sku,
                    'video_path': video.file_path,
                    'video_id': video.id,
                    'source': 'realtime_scan'
                })
        else:
            logger.warning("Could not determine product folder path for real-time scan.")
        
        return Response({
            'success': False,
            'message': f'No video found for SKU: {sku} (checked DB and Disk)'
        }, status=status.HTTP_404_NOT_FOUND)
        
    except Exception as e:
        logger.error(f"Error finding product video: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
