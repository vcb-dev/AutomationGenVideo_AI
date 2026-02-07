"""
Service for parsing Excel product catalogs.
"""
import pandas as pd
import os
from typing import List, Dict, Any
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from video_management.models import ProductList, Product
import logging

logger = logging.getLogger(__name__)


class ProductCatalogService:
    """Service for handling product catalog uploads and parsing."""
    
    # Expected column mappings (flexible)
    COLUMN_MAPPINGS = {
        'name': ['name', 'product_name', 'tên sản phẩm', 'ten san pham', 'product'],
        'category': ['category', 'loại', 'loai', 'type', 'danh mục', 'danh muc'],
        'price': ['price', 'giá', 'gia', 'cost', 'price_vnd'],
        'description': ['description', 'mô tả', 'mo ta', 'desc', 'chi tiết', 'chi tiet'],
        'highlights': ['highlights', 'đặc điểm', 'dac diem', 'features', 'nổi bật', 'noi bat'],
        'sku': ['sku', 'code', 'mã', 'ma', 'product_code', 'product_id'],
    }
    
    @classmethod
    def parse_excel_file(cls, file: UploadedFile, list_name: str = None, description: str = "") -> ProductList:
        """
        Parse an Excel file and create ProductList with Products.
        
        Args:
            file: Uploaded Excel file
            list_name: Name for the product list (defaults to filename)
            description: Optional description
            
        Returns:
            ProductList instance with all products
        """
        try:
            # Read Excel file efficiently
            # engine='openpyxl' is generally faster for xlsx
            # dtype=str ensures phone numbers/ids aren't converted to scientific notation and speeds up reading
            df = pd.read_excel(file, engine='openpyxl', dtype=str)
            
            # Clean data: Remove rows where all columns are NaN
            df.dropna(how='all', inplace=True)
            
            # Replace NaN with empty string/None to avoid DB errors
            df = df.where(pd.notnull(df), None)
            
            if df.empty:
                raise ValueError("Excel file is empty or contains no valid data")
            
            # Determine list name
            if not list_name:
                list_name = os.path.splitext(file.name)[0]
            
            with transaction.atomic():
                # Create ProductList
                product_list = ProductList.objects.create(
                    name=list_name,
                    file_name=file.name,
                    file_path=f"uploads/products/{file.name}",  # Will be saved later
                    total_products=0,
                    description=description
                )
                
                # Parse products
                products = cls._parse_dataframe(df, product_list)
                
                if not products:
                    raise ValueError("Could not extract any valid products from the file")
                
                # Bulk create products with batch_size to avoid memory issues
                Product.objects.bulk_create(products, batch_size=1000)
                
                # Update total count
                product_list.total_products = len(products)
                product_list.save()
            
            logger.info(f"Successfully parsed {len(products)} products from {file.name}")
            
            return product_list
            
        except Exception as e:
            logger.error(f"Error parsing Excel file: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to parse Excel file: {str(e)}")
    
    @classmethod
    def _parse_dataframe(cls, df: pd.DataFrame, product_list: ProductList) -> List[Product]:
        """
        Parse DataFrame into Product instances.
        
        Args:
            df: Pandas DataFrame from Excel
            product_list: ProductList to associate products with
            
        Returns:
            List of Product instances (not saved)
        """
        products = []
        
        # Normalize column names
        df.columns = df.columns.str.strip().str.lower()
        
        # Map columns
        column_map = cls._map_columns(df.columns.tolist())
        
        logger.info(f"Column mapping: {column_map}")
        
        for idx, row in df.iterrows():
            try:
                # Extract fields using column mapping
                category_raw = cls._get_value(row, column_map.get('category'), default="")
                # Normalize category: Title Case to reduce fragmentation (e.g. "NHẪN" -> "Nhẫn")
                category = category_raw.title() if category_raw else ""

                product_data = {
                    'product_list': product_list,
                    'name': cls._get_value(row, column_map.get('name'), default=f"Product {idx+1}"),
                    'category': category,
                    'price': cls._parse_price(cls._get_value(row, column_map.get('price'))),
                    'description': cls._get_value(row, column_map.get('description'), default=""),
                    'highlights': cls._get_value(row, column_map.get('highlights'), default=""),
                    'sku': cls._get_value(row, column_map.get('sku'), default=""),
                    'raw_data': row.to_dict()  # Store all original data
                }
                
                products.append(Product(**product_data))
                
            except Exception as e:
                logger.warning(f"Error parsing row {idx}: {str(e)}")
                continue
        
        return products
    
    @classmethod
    def _map_columns(cls, columns: List[str]) -> Dict[str, str]:
        """
        Map Excel columns to our field names.
        
        Args:
            columns: List of column names from Excel
            
        Returns:
            Dict mapping our field names to actual column names
        """
        mapping = {}
        
        for field, possible_names in cls.COLUMN_MAPPINGS.items():
            for col in columns:
                if col in possible_names:
                    mapping[field] = col
                    break
        
        return mapping
    
    @classmethod
    def _get_value(cls, row: pd.Series, column: str, default: Any = "") -> Any:
        """Get value from row, handling missing columns and NaN values."""
        if column is None or column not in row:
            return default
        
        value = row[column]
        
        # Handle NaN
        if pd.isna(value):
            return default
        
        # Convert to string and strip
        return str(value).strip()
    
    @classmethod
    def _parse_price(cls, value: Any) -> float:
        """Parse price value, handling various formats."""
        if value is None or value == "":
            return None
        
        try:
            # Remove common currency symbols and separators
            if isinstance(value, str):
                value = value.replace('đ', '').replace('₫', '').replace(',', '').replace('.', '').strip()
            
            return float(value)
        except (ValueError, TypeError):
            return None
    
    @classmethod
    def get_products_by_category(cls, product_list_id: int) -> Dict[str, List[Product]]:
        """
        Get products grouped by category.
        
        Args:
            product_list_id: ID of the product list
            
        Returns:
            Dict with categories as keys and lists of products as values
        """
        products = Product.objects.filter(product_list_id=product_list_id).order_by('category', 'name')
        
        grouped = {}
        for product in products:
            category = product.category or "Khác"
            if category not in grouped:
                grouped[category] = []
            grouped[category].append(product)
        
        return grouped
