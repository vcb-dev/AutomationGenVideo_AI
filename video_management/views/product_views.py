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
